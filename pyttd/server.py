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
        self._paused = False
        self._paused_seq = None
        self._shutdown = False
        self._rpc = None
        self._conn = None
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

        # 4. Install signal handlers (only works in main thread)
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except ValueError:
            pass  # non-main thread — signals handled elsewhere

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

        # Store socket FD in C extension for checkpoint child handoff
        try:
            import pyttd_native
            pyttd_native.set_socket_fd(conn.fileno())
        except Exception:
            pass

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
        self._paused = False
        from pyttd.models.db import db
        from pyttd.models import storage

        # If exception, send traceback as stderr output
        error_info = msg.get("error")
        if error_info and self._rpc and not self._rpc.is_closed:
            self._rpc.send_notification("output", {
                "category": "stderr",
                "output": error_info.get("traceback", "")
            })

        # BUG-2 fix: Ensure DB connection is fresh and indexes are rebuilt.
        # After a pause/resume cycle, indexes may have been dropped and the
        # DB connection may be stale from the partial binlog load during pause.
        try:
            storage.close_db()
        except Exception:
            pass
        storage.connect_to_db(self._db_path)
        storage.initialize_schema()

        # Rebuild secondary indexes (may have been dropped during resume)
        import sqlite3 as _sqlite3
        from pyttd.models import schema
        try:
            conn = _sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode = wal")
            for sql in schema.SECONDARY_INDEX_CREATE:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
            conn.commit()
            conn.close()
        except Exception:
            pass

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
            "initialize": self._handle_backend_init,  # DAP standard alias
            "launch": self._handle_launch,
            "configuration_done": self._handle_configuration_done,
            "set_breakpoints": self._handle_set_breakpoints,
            "set_exception_breakpoints": self._handle_set_exception_breakpoints,
            "interrupt": self._handle_interrupt,
            "pause": self._handle_pause,
            "resume_recording": self._handle_resume_recording,
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
            "get_coroutine_suspensions": self._handle_get_coroutine_suspensions,
            "get_variable_children": self._handle_get_variable_children,
            "get_variable_history": self._handle_get_variable_history,
            "get_checkpoint_memory": self._handle_get_checkpoint_memory,
            "set_function_breakpoints": self._handle_set_function_breakpoints,
            "set_data_breakpoints": self._handle_set_data_breakpoints,
            "continue_from_past": self._handle_continue_from_past,
            "set_variable": self._handle_set_variable,
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
            # Only advertise live_pause when recording is possible (not replay-only)
            if hasattr(pyttd_native, 'request_pause') and not self._replay_db:
                capabilities.append("live_pause")
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
        source_path = params.get("source", {}).get("path", "")
        if source_path:
            source_path = os.path.realpath(source_path)
        breakpoints = params.get("breakpoints", [])
        if source_path:
            for bp in breakpoints:
                if "file" not in bp:
                    bp["file"] = source_path
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

    def _handle_pause(self, params: dict) -> dict:
        """Pause the recording thread and enter paused replay mode."""
        if not self._recording or self._paused:
            return {"error": "not recording or already paused"}

        import pyttd_native
        import sqlite3 as _sqlite3

        # 1. Pause recording thread (blocks until acked or timeout)
        success = pyttd_native.request_pause()
        if not success:
            return {"error": "pause timeout — recording thread did not respond within 10s. "
                    "For CPU-bound scripts, try --include to reduce recording scope."}

        # 2. Drain flush thread — wait for all ring buffer events to reach binlog
        pyttd_native.flush_and_wait()

        # 3. Flush binlog stdio buffer to disk
        pyttd_native.binlog_flush()

        # 4. Snapshot binlog → SQLite (incremental load)
        pyttd_native.binlog_load_partial(self._db_path)

        # 5. Rebuild secondary indexes for navigation queries
        from pyttd.models import schema
        try:
            conn = _sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode = wal")
            for sql in schema.SECONDARY_INDEX_CREATE:
                try:
                    conn.execute(sql)
                except Exception:
                    pass  # Index may already exist
            conn.commit()
            conn.close()
        except Exception:
            logger.debug("Failed to rebuild indexes during pause")

        # 6. Read current sequence and enter paused replay
        paused_seq = pyttd_native.get_sequence_counter()
        # sequence_counter is the NEXT seq to assign, so last recorded is seq-1
        if paused_seq > 0:
            paused_seq -= 1

        self._paused = True
        self._paused_seq = paused_seq
        self._recording = False

        # Find the paused frame's line event (may be the last event or earlier)
        from pyttd.models.db import db
        last_line = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no DESC LIMIT 1",
            (str(self.recorder.run_id),))
        first_line = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(self.recorder.run_id),))

        target_seq = last_line.sequence_no if last_line else 0
        first_seq = first_line.sequence_no if first_line else 0

        # enter_replay at first_seq (initial position), but set pause boundary
        # to target_seq (last recorded event) so forward nav works up to pause point
        self.session.enter_replay(self.recorder.run_id, first_seq)
        self.session._pause_boundary = target_seq
        # Navigate to the pause point
        if target_seq != first_seq:
            self.session.goto_frame(target_seq)

        total_frames = paused_seq + 1

        # 7. Notify DAP
        if self._rpc and not self._rpc.is_closed:
            self._rpc.send_notification("stopped", {
                "seq": target_seq,
                "reason": "pause",
                "totalFrames": total_frames,
                "thread_id": self.session.current_thread_id or 0,
            })

        return {"seq": target_seq, "totalFrames": total_frames}

    def _handle_resume_recording(self, params: dict) -> dict:
        """Resume the recording thread after a pause."""
        if not self._paused:
            return {"error": "not paused"}

        import pyttd_native
        import sqlite3 as _sqlite3

        # 1. Drop secondary indexes (restore insert-speed optimization)
        from pyttd.models import schema
        try:
            conn = _sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode = wal")
            for sql in schema.SECONDARY_INDEX_DROP:
                try:
                    conn.execute(sql)
                except Exception:
                    pass
            conn.commit()
            conn.close()
        except Exception:
            logger.debug("Failed to drop indexes during resume")

        # 2. Clear pause state
        self._paused = False
        self._paused_seq = None
        self._recording = True
        self.session.clear_pause_boundary()
        self.session.state = "recording"

        # 3. Resume recording thread
        pyttd_native.resume()

        return {}

    def _handle_continue_from_past(self, params: dict) -> dict:
        """Resume live execution from a historical checkpoint.
        Called when user presses Continue while navigated backward from pause."""
        import uuid as _uuid
        import pyttd_native
        from pyttd.models import schema
        from pyttd.models import storage

        target_seq = params.get("targetSeq", self.session.current_frame_seq)
        parent_run_id = str(self.recorder.run_id) if self.recorder.run_id else None

        # ---- Phase 1: Stop parent recording, flush binlog → SQLite ----
        # The child's checkpoint_child_go_live() will call binlog_open("wb")
        # which TRUNCATES the shared binlog file. Parent's events must land
        # in SQLite first or they are lost.
        parent_frame_count = 0
        if self._recording:
            try:
                pyttd_native.request_stop()
                if self._recording_thread:
                    self._recording_thread.join(timeout=5)
            except Exception:
                logger.exception("Failed to stop parent recording cleanly")
            try:
                stats = pyttd_native.get_recording_stats() or {}
                parent_frame_count = int(stats.get("frame_count", 0) or 0)
            except Exception:
                parent_frame_count = 0

        # ---- Phase 2: All parent DB writes, then close connection ----
        # Generate the branch run_id HERE so we can write the runs row and
        # close the DB BEFORE the child opens its own connection. This
        # eliminates the concurrent-writer WAL corruption.
        new_run_id = _uuid.uuid4().hex

        if parent_run_id is not None:
            try:
                schema.update_run(parent_run_id, total_frames=parent_frame_count)
            except Exception:
                logger.debug("Failed to update parent total_frames", exc_info=True)

        try:
            schema.create_run(
                run_id=new_run_id,
                script_path=self.script,
                parent_run_id=parent_run_id,
                branch_seq=target_seq,
            )
        except Exception:
            logger.exception("Failed to create branch run record")

        # Close parent's DB connection and release WAL lock so the child
        # can safely open its own connection without concurrent-writer races.
        try:
            storage.close_db()
        except Exception:
            pass

        # ---- Phase 3: Send RESUME_LIVE with parent-generated run_id ----
        # The C protocol now carries the 32-byte run_id so the child uses
        # the same UUID that's already in the runs table.
        try:
            result = pyttd_native.resume_live(target_seq, new_run_id)
        except RuntimeError as e:
            self._shutdown = True
            return {"error": str(e)}

        resumed_seq = result.get("seq", target_seq) if isinstance(result, dict) else target_seq

        # Verify child acknowledged correctly
        child_run_id = result.get("new_run_id") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("status") == "error":
            err_msg = result.get("error", "unknown")
            self._shutdown = True
            return {"error": f"resume_live: {err_msg}", "detail": result}

        # ---- Phase 4: Send handoff notification to DAP ----
        if self._rpc and not self._rpc.is_closed:
            self._rpc.send_notification("handoff", {
                "new_run_id": new_run_id,
                "seq": resumed_seq,
                "parent_run_id": parent_run_id,
                "branch_seq": target_seq,
            })

        # ---- Phase 5: Shut down parent ----
        self._shutdown = True
        return {
            "status": "handoff",
            "new_run_id": new_run_id,
            "seq": resumed_seq,
            "parent_run_id": parent_run_id,
            "branch_seq": target_seq,
        }

    def _handle_set_variable(self, params: dict) -> dict:
        """Modify a variable in the paused frame."""
        if not self._paused:
            return {"error": "not paused"}
        var_name = params.get("name", "")
        new_value = params.get("value", "")
        if not var_name:
            return {"error": "no variable name"}
        return self.session.set_variable(var_name, new_value)

    def _handle_get_threads(self, params: dict) -> dict:
        if self.session.state == "replay" and self.session.known_threads:
            return {"threads": self.session.get_threads()}
        return {"threads": [{"id": 1, "name": "Main Thread"}]}

    def _handle_get_stack_trace(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"stackFrames": []}
        seq = params.get("seq", self.session.current_frame_seq)
        frames = self.session.get_stack_at(seq)
        # Return both keys for DAP compat and backward compat
        return {"stackFrames": frames, "frames": frames}

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
            return {"error": "missing_targetSeq",
                    "message": "Required parameter 'targetSeq' not provided"}
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
            return {"error": "missing_frameSeq",
                    "message": "Required parameter 'frameSeq' not provided"}
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
        total_frames = (self.session.last_line_seq or 0) + 1 if self.session.last_line_seq is not None else 0
        return {"buckets": buckets, "totalFrames": total_frames}

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

    def _handle_get_coroutine_suspensions(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        return {
            "suspensions": self.session.get_coroutine_suspensions(
                params.get("call_seq", 0),
                params.get("return_seq", 0),
            )
        }

    def _handle_get_variable_children(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        seq = params.get("seq")
        var_name = params.get("variableName") or params.get("variable_name")
        if seq is not None and var_name is not None:
            return {"variables": self.session.get_variable_children_by_name(seq, var_name)}
        ref = params.get("variablesReference", 0)
        if ref == 0 and var_name and seq is None:
            return {"error": "missing_seq",
                    "message": "When using variableName, 'seq' is also required"}
        return {"variables": self.session.get_variable_children(ref)}

    def _handle_get_variable_history(self, params: dict) -> dict:
        if self.session.state != "replay":
            return {"error": "not_in_replay"}
        name = params.get("variableName") or params.get("variable_name", "")
        start_seq = params.get("startSeq", 0)
        # Default end to last_line_seq; fall back to a large value so the
        # query covers the full recording even if last_line_seq isn't set yet.
        end_seq = params.get("endSeq", self.session.last_line_seq or 2**63)
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

        try:
            storage.close_db()
        except Exception:
            pass
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

        # BUG-7: Delete stale WAL/SHM files from prior recordings to prevent
        # "database is locked" errors when re-recording to the same DB path.
        from pyttd.models.storage import delete_db_files
        delete_db_files(self._db_path)

        self.recorder._resume_live_callback = self._child_bootstrap_callback
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
            # Close the recording thread's thread-local DB connection.
            # recorder.stop() → schema.update_run opens a connection on THIS
            # thread; if left open, it holds a WAL lock that prevents the
            # checkpoint child from safely opening the DB after handoff.
            try:
                from pyttd.models import storage as _storage
                _storage.close_db()
            except Exception:
                pass
            self._msg_queue.put({
                "type": "recording_complete",
                "stats": stats,
                "error": error_info,
            })
            try:
                os.write(self._wakeup_w, b'\x00')
            except OSError:
                pass

    @staticmethod
    def _child_bootstrap_callback(new_run_id, db_path, socket_fd):
        """Called in the resumed checkpoint child after checkpoint_child_go_live().
        Starts a background RPC event loop on the inherited TCP socket.
        The main thread continues executing the user script."""
        import atexit
        import socket as _socket
        import selectors as _selectors
        from datetime import datetime as _datetime
        from pyttd.models import storage
        from pyttd.models.db import db
        from pyttd.models import schema
        from pyttd.protocol import JsonRpcConnection as _JsonRpcConnection
        from pyttd.session import Session as _Session

        # Reconnect to DB (child has stale connection from parent).
        # Do NOT call initialize_schema() — the parent already created the
        # schema, and the ALTER TABLE migrations open write transactions that
        # can race with the parent's final writes to the same WAL file.
        try:
            storage.close_db()
        except Exception:
            pass
        storage.connect_to_db(db_path)

        # Reconstruct socket from inherited FD
        child_sock = _socket.fromfd(socket_fd, _socket.AF_INET, _socket.SOCK_STREAM)

        # Create fresh RPC connection (empty buffer)
        rpc = _JsonRpcConnection(child_sock)

        # Create fresh session
        session = _Session()
        session.state = "recording"

        def _child_dispatch(msg):
            """Dispatch a single RPC in the child process."""
            method = msg.get("method")
            params = msg.get("params", {})
            request_id = msg.get("id")

            result = None
            try:
                if method == "get_threads":
                    if session.state == "replay" and session.known_threads:
                        result = {"threads": session.get_threads()}
                    else:
                        result = {"threads": [{"id": 1, "name": "Main Thread"}]}
                elif method == "get_stack_trace":
                    if session.state == "replay":
                        result = {"stackFrames": session.get_stack_at(
                            params.get("seq", session.current_frame_seq))}
                    else:
                        result = {"stackFrames": []}
                elif method == "get_scopes":
                    if session.state == "replay":
                        seq = params.get("seq", session.current_frame_seq)
                        result = {"scopes": [{"name": "Locals", "variablesReference": seq + 1}]}
                    else:
                        result = {"scopes": []}
                elif method == "get_variables":
                    if session.state == "replay":
                        seq = params.get("seq", session.current_frame_seq)
                        result = {"variables": session.get_variables_at(seq)}
                    else:
                        result = {"variables": []}
                elif method == "evaluate":
                    if session.state == "replay":
                        seq = params.get("seq", session.current_frame_seq)
                        result = session.evaluate_at(seq, params.get("expression", ""),
                                                     params.get("context", "hover"))
                    else:
                        result = {"result": "", "error": "not_in_replay"}
                elif method == "continue":
                    if session.state == "replay":
                        result = session.continue_forward()
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "next":
                    if session.state == "replay":
                        result = session.step_over()
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "step_in":
                    if session.state == "replay":
                        result = session.step_into()
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "step_out":
                    if session.state == "replay":
                        result = session.step_out()
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "step_back":
                    if session.state == "replay":
                        result = session.step_back()
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "reverse_continue":
                    if session.state == "replay":
                        result = session.reverse_continue()
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "goto_frame":
                    if session.state == "replay":
                        target = params.get("targetSeq", 0)
                        result = session.goto_frame(target)
                    else:
                        result = {"error": "not_in_replay"}
                elif method == "goto_targets":
                    if session.state == "replay":
                        result = session.goto_targets(
                            params.get("file", ""), params.get("line", 0))
                    else:
                        result = {"targets": []}
                elif method == "set_breakpoints":
                    session.set_breakpoints(params.get("breakpoints", []))
                    result = params.get("breakpoints", [])
                elif method == "set_exception_breakpoints":
                    session.set_exception_filters(params.get("filters", []))
                    result = {}
                elif method == "set_function_breakpoints":
                    session.set_function_breakpoints(params.get("breakpoints", []))
                    result = {}
                elif method == "interrupt":
                    import pyttd_native
                    pyttd_native.request_stop()
                    result = {}
                elif method == "disconnect":
                    result = {}
                elif method == "get_timeline_summary":
                    from pyttd.models.timeline import get_timeline_summary
                    result = get_timeline_summary(
                        str(session.run_id),
                        params.get("startSeq", 0),
                        params.get("endSeq", 0),
                        params.get("bucketCount", 500),
                        session.breakpoints)
                elif method == "backend_init":
                    import pyttd
                    result = {"version": pyttd.__version__, "capabilities": ["recording", "warm_navigation"]}
                else:
                    if request_id is not None:
                        rpc.send_error(request_id, -32601, f"Method not found: {method}")
                    return

                if request_id is not None:
                    rpc.send_response(request_id, result or {})
            except Exception as e:
                if request_id is not None:
                    rpc.send_error(request_id, -32603, str(e))

        # Start the event loop on a daemon thread
        def _child_event_loop():
            """Full RPC loop in the resumed child — handles all DAP queries
            while the user script runs on the main thread."""
            sel = _selectors.DefaultSelector()
            sel.register(child_sock, _selectors.EVENT_READ)
            try:
                while True:
                    try:
                        events = sel.select(timeout=1.0)
                    except Exception:
                        break
                    for key, mask in events:
                        try:
                            data = child_sock.recv(65536)
                            if not data:
                                return
                            rpc.feed(data)
                            while True:
                                try:
                                    msg = rpc.try_read_message()
                                except Exception:
                                    break
                                if msg is None:
                                    break
                                _child_dispatch(msg)
                        except (ConnectionResetError, BrokenPipeError, OSError):
                            return
            finally:
                sel.close()

        t = threading.Thread(target=_child_event_loop, daemon=True)
        t.start()

        # NOTE: Findings #14/#16 (terminal notification + total_frames update)
        # require the child to notify clients when it finishes the branched
        # run. This is best-effort: the child's atexit may not fire reliably
        # (e.g., SIGKILL from unrelated process, inherited broken pipes from
        # parent capture, or interpreter teardown ordering issues). The child
        # still records events via C binlog + flush, so the data is preserved
        # even without atexit — total_frames and notification are cosmetic.
        def _child_finalize():
            import pyttd_native as _native
            stats = {}
            try:
                stats = _native.stop_recording() or {}
            except Exception:
                pass
            frame_count = 0
            try:
                frame_count = int(stats.get("frame_count", 0) or 0)
            except Exception:
                pass
            try:
                schema.update_run(new_run_id,
                                  timestamp_end=_datetime.now().timestamp(),
                                  total_frames=frame_count)
            except Exception:
                pass
            try:
                if not rpc.is_closed:
                    rpc.send_notification("stopped", {
                        "reason": "recording_complete",
                        "totalFrames": frame_count,
                        "run_id": new_run_id,
                        "seq": frame_count,
                    })
            except Exception:
                pass
            try:
                child_sock.close()
            except Exception:
                pass

        atexit.register(_child_finalize)
