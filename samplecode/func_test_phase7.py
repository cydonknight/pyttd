"""Phase 7 functional test: exercises CLI improvements, protocol robustness,
PYTTD_RECORDING env var, serve --db replay mode, and end-to-end recording
with the full debug workflow as a user would experience it.
"""
import gc
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PYTHON = sys.executable
TIMEOUT = 15

passed = 0
failed = 0

def ok(msg):
    global passed; passed += 1; print(f"  [PASS] {msg}")
def fail(msg):
    global failed; failed += 1; print(f"  [FAIL] {msg}")
def check(cond, msg):
    ok(msg) if cond else fail(msg)


class RpcClient:
    """Minimal JSON-RPC client."""
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.pending = []
        self._id = 0

    def next_id(self):
        self._id += 1
        return self._id

    def _recv(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
            if data:
                self.buf += data
        except socket.timeout:
            pass
        messages = []
        while True:
            idx = self.buf.find(b"\r\n\r\n")
            if idx < 0:
                break
            header = self.buf[:idx].decode('ascii')
            cl = None
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    cl = int(line.split(":", 1)[1].strip())
            if cl is None:
                break
            body_start = idx + 4
            body_end = body_start + cl
            if len(self.buf) < body_end:
                break
            body = self.buf[body_start:body_end]
            self.buf = self.buf[body_end:]
            messages.append(json.loads(body))
        return messages

    def call(self, method, params=None, timeout=TIMEOUT):
        rid = self.next_id()
        msg = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params:
            msg["params"] = params
        body = json.dumps(msg).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('ascii')
        self.sock.sendall(header + body)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self._recv(timeout=max(0.2, deadline - time.monotonic()))
            for m in msgs:
                if "id" in m and m["id"] == rid:
                    return m.get("result", m.get("error", {}))
                else:
                    self.pending.append(m)
        raise TimeoutError(f"No response for {method} (id={rid})")

    def wait_notification(self, method, timeout=TIMEOUT):
        for i, n in enumerate(self.pending):
            if n.get("method") == method:
                return self.pending.pop(i).get("params", {})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self._recv(timeout=max(0.2, deadline - time.monotonic()))
            for m in msgs:
                if m.get("method") == method:
                    return m.get("params", {})
                self.pending.append(m)
        raise TimeoutError(f"No {method} notification within {timeout}s")


def start_server(args, timeout=TIMEOUT):
    """Start pyttd serve with given args. Returns (proc, port)."""
    cmd = [PYTHON, '-m', 'pyttd'] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    port_line = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        byte = proc.stdout.read(1)
        if not byte:
            stderr = proc.stderr.read()
            raise RuntimeError(f"Server died: {stderr}")
        port_line += byte
        if byte == b'\n':
            break
    import re
    match = re.match(rb'PYTTD_PORT:(\d+)', port_line.strip())
    if not match:
        proc.kill()
        raise RuntimeError(f"Bad port: {port_line!r}")
    return proc, int(match.group(1))


def connect(port, timeout=5.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(('127.0.0.1', port))
    return RpcClient(sock)


def kill_server(proc):
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)
    except:
        proc.kill()
        proc.wait()


# === Test 1: CLI --version ===
def test_cli_version():
    print("\n--- Test 1: CLI --version ---")
    import pyttd
    result = subprocess.run([PYTHON, "-m", "pyttd", "--version"],
                          capture_output=True, text=True)
    check(result.returncode == 0, f"--version exits 0")
    check(pyttd.__version__ in result.stdout, f"version {pyttd.__version__} in output")


# === Test 2: CLI --verbose ===
def test_cli_verbose():
    print("\n--- Test 2: CLI --verbose ---")
    result = subprocess.run([PYTHON, "-m", "pyttd", "--verbose", "--version"],
                          capture_output=True, text=True)
    check(result.returncode == 0, "--verbose accepted")


# === Test 3: CLI error handling ===
def test_cli_errors():
    print("\n--- Test 3: CLI error handling ---")
    # No command
    result = subprocess.run([PYTHON, "-m", "pyttd"], capture_output=True, text=True)
    check(result.returncode != 0, "no command -> nonzero exit")

    # Record nonexistent script
    result = subprocess.run([PYTHON, "-m", "pyttd", "record", "/nonexistent.py"],
                          capture_output=True, text=True)
    check(result.returncode != 0, "record nonexistent -> nonzero exit")
    check("not found" in result.stderr, "record nonexistent -> error message")

    # Serve nonexistent script
    result = subprocess.run([PYTHON, "-m", "pyttd", "serve", "--script", "/nonexistent.py"],
                          capture_output=True, text=True, timeout=5)
    check(result.returncode != 0, "serve nonexistent -> nonzero exit")

    # Serve mutually exclusive args
    result = subprocess.run([PYTHON, "-m", "pyttd", "serve", "--script", "x.py", "--db", "y.db"],
                          capture_output=True, text=True)
    check(result.returncode != 0, "serve --script --db -> conflict error")


# === Test 4: PYTTD_RECORDING env var ===
def test_recording_env_var():
    print("\n--- Test 4: PYTTD_RECORDING env var ---")
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.models import storage
    from pyttd.models.base import db
    from pyttd.models.frames import ExecutionFrames
    import pyttd_native

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "env_check.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                import os
                rec_var = os.environ.get('PYTTD_RECORDING', 'NOT_SET')
                result = f"recording={rec_var}"
            """))
        db_path = os.path.join(tmp, "env_check.pyttd.db")
        storage.delete_db_files(db_path)
        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=script)
        import runpy
        old_argv = sys.argv[:]
        sys.argv = [script]
        try:
            runpy.run_path(script, run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        stats = recorder.stop()
        run_id = recorder.run_id

        # Check that env var was '1' during recording
        frames = list(ExecutionFrames.select().where(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.frame_event == 'line')
        ).order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot:
                locals_data = json.loads(f.locals_snapshot)
                if 'rec_var' in locals_data:
                    check(locals_data['rec_var'] == "'1'",
                          f"PYTTD_RECORDING=1 during recording (got {locals_data['rec_var']})")
                    found = True
                    break
        if not found:
            fail("Could not find rec_var in frame locals")

        # After stop, env var should be cleared
        check(os.environ.get('PYTTD_RECORDING') is None,
              "PYTTD_RECORDING cleared after stop")

        pyttd_native.kill_all_checkpoints()
        storage.close_db()
        db.init(None)


# === Test 5: Full record + replay via server ===
def test_full_server_cycle():
    print("\n--- Test 5: Full server record+replay cycle ---")
    import pyttd as pyttd_pkg

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "server_test.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                import time
                import random

                def fibonacci(n):
                    if n <= 1:
                        return n
                    return fibonacci(n - 1) + fibonacci(n - 2)

                def greet(name):
                    return f"Hello, {name}!"

                result = fibonacci(6)
                names = ["Alice", "Bob"]
                for name in names:
                    greet(name)

                t = time.time()
                r = random.random()
            """))

        proc, port = start_server(['serve', '--script', script, '--cwd', tmp])
        try:
            rpc = connect(port)

            # Init
            resp = rpc.call("backend_init")
            check(resp.get("version") == pyttd_pkg.__version__,
                  f"backend_init version={resp.get('version')}")
            check("recording" in resp.get("capabilities", []),
                  "recording capability present")

            # Launch + record
            rpc.call("launch")
            rpc.call("set_breakpoints", {"breakpoints": []})
            rpc.call("configuration_done")

            stopped = rpc.wait_notification("stopped", timeout=TIMEOUT)
            total = stopped.get("totalFrames", 0)
            first_seq = stopped["seq"]
            check(total > 50, f"recorded {total} frames (fibonacci should generate many)")
            check(stopped["reason"] == "recording_complete", "reason=recording_complete")

            # Navigate forward
            seq = first_seq
            for _ in range(10):
                r = rpc.call("step_in")
                if r.get("reason") == "end":
                    break
                seq = r["seq"]
            check(seq > first_seq, f"stepped forward to seq={seq}")

            # Navigate backward
            for _ in range(5):
                r = rpc.call("step_back")
                if r.get("reason") == "start":
                    break
                seq = r["seq"]
            check(seq < total, "stepped backward successfully")

            # Get stack
            stack_resp = rpc.call("get_stack_trace", {"seq": seq})
            frames = stack_resp.get("frames", [])
            check(len(frames) >= 1, f"stack trace: {len(frames)} frames")

            # Get variables
            var_resp = rpc.call("get_variables", {"seq": seq})
            variables = var_resp.get("variables", [])
            check(isinstance(variables, list), f"got {len(variables)} variables")

            # Goto frame
            mid = total // 2
            r = rpc.call("goto_frame", {"target_seq": mid})
            check("seq" in r, f"goto_frame({mid}) -> seq={r.get('seq')}")

            # Timeline
            tl = rpc.call("get_timeline_summary", {
                "startSeq": 0, "endSeq": total, "bucketCount": 50
            })
            buckets = tl.get("buckets", [])
            check(len(buckets) > 0, f"timeline: {len(buckets)} buckets")

            # Phase 6 queries
            files = rpc.call("get_traced_files")
            traced = files.get("files", [])
            check(len(traced) >= 1, "get_traced_files")

            # Use the actual traced filename (may differ from script due to realpath)
            target_file = traced[0] if traced else script
            stats = rpc.call("get_execution_stats", {"filename": target_file})
            fns = stats.get("stats", [])
            fn_names = [f["functionName"] for f in fns]
            check("fibonacci" in fn_names, f"execution_stats has fibonacci: {fn_names}")

            children = rpc.call("get_call_children")
            check(len(children.get("children", [])) >= 1, "get_call_children root")

            # Evaluate
            eval_resp = rpc.call("evaluate", {
                "seq": first_seq, "expression": "result", "context": "hover"
            })
            # May or may not have 'result' visible at first_seq
            check("result" in eval_resp, "evaluate returned response")

            # Continue to end
            r = rpc.call("continue")
            while r.get("reason") != "end":
                r = rpc.call("continue")
            check(r["reason"] == "end", "continue to end")

            # Reverse continue (no breakpoints/exceptions)
            r = rpc.call("reverse_continue")
            check(r.get("reason") in ("start", "exception"),
                  f"reverse_continue: {r.get('reason')}")

            rpc.call("disconnect")
            ok("full server cycle complete")
        finally:
            kill_server(proc)


