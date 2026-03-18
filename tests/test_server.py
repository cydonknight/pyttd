"""Phase 3: Server integration tests.

Start the pyttd server as a subprocess, send JSON-RPC requests via TCP,
verify responses and the full recording → replay flow.
"""
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import pytest


PYTHON = sys.executable
TIMEOUT = 15


class RpcClient:
    """Minimal JSON-RPC client for testing."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = b""
        self._next_id = 1

    def send_request(self, method: str, params: dict | None = None) -> int:
        rid = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        body = json.dumps(msg).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('ascii')
        self._sock.sendall(header + body)
        return rid

    def read_message(self, timeout: float = 5.0) -> dict:
        self._sock.settimeout(timeout)
        while True:
            # Try to parse from buffer
            header_end = self._buffer.find(b"\r\n\r\n")
            if header_end >= 0:
                header = self._buffer[:header_end].decode('ascii')
                content_length = None
                for line in header.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                if content_length is not None:
                    body_start = header_end + 4
                    body_end = body_start + content_length
                    if len(self._buffer) >= body_end:
                        body = self._buffer[body_start:body_end]
                        self._buffer = self._buffer[body_end:]
                        return json.loads(body)
            # Need more data
            data = self._sock.recv(4096)
            if not data:
                raise ConnectionError("Server closed connection")
            self._buffer += data

    def send_and_receive(self, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
        rid = self.send_request(method, params)
        # Read messages until we get our response
        while True:
            msg = self.read_message(timeout=timeout)
            if msg.get("id") == rid:
                return msg
            # Otherwise it's a notification — keep reading

    def read_notification(self, method: str | None = None, timeout: float = 10.0) -> dict:
        """Read the next notification, optionally filtering by method."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timeout waiting for notification {method}")
            msg = self.read_message(timeout=remaining)
            if "method" in msg and not "id" in msg:
                if method is None or msg["method"] == method:
                    return msg


def _start_server(script_content: str, tmp_path, extra_args=None):
    """Write a test script and start the server. Returns (process, port, script_path)."""
    script_path = str(tmp_path / "test_script.py")
    with open(script_path, 'w') as f:
        f.write(textwrap.dedent(script_content))

    cmd = [PYTHON, '-m', 'pyttd', 'serve', '--script', script_path, '--cwd', str(tmp_path)]
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )

    # Read port from stdout
    port_line = b""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        byte = proc.stdout.read(1)
        if not byte:
            stderr_out = proc.stderr.read()
            raise RuntimeError(f"Server exited early. stderr: {stderr_out}")
        port_line += byte
        if byte == b'\n':
            break

    match = re.match(rb'^PYTTD_PORT:(\d+)', port_line.strip())
    if not match:
        proc.kill()
        raise RuntimeError(f"Bad port line: {port_line!r}")

    port = int(match.group(1))
    return proc, port, script_path


