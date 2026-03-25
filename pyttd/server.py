import os
import sys
import socket
import signal
import selectors
import threading
import queue
import logging
import time
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.runner import Runner
from pyttd.session import Session
from pyttd.protocol import JsonRpcConnection
from pyttd.models.storage import compute_db_path

logger = logging.getLogger(__name__)


class PyttdServer:
    def __init__(self, script: str | None, is_module: bool = False, cwd: str = '.',
                 checkpoint_interval: int = 1000, replay_db: str | None = None,
                 include_functions: list[str] | None = None,
                 max_frames: int = 0, env_vars: dict | None = None,
                 include_files: list[str] | None = None,
                 exclude_functions: list[str] | None = None,
                 exclude_files: list[str] | None = None,
                 db_path: str | None = None,
                 max_db_size_mb: int = 0,
                 keep_runs: int = 0,
                 target_run_id: str | None = None):
        self.script = script
        self.is_module = is_module
        self.cwd = os.path.abspath(cwd)
        self.config = PyttdConfig(
            checkpoint_interval=checkpoint_interval,
            include_functions=include_functions or [],
            max_frames=max_frames,
            include_files=include_files or [],
            exclude_functions=exclude_functions or [],
            exclude_files=exclude_files or [],
            max_db_size_mb=max_db_size_mb,
            keep_runs=keep_runs,
        )
        self._launch_env = env_vars or {}
        self.recorder = Recorder(self.config)
        self.runner = Runner()
        self.session = Session()
        self._sel = selectors.DefaultSelector()
        self._wakeup_r, self._wakeup_w = os.pipe()
        os.set_blocking(self._wakeup_r, False)
        self._msg_queue = queue.Queue()
        self._recording_thread = None
        self._recording = False
        self._shutdown = False
        self._rpc = None
        self._conn = None
        self._frame_count = 0
        self._frame_count_lock = threading.Lock()
        self._replay_db = replay_db
        self._target_run_id = target_run_id

        # Compute DB path
        if replay_db:
            self._db_path = os.path.abspath(replay_db)
        else:
            self._db_path = compute_db_path(
                script, is_module=is_module, cwd=self.cwd,
                explicit_path=db_path,
            )

        self._script_args = []
        self._saved_stdout = None
        self._saved_stderr = None
        self._capture_r_stdout = None
        self._capture_r_stderr = None

    def _setup_capture(self):
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()
        self._saved_stdout = os.dup(1)
        self._saved_stderr = os.dup(2)
        os.dup2(w_out, 1)
        os.dup2(w_err, 2)
        os.close(w_out)
        os.close(w_err)
        self._capture_r_stdout = r_out
        self._capture_r_stderr = r_err
        os.set_blocking(r_out, False)
        os.set_blocking(r_err, False)
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)

    def _restore_capture(self):
        if self._capture_r_stdout is not None:
            try:
                self._sel.unregister(self._capture_r_stdout)
            except (KeyError, ValueError):
                pass
            os.close(self._capture_r_stdout)
            self._capture_r_stdout = None
        if self._capture_r_stderr is not None:
            try:
                self._sel.unregister(self._capture_r_stderr)
            except (KeyError, ValueError):
                pass
            os.close(self._capture_r_stderr)
            self._capture_r_stderr = None
        if self._saved_stdout is not None:
            os.dup2(self._saved_stdout, 1)
            os.close(self._saved_stdout)
            self._saved_stdout = None
        if self._saved_stderr is not None:
            os.dup2(self._saved_stderr, 2)
            os.close(self._saved_stderr)
            self._saved_stderr = None

    def run(self):
        # 1. Bind TCP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        # 2. Write port handshake (BEFORE stdout capture)
        sys.stdout.write(f"PYTTD_PORT:{port}\n")
        sys.stdout.flush()

        # 3. Capture stdout/stderr
        self._setup_capture()

        # 4. Install signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # 5. Accept connection (30s timeout)
        sock.settimeout(30)
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            logger.error("Timeout waiting for adapter connection")
            self._restore_capture()
            sock.close()
            self._close_wakeup_fds()
            return
        sock.settimeout(None)
        self._conn = conn
        self._rpc = JsonRpcConnection(conn)

        # 6. Register with selector
        self._sel.register(conn, selectors.EVENT_READ, 'rpc')
        self._sel.register(self._wakeup_r, selectors.EVENT_READ, 'wakeup')
        if self._capture_r_stdout:
            self._sel.register(self._capture_r_stdout, selectors.EVENT_READ, 'stdout')
        if self._capture_r_stderr:
            self._sel.register(self._capture_r_stderr, selectors.EVENT_READ, 'stderr')

        # 7. Event loop
        self._event_loop()

        # 8. Cleanup
        if self._recording_thread and self._recording_thread.is_alive():
            import pyttd_native
            pyttd_native.request_stop()
            self._recording_thread.join(timeout=2.0)

        self.recorder.cleanup()
        self._restore_capture()
        try:
            self._sel.unregister(conn)
        except (KeyError, ValueError):
            pass
        try:
            self._sel.unregister(self._wakeup_r)
        except (KeyError, ValueError):
            pass
        self._sel.close()
        conn.close()
        sock.close()
        self._close_wakeup_fds()

    def _close_wakeup_fds(self):
        if self._wakeup_r >= 0:
            os.close(self._wakeup_r)
            self._wakeup_r = -1
        if self._wakeup_w >= 0:
            os.close(self._wakeup_w)
            self._wakeup_w = -1

    def _signal_handler(self, signum, frame):
        if self._recording:
            import pyttd_native
            pyttd_native.request_stop()
        self._shutdown = True
        try:
            os.write(self._wakeup_w, b'\x01')
        except OSError:
            pass

    def _event_loop(self):
        while not self._shutdown:
            timeout = 0.5 if self._recording else 0.1
            try:
                events = self._sel.select(timeout=timeout)
            except OSError:
                break

            for key, mask in events:
                if key.data == 'rpc':
                    try:
                        data = key.fileobj.recv(4096)
                    except (ConnectionResetError, OSError):
                        self._shutdown = True
                        break
                    self._rpc.feed(data)
                    if self._rpc.is_closed:
                        self._shutdown = True
                        break
                    while True:
                        try:
                            msg = self._rpc.try_read_message()
                        except Exception as e:
                            logger.warning("Malformed RPC message: %s", e)
                            break
                        if msg is None:
                            break
                        self._dispatch(msg)

                elif key.data == 'wakeup':
                    try:
                        os.read(self._wakeup_r, 1024)
                    except (BlockingIOError, OSError):
                        pass
                    self._process_messages()

                elif key.data == 'stdout':
                    try:
                        data = os.read(key.fileobj, 4096)
                    except (BlockingIOError, OSError):
                        data = b''
                    if data and self._rpc and not self._rpc.is_closed:
                        self._rpc.send_notification("output", {
                            "category": "stdout",
                            "output": data.decode('utf-8', errors='replace')
                        })

                elif key.data == 'stderr':
                    try:
                        data = os.read(key.fileobj, 4096)
                    except (BlockingIOError, OSError):
                        data = b''
                    if data and self._rpc and not self._rpc.is_closed:
                        self._rpc.send_notification("output", {
                            "category": "stderr",
                            "output": data.decode('utf-8', errors='replace')
                        })

            # Progress notifications during recording
            if self._recording and not self._shutdown and self._rpc and not self._rpc.is_closed:
                progress_data = {
                    "elapsedMs": int((time.monotonic() - self._recording_start) * 1000),
                }
                try:
                    import pyttd_native
                    stats = pyttd_native.get_recording_stats()
                    progress_data["frameCount"] = stats.get('frame_count', 0)
                    progress_data["droppedFrames"] = stats.get('dropped_frames', 0)
                    progress_data["poolOverflows"] = stats.get('pool_overflows', 0)
                    progress_data["checkpointCount"] = stats.get('checkpoint_count', 0)
                    progress_data["checkpointMemoryMB"] = round(
                        stats.get('checkpoint_memory_bytes', 0) / (1024 * 1024), 1)
                except Exception:
                    pass
                try:
                    progress_data["dbSizeMB"] = round(os.path.getsize(self._db_path) / (1024 * 1024), 1)
                except OSError:
                    pass
                self._rpc.send_notification("progress", progress_data)

    def _process_messages(self):
        while True:
            try:
                msg = self._msg_queue.get_nowait()
            except queue.Empty:
                break
            if msg["type"] == "recording_complete":
                self._on_recording_complete(msg)

    def _on_recording_complete(self, msg):
        self._recording = False
        from pyttd.models.db import db

        # If exception, send traceback as stderr output
        error_info = msg.get("error")
        if error_info and self._rpc and not self._rpc.is_closed:
            self._rpc.send_notification("output", {
                "category": "stderr",
                "output": error_info.get("traceback", "")
            })

        # Find first line event
        first_line = db.fetchone(
            "SELECT * FROM executionframes "
            "WHERE run_id = ? AND frame_event = 'line' "
            "ORDER BY sequence_no LIMIT 1",
            (str(self.recorder.run_id),))
        first_line_seq = first_line.sequence_no if first_line else 0

        self.session.enter_replay(self.recorder.run_id, first_line_seq)

        stats = msg.get("stats", {})
        total_frames = stats.get("frame_count", 0)
        thread_id = first_line.thread_id if first_line else 0
        if self._rpc and not self._rpc.is_closed:
            self._rpc.send_notification("stopped", {
                "seq": first_line_seq,
                "reason": "recording_complete",
                "totalFrames": total_frames,
                "thread_id": thread_id,
            })

    def _dispatch(self, msg: dict):
        method = msg.get("method")
        params = msg.get("params", {})
        request_id = msg.get("id")

        handler = {
            "backend_init": self._handle_backend_init,
            "launch": self._handle_launch,
            "configuration_done": self._handle_configuration_done,
            "set_breakpoints": self._handle_set_breakpoints,
            "set_exception_breakpoints": self._handle_set_exception_breakpoints,
            "interrupt": self._handle_interrupt,
            "get_threads": self._handle_get_threads,
            "get_stack_trace": self._handle_get_stack_trace,
            "get_scopes": self._handle_get_scopes,
            "get_variables": self._handle_get_variables,
            "evaluate": self._handle_evaluate,
            "continue": self._handle_continue,
            "next": self._handle_next,
            "step_in": self._handle_step_in,
            "step_out": self._handle_step_out,
            "step_back": self._handle_step_back,
            "reverse_continue": self._handle_reverse_continue,
            "goto_frame": self._handle_goto_frame,
            "goto_targets": self._handle_goto_targets,
            "restart_frame": self._handle_restart_frame,
            "get_timeline_summary": self._handle_get_timeline_summary,
            "get_traced_files": self._handle_get_traced_files,
            "get_execution_stats": self._handle_get_execution_stats,
            "get_call_children": self._handle_get_call_children,
            "get_variable_children": self._handle_get_variable_children,
            "get_variable_history": self._handle_get_variable_history,
            "get_checkpoint_memory": self._handle_get_checkpoint_memory,
            "set_function_breakpoints": self._handle_set_function_breakpoints,
            "set_data_breakpoints": self._handle_set_data_breakpoints,
            "disconnect": self._handle_disconnect,
        }.get(method)

        if handler is None:
            if request_id is not None:
                self._rpc.send_error(request_id, -32601, f"Method not found: {method}")
            return

        try:
            result = handler(params)
            if request_id is not None:
                self._rpc.send_response(request_id, result or {})
        except Exception as e:
            logger.exception("RPC handler error: %s", method)
            if request_id is not None:
                self._rpc.send_error(request_id, -32603, str(e))

    # --- RPC Handlers ---

    def _handle_backend_init(self, params: dict) -> dict:
        import pyttd
        capabilities = ["recording", "warm_navigation"]
        try:
            import pyttd_native
            if hasattr(pyttd_native, 'restore_checkpoint'):
                capabilities.extend(["cold_navigation", "checkpoints"])
        except ImportError:
            pass
        return {"version": pyttd.__version__, "capabilities": capabilities}

    def _handle_launch(self, params: dict) -> dict:
        self._script_args = params.get("args", [])
        if "checkpointInterval" in params:
            self.config.checkpoint_interval = params["checkpointInterval"]
        if "traceDb" in params:
            db_path = params["traceDb"]
            if not os.path.isabs(db_path):
                db_path = os.path.join(self.cwd, db_path)
            self._db_path = db_path
        if "includePatterns" in params:
            self.config.include_functions = params["includePatterns"]
        if "maxFrames" in params:
            self.config.max_frames = params["maxFrames"]
        env_vars = params.get("env", {})
        if env_vars:
            self._launch_env = env_vars
        return {}

    def _handle_configuration_done(self, params: dict) -> dict:
        if self._replay_db:
            self._enter_replay_db()
        else:
            self._start_recording()
        return {}

    def _handle_set_breakpoints(self, params: dict) -> dict:
        breakpoints = params.get("breakpoints", [])
        self.session.set_breakpoints(breakpoints)
        if self.session.state == "replay":
            verification = self.session.verify_breakpoints(breakpoints)
            return {"verified": verification}
        return {}

    def _handle_set_exception_breakpoints(self, params: dict) -> dict:
        self.session.set_exception_filters(params.get("filters", []))
        return {}

    def _handle_interrupt(self, params: dict) -> dict:
        if self._recording:
            import pyttd_native
            pyttd_native.request_stop()
        return {}

    def _handle_get_threads(self, params: dict) -> dict:
        if self.session.state == "replay" and self.session.known_threads:
            return {"threads": self.session.get_threads()}
        return {"threads": [{"id": 1, "name": "Main Thread"}]}

    def _handle_get_stack_trace(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"frames": []}
        seq = params.get("seq", self.session.current_frame_seq)
        frames = self.session.get_stack_at(seq)
        return {"frames": frames}

    def _handle_get_scopes(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"scopes": []}
        seq = params.get("seq", self.session.current_frame_seq)
        return {"scopes": [{"name": "Locals", "variablesReference": seq + 1}]}

    def _handle_get_variables(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"variables": []}
        seq = params.get("seq", self.session.current_frame_seq)
        variables = self.session.get_variables_at(seq)
        return {"variables": variables}

    def _handle_evaluate(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"result": "", "error": "not_in_replay"}
        seq = params.get("seq", self.session.current_frame_seq)
        expression = params.get("expression", "")
        context = params.get("context", "hover")
        return self.session.evaluate_at(seq, expression, context)

    def _handle_continue(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        result = self.session.continue_forward()
        # Send accumulated log point messages
        for msg in getattr(self.session, '_log_messages', []):
            if self._rpc and not self._rpc.is_closed:
                self._rpc.send_notification("logpoint", {"message": msg})
        # Surface condition eval errors as notifications
        for err in self.session.get_condition_errors():
            if self._rpc and not self._rpc.is_closed:
                self._rpc.send_notification("conditionError", {
                    "seq": err["seq"],
                    "condition": err["condition"],
                    "error": err["error"],
                })
        return result

    def _handle_next(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        return self.session.step_over()

    def _handle_step_in(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        return self.session.step_into()

    def _handle_step_out(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        return self.session.step_out()

    def _handle_step_back(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        return self.session.step_back()

    def _handle_reverse_continue(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        result = self.session.reverse_continue()
        for err in self.session.get_condition_errors():
            if self._rpc and not self._rpc.is_closed:
                self._rpc.send_notification("conditionError", {
                    "seq": err["seq"],
                    "condition": err["condition"],
                    "error": err["error"],
                })
        return result

    def _handle_goto_frame(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        target_seq = params.get("targetSeq")
        if target_seq is None:
            target_seq = params.get("target_seq")
        if target_seq is None:
            return {"error": "missing_targetSeq"}
        return self.session.goto_frame(target_seq)

    def _handle_goto_targets(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        filename = params.get("filename", "")
        line = params.get("line", 0)
        return {"targets": self.session.goto_targets(filename, line)}

    def _handle_restart_frame(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        frame_seq = params.get("frameSeq")
        if frame_seq is None:
            frame_seq = params.get("frame_seq")
        if frame_seq is None:
            return {"error": "missing_frameSeq"}
        return self.session.restart_frame(frame_seq)

    def _handle_get_timeline_summary(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        from pyttd.models.timeline import get_timeline_summary
        start_seq = params.get("startSeq", 0)
        end_seq = params.get("endSeq", self.session.last_line_seq or 0)
        bucket_count = params.get("bucketCount", 500)
        buckets = get_timeline_summary(
            self.session.run_id, start_seq, end_seq, bucket_count,
            breakpoints=self.session.breakpoints)
        return {"buckets": buckets}

    def _handle_get_traced_files(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        return {"files": self.session.get_traced_files()}

    def _handle_get_execution_stats(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        filename = params.get("filename", "")
        return {"stats": self.session.get_execution_stats(filename)}

    def _handle_get_call_children(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        parent_call_seq = params.get("parentCallSeq")
        parent_return_seq = params.get("parentReturnSeq")
        return {"children": self.session.get_call_children(parent_call_seq, parent_return_seq)}

    def _handle_get_variable_children(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        seq = params.get("seq")
        var_name = params.get("variableName")
        if seq is not None and var_name is not None:
            return {"variables": self.session.get_variable_children_by_name(seq, var_name)}
        ref = params.get("variablesReference", 0)
        return {"variables": self.session.get_variable_children(ref)}

    def _handle_get_variable_history(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        name = params.get("variableName", "")
        start_seq = params.get("startSeq", 0)
        end_seq = params.get("endSeq", self.session.last_line_seq or 0)
        max_points = params.get("maxPoints", 500)
        return {"history": self.session.get_variable_history(name, start_seq, end_seq, max_points)}

    def _handle_get_checkpoint_memory(self, params: dict) -> dict:
        import pyttd_native
        mem_info = pyttd_native.get_checkpoint_memory()
        mem_info["limitMB"] = self.config.checkpoint_memory_limit_mb
        return mem_info

    def _handle_set_function_breakpoints(self, params: dict) -> dict:
        breakpoints = params.get("breakpoints", [])
        self.session.set_function_breakpoints(breakpoints)
        if self.session.state == "replay":
            verification = self.session.verify_function_breakpoints(breakpoints)
            return {"verified": verification}
        return {}

    def _handle_set_data_breakpoints(self, params: dict) -> dict:
        breakpoints = params.get("breakpoints", [])
        self.session.set_data_breakpoints(breakpoints)
        return {"verified": [{"verified": True} for _ in breakpoints]}

    def _handle_disconnect(self, params: dict) -> dict:
        if self._recording:
            import pyttd_native
            pyttd_native.request_stop()
            if self._recording_thread and self._recording_thread.is_alive():
                self._recording_thread.join(timeout=2.0)
        self._shutdown = True
        return {}

    # --- Recording ---

    def _enter_replay_db(self):
        """Enter replay mode from an existing .pyttd.db (no recording)."""
        from pyttd.models import storage
        from pyttd.models.db import db

        storage.connect_to_db(self._db_path)
        storage.initialize_schema()

        if self._target_run_id:
            from pyttd.query import get_run_by_id
            try:
                last_run = get_run_by_id(self._db_path, self._target_run_id)
            except ValueError as e:
                if self._rpc and not self._rpc.is_closed:
                    self._rpc.send_notification("output", {
                        "category": "stderr",
                        "output": f"{e}\n",
                    })
                self._shutdown = True
                return
        else:
            last_run = db.fetchone(
                "SELECT * FROM runs ORDER BY timestamp_start DESC LIMIT 1")
        if not last_run:
            if self._rpc and not self._rpc.is_closed:
                self._rpc.send_notification("output", {
                    "category": "stderr",
                    "output": f"No runs found in {self._db_path}\n",
                })
            self._shutdown = True
            return

        first_line = db.fetchone(
            "SELECT * FROM executionframes "
            "WHERE run_id = ? AND frame_event = 'line' "
            "ORDER BY sequence_no LIMIT 1",
            (str(last_run.run_id),))
        first_line_seq = first_line.sequence_no if first_line else 0

        self.session.enter_replay(last_run.run_id, first_line_seq)

        total_frames = last_run.total_frames or 0
        thread_id = first_line.thread_id if first_line else 0
        if self._rpc and not self._rpc.is_closed:
            self._rpc.send_notification("stopped", {
                "seq": first_line_seq,
                "reason": "recording_complete",
                "totalFrames": total_frames,
                "thread_id": thread_id,
            })

    def _start_recording(self):
        script_abs = self.script
        if not self.is_module:
            script_abs = os.path.realpath(self.script)

        # Wrap flush callback to track frame count
        original_on_flush = self.recorder._on_flush
        def counting_flush(events):
            original_on_flush(events)
            with self._frame_count_lock:
                self._frame_count += len(events)
        self.recorder._on_flush = counting_flush

        self.recorder.start(self._db_path, script_path=script_abs)
        self.session.state = "recording"
        self._recording = True
        self._recording_start = time.monotonic()

        self._recording_thread = threading.Thread(
            target=self._recording_thread_main,
            daemon=True,
        )
        self._recording_thread.start()

    def _recording_thread_main(self):
        import pyttd_native
        pyttd_native.set_recording_thread()
        # Apply launch env vars to the recording thread's environment
        for key, value in self._launch_env.items():
            if key == 'PYTTD_RECORDING':
                continue  # Don't let user override internal env var
            os.environ[key] = value
        error_info = None
        try:
            if self.is_module:
                self.runner.run_module(self.script, self.cwd, self._script_args)
            else:
                script_abs = os.path.realpath(self.script)
                self.runner.run_script(script_abs, self.cwd, self._script_args)
        except BaseException as e:
            import traceback
            error_info = {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
            }
        finally:
            try:
                stats = self.recorder.stop()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error("recorder.stop() failed: %s", e)
                stats = {}
            self._msg_queue.put({
                "type": "recording_complete",
                "stats": stats,
                "error": error_info,
            })
            try:
                os.write(self._wakeup_w, b'\x00')
            except OSError:
                pass