# === Test 6: Serve --db replay mode ===
def test_serve_db_replay():
    print("\n--- Test 6: Serve --db replay mode ---")
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.models import storage
    from pyttd.models.base import db
    import pyttd_native

    with tempfile.TemporaryDirectory() as tmp:
        # Step 1: Record a script
        script = os.path.join(tmp, "replay_target.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                def greet(name):
                    return f"Hello, {name}"
                greet("World")
                x = 42
            """))

        db_path = os.path.join(tmp, "replay_target.pyttd.db")
        storage.delete_db_files(db_path)
        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=script)
        import runpy
        old_argv = sys.argv[:]
        sys.argv = [script]
        try:
            runpy.run_path(script, run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        pyttd_native.kill_all_checkpoints()
        storage.close_db()
        db.init(None)

        # Step 2: Start server in --db mode
        proc, port = start_server(['serve', '--db', db_path])
        try:
            rpc = connect(port)

            # Init
            resp = rpc.call("backend_init")
            check("version" in resp, "backend_init in db mode")

            # Launch + configuration_done should trigger replay immediately
            rpc.call("launch", {"args": []})
            rpc.call("configuration_done")

            # Should get stopped notification
            stopped = rpc.wait_notification("stopped", timeout=5)
            check(stopped["reason"] == "recording_complete",
                  f"db mode: reason={stopped['reason']}")
            check(stopped.get("totalFrames", 0) > 0,
                  f"db mode: {stopped['totalFrames']} frames")

            # Navigate
            r = rpc.call("get_threads")
            check(len(r.get("threads", [])) == 1, "db mode: threads")

            # Step forward
            r = rpc.call("step_in")
            check("seq" in r, f"db mode: step_in -> seq={r.get('seq')}")

            # Step back
            r = rpc.call("step_back")
            check("seq" in r, f"db mode: step_back -> seq={r.get('seq')}")

            rpc.call("disconnect")
            ok("serve --db replay mode works")
        finally:
            kill_server(proc)


# === Test 7: Protocol robustness ===
def test_protocol_robustness():
    print("\n--- Test 7: Protocol robustness ---")
    from pyttd.protocol import JsonRpcConnection, MAX_HEADER_ACCUMULATION
    import pytest

    # Normal message
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    body = json.dumps({"jsonrpc": "2.0", "method": "test"}).encode()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
    conn.feed(header + body)
    msg = conn.try_read_message()
    check(msg is not None and msg["method"] == "test", "normal message parses")
    check(not conn.is_closed, "normal message: not closed")
    s1.close(); s2.close()

    # Header accumulation overflow
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    chunk = b"X" * 4096
    for _ in range(MAX_HEADER_ACCUMULATION // 4096 + 1):
        conn.feed(chunk)
    try:
        conn.try_read_message()
        fail("should raise on accumulation overflow")
    except ValueError as e:
        check("accumulation limit" in str(e), "accumulation limit error")
    check(conn.is_closed, "closed after accumulation overflow")
    s1.close(); s2.close()

    # Non-ASCII header
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    conn.feed(b"\xff\xfe\r\n\r\n")
    try:
        conn.try_read_message()
        fail("should raise on non-ASCII")
    except ValueError as e:
        check("Non-ASCII" in str(e), "non-ASCII header error")
    check(conn.is_closed, "closed after non-ASCII")
    s1.close(); s2.close()

    # Negative content-length
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    conn.feed(b"Content-Length: -1\r\n\r\n")
    try:
        conn.try_read_message()
        fail("should raise on negative CL")
    except ValueError as e:
        check("Negative" in str(e), "negative CL error")
    check(conn.is_closed, "closed after negative CL")
    s1.close(); s2.close()

    # Oversized content-length
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    conn.feed(b"Content-Length: 999999999\r\n\r\n")
    try:
        conn.try_read_message()
        fail("should raise on oversized CL")
    except ValueError as e:
        check("too large" in str(e), "oversized CL error")
    check(conn.is_closed, "closed after oversized CL")
    s1.close(); s2.close()

    # Multiple messages in one feed
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    msgs_data = b""
    for i in range(3):
        body = json.dumps({"jsonrpc": "2.0", "method": f"m{i}"}).encode()
        msgs_data += f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    conn.feed(msgs_data)
    parsed = []
    while True:
        m = conn.try_read_message()
        if m is None:
            break
        parsed.append(m)
    check(len(parsed) == 3, f"parsed {len(parsed)} messages from single feed")
    check(parsed[0]["method"] == "m0" and parsed[2]["method"] == "m2",
          "messages in correct order")
    s1.close(); s2.close()

    # EOF (empty data)
    s1, s2 = socket.socketpair()
    conn = JsonRpcConnection(s2)
    conn.feed(b"")
    check(conn.is_closed, "EOF sets closed")
    s1.close(); s2.close()


# === Test 8: Complex script with exceptions, I/O hooks, deep calls ===
def test_complex_recording():
    print("\n--- Test 8: Complex script recording + navigation ---")
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.session import Session
    from pyttd.models import storage
    from pyttd.models.base import db
    from pyttd.models.frames import ExecutionFrames
    from pyttd.models.io_events import IOEvent
    import pyttd_native

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "complex.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                import time
                import random

                def outer():
                    def inner(x):
                        if x == 0:
                            raise ValueError("zero!")
                        return x * 2

                    results = []
                    for i in range(5):
                        try:
                            results.append(inner(i))
                        except ValueError:
                            results.append(-1)
                    return results

                # Deeply nested
                def a(): return b()
                def b(): return c()
                def c(): return d()
                def d(): return "deep"

                # Main
                output = outer()
                deep = a()
                t = time.time()
                r = random.random()
            """))

        db_path = os.path.join(tmp, "complex.pyttd.db")
        storage.delete_db_files(db_path)
        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        runner = Runner()
        recorder.start(db_path, script_path=script)
        try:
            runner.run_script(script, tmp)
        except BaseException:
            pass
        stats = recorder.stop()
        run_id = recorder.run_id
        check(stats.get('frame_count', 0) > 50,
              f"complex script: {stats.get('frame_count')} frames")
        check(stats.get('dropped_frames', 0) == 0, "no dropped frames")

        # Check I/O events were recorded
        io_count = IOEvent.select().where(IOEvent.run_id == run_id).count()
        check(io_count >= 2, f"IO events: {io_count} (time.time + random.random)")

        # Navigate with Session
        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Step through several frames
        for _ in range(20):
            r = session.step_into()
            if r.get("reason") == "end":
                break
        check(session.current_frame_seq > first_line.sequence_no,
              "stepped forward in complex script")

        # Find exception frames
        exception_frames = list(ExecutionFrames.select().where(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.frame_event == 'exception')
        ))
        check(len(exception_frames) >= 1,
              f"exception events: {len(exception_frames)}")

        # Exception unwind frames
        unwind_frames = list(ExecutionFrames.select().where(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.frame_event == 'exception_unwind')
        ))
        check(len(unwind_frames) >= 1,
              f"exception_unwind events: {len(unwind_frames)}")

        # Step back
        r = session.step_back()
        check(r.get("reason") != "error", "step_back works")

        # Goto frame
        mid_seq = stats['frame_count'] // 2
        r = session.goto_frame(mid_seq)
        check("seq" in r, f"goto_frame({mid_seq}) succeeded")

        # Verify stack at deep call
        deep_calls = list(ExecutionFrames.select().where(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'd') &
            (ExecutionFrames.frame_event == 'line')
        ))
        if deep_calls:
            stack = session.get_stack_at(deep_calls[0].sequence_no)
            names = [f['name'] for f in stack]
            check('d' in names, f"deep stack: {names}")
            check(len(stack) >= 4, f"deep stack depth: {len(stack)}")

        # Verify locals JSON is valid everywhere
        all_with_locals = list(ExecutionFrames.select().where(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.locals_snapshot.is_null(False)) &
            (ExecutionFrames.locals_snapshot != '')
        ).limit(100))
        bad_json = 0
        for f in all_with_locals:
            try:
                json.loads(f.locals_snapshot)
            except json.JSONDecodeError:
                bad_json += 1
                if bad_json == 1:
                    print(f"    Bad JSON at seq={f.sequence_no}: {f.locals_snapshot[:100]}")
        check(bad_json == 0, f"all locals valid JSON ({len(all_with_locals)} checked)")

        pyttd_native.kill_all_checkpoints()
        storage.close_db()
        db.init(None)