def _connect(port: int, timeout: float = 5.0) -> RpcClient:
    """Connect to the server and return an RPC client."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(('127.0.0.1', port))
    return RpcClient(sock)


@pytest.fixture
def server_session(tmp_path):
    """Fixture that starts a server with a simple script and provides an RPC client.
    Cleans up on teardown."""
    procs = []
    clients = []

    def _start(script_content, extra_args=None):
        proc, port, script_path = _start_server(script_content, tmp_path, extra_args)
        procs.append(proc)
        client = _connect(port)
        clients.append(client)
        return client, script_path

    yield _start

    for client in clients:
        try:
            client.send_request("disconnect")
            time.sleep(0.1)
        except Exception:
            pass
        try:
            client._sock.close()
        except Exception:
            pass
    for proc in procs:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _run_to_replay(client: RpcClient, script_path: str):
    """Run the standard init sequence through recording completion.
    Returns the 'stopped' notification params."""
    resp = client.send_and_receive("backend_init")
    assert "result" in resp
    import pyttd
    assert resp["result"]["version"] == pyttd.__version__

    resp = client.send_and_receive("launch", {"args": []})
    assert "result" in resp

    resp = client.send_and_receive("configuration_done")
    assert "result" in resp

    # Wait for recording to complete
    stopped = client.read_notification("stopped", timeout=TIMEOUT)
    return stopped["params"]


class TestServerBasic:
    def test_backend_init(self, server_session):
        import pyttd
        client, script_path = server_session("x = 1\n")
        resp = client.send_and_receive("backend_init")
        assert resp["result"]["version"] == pyttd.__version__
        assert "recording" in resp["result"]["capabilities"]
        assert "warm_navigation" in resp["result"]["capabilities"]

    def test_full_record_replay_cycle(self, server_session):
        client, script_path = server_session("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        stopped = _run_to_replay(client, script_path)
        assert stopped["reason"] == "recording_complete"
        assert stopped["totalFrames"] > 0
        assert stopped["seq"] >= 0

    def test_get_threads(self, server_session):
        client, script_path = server_session("x = 1\n")
        _run_to_replay(client, script_path)
        resp = client.send_and_receive("get_threads")
        threads = resp["result"]["threads"]
        assert len(threads) == 1
        assert isinstance(threads[0]["id"], int)
        assert threads[0]["name"] == "Main Thread"

    def test_get_stack_trace(self, server_session):
        client, script_path = server_session("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        stopped = _run_to_replay(client, script_path)
        resp = client.send_and_receive("get_stack_trace", {"seq": stopped["seq"]})
        frames = resp["result"]["frames"]
        assert len(frames) >= 1
        assert "name" in frames[0]
        assert "file" in frames[0]
        assert "line" in frames[0]

    def test_get_variables(self, server_session):
        client, script_path = server_session("""\
            def foo():
                x = 42
                y = "hello"
                return x
            foo()
        """)
        # Use breakpoint to navigate directly to user code
        resp = client.send_and_receive("backend_init")
        resp = client.send_and_receive("launch", {"args": []})
        # Breakpoint on line 3 (y = "hello") — x is already set
        client.send_and_receive("set_breakpoints", {
            "breakpoints": [{"file": script_path, "line": 3}]
        })
        resp = client.send_and_receive("configuration_done")
        stopped = client.read_notification("stopped", timeout=TIMEOUT)

        resp = client.send_and_receive("continue")
        result = resp["result"]
        assert result["reason"] == "breakpoint", f"Expected breakpoint, got {result}"
        seq = result["seq"]

        resp = client.send_and_receive("get_variables", {"seq": seq})
        variables = resp["result"]["variables"]
        names = [v["name"] for v in variables]
        assert "x" in names, f"Expected 'x' in variables, got {names}"
        x_var = next(v for v in variables if v["name"] == "x")
        assert x_var["value"] == "42"

    def test_get_scopes(self, server_session):
        client, script_path = server_session("x = 1\n")
        stopped = _run_to_replay(client, script_path)
        resp = client.send_and_receive("get_scopes", {"seq": stopped["seq"]})
        scopes = resp["result"]["scopes"]
        assert len(scopes) == 1
        assert scopes[0]["name"] == "Locals"
        assert scopes[0]["variablesReference"] == stopped["seq"] + 1


class TestNavigation:
    def test_step_into(self, server_session):
        client, script_path = server_session("""\
            def foo():
                x = 1
                y = 2
                return x + y
            foo()
        """)
        stopped = _run_to_replay(client, script_path)
        initial_seq = stopped["seq"]

        resp = client.send_and_receive("step_in")
        result = resp["result"]
        assert result["seq"] > initial_seq or result["reason"] == "end"

    def test_step_over(self, server_session):
        client, script_path = server_session("""\
            def foo():
                x = 1
                return x
            def bar():
                a = foo()
                return a
            bar()
        """)
        stopped = _run_to_replay(client, script_path)
        resp = client.send_and_receive("next")
        result = resp["result"]
        assert "seq" in result

    def test_step_out(self, server_session):
        client, script_path = server_session("""\
            def inner():
                x = 1
                return x
            def outer():
                y = inner()
                return y
            outer()
        """)
        # Use breakpoint to navigate directly inside inner
        resp = client.send_and_receive("backend_init")
        resp = client.send_and_receive("launch", {"args": []})
        # Breakpoint on line 2 (x = 1) inside inner
        client.send_and_receive("set_breakpoints", {
            "breakpoints": [{"file": script_path, "line": 2}]
        })
        resp = client.send_and_receive("configuration_done")
        stopped = client.read_notification("stopped", timeout=TIMEOUT)

        resp = client.send_and_receive("continue")
        result = resp["result"]
        assert result["reason"] == "breakpoint", f"Expected breakpoint, got {result}"

        # Step out from inner
        resp = client.send_and_receive("step_out")
        result = resp["result"]
        assert "seq" in result
        assert result["reason"] in ("step", "end")

    def test_continue_to_end(self, server_session):
        client, script_path = server_session("""\
            x = 1
            y = 2
            z = 3
        """)
        _run_to_replay(client, script_path)
        resp = client.send_and_receive("continue")
        result = resp["result"]
        assert result["reason"] == "end"

    def test_continue_with_breakpoint(self, server_session):
        client, script_path = server_session("""\
            def foo():
                a = 1
                b = 2
                c = 3
                return a + b + c
            foo()
        """)
        # Set breakpoint before init
        resp = client.send_and_receive("backend_init")
        resp = client.send_and_receive("launch", {"args": []})
        client.send_and_receive("set_breakpoints", {
            "breakpoints": [{"file": script_path, "line": 4}]
        })
        resp = client.send_and_receive("configuration_done")
        stopped = client.read_notification("stopped", timeout=TIMEOUT)

        resp = client.send_and_receive("continue")
        result = resp["result"]
        # Should stop at breakpoint or reach end
        assert result["reason"] in ("breakpoint", "end")


class TestEvaluate:
    def test_evaluate_hover(self, server_session):
        client, script_path = server_session("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        # Use breakpoint to navigate to where x is set
        resp = client.send_and_receive("backend_init")
        resp = client.send_and_receive("launch", {"args": []})
        # Breakpoint on line 3 (return x) — x is already set
        client.send_and_receive("set_breakpoints", {
            "breakpoints": [{"file": script_path, "line": 3}]
        })
        resp = client.send_and_receive("configuration_done")
        stopped = client.read_notification("stopped", timeout=TIMEOUT)

        resp = client.send_and_receive("continue")
        result = resp["result"]
        assert result["reason"] == "breakpoint", f"Expected breakpoint, got {result}"
        seq = result["seq"]

        eval_resp = client.send_and_receive("evaluate", {
            "seq": seq, "expression": "x", "context": "hover"
        })
        assert eval_resp["result"]["result"] == "42"

    def test_evaluate_repl(self, server_session):
        client, script_path = server_session("x = 1\n")
        _run_to_replay(client, script_path)
        resp = client.send_and_receive("evaluate", {
            "seq": 0, "expression": "x", "context": "repl"
        })
        assert "Replay mode" in resp["result"]["result"]


class TestOutputCapture:
    def test_stdout_captured(self, server_session):
        client, script_path = server_session("""\
            print("hello from script")
        """)
        resp = client.send_and_receive("backend_init")
        resp = client.send_and_receive("launch", {"args": []})
        resp = client.send_and_receive("configuration_done")

        # Collect notifications until we get stopped
        outputs = []
        deadline = time.monotonic() + TIMEOUT
        got_stopped = False
        while time.monotonic() < deadline:
            try:
                msg = client.read_message(timeout=1.0)
            except (TimeoutError, socket.timeout):
                continue
            if msg.get("method") == "output" and msg.get("params", {}).get("category") == "stdout":
                outputs.append(msg["params"]["output"])
            if msg.get("method") == "stopped":
                got_stopped = True
                break

        assert got_stopped
        combined = "".join(outputs)
        assert "hello from script" in combined


class TestDisconnect:
    def test_disconnect_clean(self, server_session):
        client, script_path = server_session("x = 1\n")
        _run_to_replay(client, script_path)
        resp = client.send_and_receive("disconnect")
        assert "result" in resp


class TestErrorHandling:
    def test_unknown_method(self, server_session):
        client, script_path = server_session("x = 1\n")
        _run_to_replay(client, script_path)
        resp = client.send_and_receive("nonexistent_method")
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_script_exception_doesnt_crash(self, server_session):
        client, script_path = server_session("""\
            raise ValueError("intentional error")
        """)
        stopped = _run_to_replay(client, script_path)
        # Server should still be alive and in replay mode
        assert stopped["reason"] == "recording_complete"
        resp = client.send_and_receive("get_threads")
        assert "result" in resp