# === Test 9: Record + query CLI workflow ===
def test_cli_record_query():
    print("\n--- Test 9: Record + query CLI workflow ---")
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "cli_target.py")
        with open(script, 'w') as f:
            f.write("x = 42\ny = x + 1\nprint(f'result={y}')\n")

        # Record
        result = subprocess.run(
            [PYTHON, "-m", "pyttd", "record", script],
            capture_output=True, text=True, cwd=tmp, timeout=10
        )
        check(result.returncode == 0, f"record exit 0 (stderr: {result.stderr[:100]})")
        check("Recording complete" in result.stdout, "recording complete message")

        # Query last run
        db_path = os.path.join(tmp, "cli_target.pyttd.db")
        result = subprocess.run(
            [PYTHON, "-m", "pyttd", "query", "--last-run", "--db", db_path],
            capture_output=True, text=True, timeout=5
        )
        check(result.returncode == 0, "query --last-run exits 0")
        check("frames" in result.stdout.lower(), f"query shows frames: {result.stdout[:100]}")

        # Query with --frames
        result = subprocess.run(
            [PYTHON, "-m", "pyttd", "query", "--last-run", "--frames", "--db", db_path],
            capture_output=True, text=True, timeout=5
        )
        check(result.returncode == 0, "query --frames exits 0")
        check("line" in result.stdout.lower(), "query shows line events")


# === Test 10: Rapid record/replay cycles (resource leak check) ===
def test_rapid_cycles():
    print("\n--- Test 10: Rapid record/replay cycles ---")
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.session import Session
    from pyttd.models import storage
    from pyttd.models.base import db
    from pyttd.models.frames import ExecutionFrames
    import pyttd_native

    gc.collect()
    initial_fds = _count_fds()

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(5):
            script = os.path.join(tmp, f"cycle_{i}.py")
            with open(script, 'w') as f:
                f.write(f"x = {i}\ny = x * 2\n")
            db_path = os.path.join(tmp, f"cycle_{i}.pyttd.db")
            storage.delete_db_files(db_path)

            config = PyttdConfig(checkpoint_interval=0)
            recorder = Recorder(config)
            runner = Runner()
            recorder.start(db_path, script_path=script)
            try:
                runner.run_script(script, tmp)
            except BaseException:
                pass
            stats = recorder.stop()
            run_id = recorder.run_id

            # Brief navigation
            session = Session()
            first = (ExecutionFrames.select()
                .where((ExecutionFrames.run_id == run_id) &
                       (ExecutionFrames.frame_event == 'line'))
                .order_by(ExecutionFrames.sequence_no).first())
            if first:
                session.enter_replay(run_id, first.sequence_no)
                session.step_into()
                session.step_back()

            recorder.cleanup()
            db.init(None)

    gc.collect()
    final_fds = _count_fds()
    leaked = final_fds - initial_fds
    check(leaked <= 2, f"FD leak check: {leaked} fds leaked (initial={initial_fds}, final={final_fds})")


def _count_fds():
    count = 0
    for fd in range(1024):
        try:
            os.fstat(fd)
            count += 1
        except OSError:
            pass
    return count


# === Test 11: Unicode and special characters in locals ===
def test_unicode_locals():
    print("\n--- Test 11: Unicode and special chars in locals ---")
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.models import storage
    from pyttd.models.base import db
    from pyttd.models.frames import ExecutionFrames
    import pyttd_native

    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "unicode.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                emoji = "Hello \\U0001f30d"
                japanese = "\\u3053\\u3093\\u306b\\u3061\\u306f"
                backslash = 'path\\\\to\\\\file "quoted"'
                newlines = "line1\\nline2\\ttab"
                done = True
            """))

        db_path = os.path.join(tmp, "unicode.pyttd.db")
        storage.delete_db_files(db_path)
        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=script)
        import runpy
        old_argv = sys.argv[:]
        sys.argv = [script]
        try:
            runpy.run_path(script, run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        run_id = recorder.run_id

        # Check all locals are valid JSON
        frames = list(ExecutionFrames.select().where(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.frame_event == 'line') &
            (ExecutionFrames.locals_snapshot.is_null(False))
        ))
        bad = 0
        for f in frames:
            if f.locals_snapshot:
                try:
                    json.loads(f.locals_snapshot)
                except json.JSONDecodeError:
                    bad += 1
                    if bad == 1:
                        print(f"    Bad JSON at seq={f.sequence_no}: {f.locals_snapshot[:200]}")
        check(bad == 0, f"all unicode locals valid JSON ({len(frames)} frames)")

        # Check specific values exist
        last_frame = frames[-1] if frames else None
        if last_frame and last_frame.locals_snapshot:
            data = json.loads(last_frame.locals_snapshot)
            keys = list(data.keys())
            check('emoji' in keys, f"emoji var recorded: {keys}")
            check('japanese' in keys, f"japanese var recorded")
            check('backslash' in keys, f"backslash var recorded")

        pyttd_native.kill_all_checkpoints()
        storage.close_db()
        db.init(None)


# === Test 12: Breakpoint navigation ===
def test_breakpoint_navigation():
    print("\n--- Test 12: Breakpoint navigation ---")
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "bp_target.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                def work():
                    a = 1
                    b = 2
                    c = 3
                    d = 4
                    return a + b + c + d
                for i in range(3):
                    work()
            """))

        proc, port = start_server(['serve', '--script', script, '--cwd', tmp])
        try:
            rpc = connect(port)
            rpc.call("backend_init")
            rpc.call("launch")

            # Set breakpoint on line 4 (c = 3)
            rpc.call("set_breakpoints", {
                "breakpoints": [{"file": script, "line": 4}]
            })
            rpc.call("configuration_done")

            stopped = rpc.wait_notification("stopped", timeout=TIMEOUT)
            check(stopped["reason"] == "recording_complete", "recording done")

            # Continue to first breakpoint
            r = rpc.call("continue")
            check(r.get("reason") == "breakpoint",
                  f"hit breakpoint: reason={r.get('reason')}")

            bp_seq = r["seq"]
            # Get variables at breakpoint
            vars_resp = rpc.call("get_variables", {"seq": bp_seq})
            var_names = [v["name"] for v in vars_resp.get("variables", [])]
            check("a" in var_names and "b" in var_names,
                  f"vars at breakpoint: {var_names}")

            # Continue to next breakpoint
            r = rpc.call("continue")
            check(r.get("reason") == "breakpoint", "second breakpoint hit")

            # Reverse continue should find the first breakpoint
            r = rpc.call("reverse_continue")
            check(r.get("reason") in ("breakpoint", "start"),
                  f"reverse_continue: {r.get('reason')}")

            rpc.call("disconnect")
            ok("breakpoint navigation works")
        finally:
            kill_server(proc)


# === Test 13: Exception breakpoints ===
def test_exception_breakpoints():
    print("\n--- Test 13: Exception breakpoints ---")
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "exc_target.py")
        with open(script, 'w') as f:
            f.write(textwrap.dedent("""\
                def risky(x):
                    if x == 0:
                        raise ValueError("zero")
                    return 1 / x

                for i in [5, 3, 0, 2]:
                    try:
                        risky(i)
                    except ValueError:
                        pass
            """))

        proc, port = start_server(['serve', '--script', script, '--cwd', tmp])
        try:
            rpc = connect(port)
            rpc.call("backend_init")
            rpc.call("launch")
            rpc.call("set_exception_breakpoints", {"filters": ["raised"]})
            rpc.call("configuration_done")

            stopped = rpc.wait_notification("stopped", timeout=TIMEOUT)

            # Continue should stop on exception
            r = rpc.call("continue")
            check(r.get("reason") == "exception",
                  f"exception breakpoint: reason={r.get('reason')}")

            # Variables should show the exception context
            vars_resp = rpc.call("get_variables", {"seq": r["seq"]})
            check(len(vars_resp.get("variables", [])) >= 1, "vars at exception")

            rpc.call("disconnect")
            ok("exception breakpoints work")
        finally:
            kill_server(proc)


if __name__ == "__main__":
    print("=" * 60)
    print("PHASE 7 FUNCTIONAL TESTS")
    print("=" * 60)

    test_cli_version()
    test_cli_verbose()
    test_cli_errors()
    test_recording_env_var()
    test_full_server_cycle()
    test_serve_db_replay()
    test_protocol_robustness()
    test_complex_recording()
    test_cli_record_query()
    test_rapid_cycles()
    test_unicode_locals()
    test_breakpoint_navigation()
    test_exception_breakpoints()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)
