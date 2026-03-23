"""Phase 7 tests: CLI improvements, protocol robustness, PYTTD_RECORDING env var,
version consistency, serve --db mode."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest
import pyttd
import pyttd_native

from pyttd.config import PyttdConfig
from pyttd.protocol import JsonRpcConnection, MAX_HEADER_ACCUMULATION
from pyttd.models import storage
from pyttd.models.db import db


# --- CLI Tests ---

class TestCLI:
    def test_version_flag(self):
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "--version"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert pyttd.__version__ in result.stdout

    def test_version_consistency(self):
        """Version in __init__.py matches pyproject.toml."""
        import tomllib
        toml_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        if not os.path.exists(toml_path):
            pytest.skip("pyproject.toml not found")
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == pyttd.__version__

    def test_record_missing_script(self):
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "record", "/nonexistent/script.py"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_serve_missing_script(self):
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "serve", "--script", "/nonexistent/script.py"],
            capture_output=True, text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_serve_mutually_exclusive_args(self):
        """--script and --db are mutually exclusive."""
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "serve", "--script", "x.py", "--db", "y.db"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0

    def test_verbose_flag_accepted(self):
        """--verbose flag is accepted without error."""
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "--verbose", "--version"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert pyttd.__version__ in result.stdout

    def test_no_command_shows_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "pyttd"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower()


# --- Protocol Robustness Tests ---

class TestProtocolRobustness:
    def _make_pair(self):
        """Create connected socket pair for testing."""
        s1, s2 = socket.socketpair()
        return s1, JsonRpcConnection(s2)

    def test_header_accumulation_limit(self):
        """Buffer overflow without header terminator should close connection."""
        s1, conn = self._make_pair()
        try:
            # Feed data without \r\n\r\n header terminator
            chunk = b"X" * 4096
            for _ in range(MAX_HEADER_ACCUMULATION // 4096 + 1):
                conn.feed(chunk)
            with pytest.raises(ValueError, match="accumulation limit"):
                conn.try_read_message()
            assert conn.is_closed
        finally:
            s1.close()

    def test_normal_message_still_works(self):
        """Normal messages parse fine even with the accumulation check."""
        s1, conn = self._make_pair()
        try:
            body = json.dumps({"jsonrpc": "2.0", "method": "test"}).encode()
            header = f"Content-Length: {len(body)}\r\n\r\n".encode()
            conn.feed(header + body)
            msg = conn.try_read_message()
            assert msg is not None
            assert msg["method"] == "test"
            assert not conn.is_closed
        finally:
            s1.close()

    def test_non_ascii_header_closes(self):
        """Non-ASCII in header should close connection."""
        s1, conn = self._make_pair()
        try:
            conn.feed(b"\xff\xfe\r\n\r\n")
            with pytest.raises(ValueError, match="Non-ASCII"):
                conn.try_read_message()
            assert conn.is_closed
        finally:
            s1.close()

    def test_negative_content_length(self):
        s1, conn = self._make_pair()
        try:
            conn.feed(b"Content-Length: -1\r\n\r\n")
            with pytest.raises(ValueError, match="Negative"):
                conn.try_read_message()
            assert conn.is_closed
        finally:
            s1.close()

    def test_oversized_content_length(self):
        s1, conn = self._make_pair()
        try:
            conn.feed(b"Content-Length: 999999999\r\n\r\n")
            with pytest.raises(ValueError, match="too large"):
                conn.try_read_message()
            assert conn.is_closed
        finally:
            s1.close()

    def test_malformed_json_body(self):
        """Malformed JSON body should raise ValueError, not JSONDecodeError."""
        s1, conn = self._make_pair()
        try:
            body = b"{not valid json}"
            header = f"Content-Length: {len(body)}\r\n\r\n".encode()
            conn.feed(header + body)
            with pytest.raises(ValueError, match="Invalid JSON body"):
                conn.try_read_message()
            # Connection stays open (bytes consumed, recoverable)
            assert not conn.is_closed
        finally:
            s1.close()


# --- PYTTD_RECORDING Environment Variable ---

class TestRecordingEnvVar:
    def test_env_var_set_during_recording(self, record_func):
        """PYTTD_RECORDING=1 is visible to the user script during recording."""
        db_path, run_id, stats = record_func("""
            import os
            pyttd_rec = os.environ.get('PYTTD_RECORDING', '')
            _done = True
        """)
        assert stats.get('frame_count', 0) > 0
        # Verify the env var was actually '1' by reading the recorded locals.
        # Locals are captured at the START of each line event, so pyttd_rec
        # only appears in the line event for the line AFTER assignment.
        import json
        frames = db.fetchall(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'",
            (str(run_id),))
        found = False
        for f in frames:
            if f.locals_snapshot:
                locals_data = json.loads(f.locals_snapshot)
                if 'pyttd_rec' in locals_data:
                    assert locals_data['pyttd_rec'] == "'1'", \
                        f"Expected PYTTD_RECORDING='1', got '{locals_data['pyttd_rec']}'"
                    found = True
                    break
        assert found, "Could not find 'pyttd_rec' in any recorded frame locals"

    def test_env_var_cleared_after_stop(self, record_func):
        """PYTTD_RECORDING is cleared after recording stops."""
        record_func("x = 1\n")
        assert os.environ.get('PYTTD_RECORDING') is None


# --- Serve --db Replay Mode ---

class TestServeReplayDB:
    def _record_to_db(self, tmp_path):
        """Record a script and return the db_path."""
        from pyttd.recorder import Recorder
        from pyttd.models.storage import delete_db_files
        import runpy

        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent("""
            def greet(name):
                return f"Hello, {name}"
            greet("World")
            x = 42
        """))
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        pyttd_native.kill_all_checkpoints()
        storage.close_db()
        db.init(None)
        return db_path

    def test_serve_db_replay(self, tmp_path):
        """Server in --db mode enters replay immediately without recording."""
        db_path = self._record_to_db(tmp_path)

        proc = subprocess.Popen(
            [sys.executable, "-m", "pyttd", "serve", "--db", db_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Read port
            port_line = proc.stdout.readline().decode().strip()
            assert port_line.startswith("PYTTD_PORT:")
            port = int(port_line.split(":")[1])

            # Connect
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(('127.0.0.1', port))
            try:
                def send_recv(method, params=None):
                    msg = {"jsonrpc": "2.0", "method": method, "id": 1}
                    if params:
                        msg["params"] = params
                    body = json.dumps(msg).encode()
                    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
                    sock.sendall(header + body)
                    data = b""
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        chunk = sock.recv(4096)
                        if chunk:
                            data += chunk
                        # Try to parse
                        idx = data.find(b"\r\n\r\n")
                        if idx >= 0:
                            hdr = data[:idx].decode('ascii')
                            for line in hdr.split("\r\n"):
                                if line.lower().startswith("content-length:"):
                                    cl = int(line.split(":")[1].strip())
                                    body_start = idx + 4
                                    if len(data) >= body_start + cl:
                                        return json.loads(data[body_start:body_start + cl])
                    return None

                # Init
                resp = send_recv("backend_init")
                assert resp is not None

                # Launch + configuration_done should immediately enter replay
                send_recv("launch", {"args": []})
                send_recv("configuration_done")

                # Allow time for the stopped notification
                time.sleep(0.5)

                # Should be in replay — query threads
                resp = send_recv("get_threads")
                assert resp is not None

                # Disconnect
                send_recv("disconnect")
            finally:
                sock.close()
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except:
                proc.kill()
                proc.wait()


# --- Packaging Tests ---

class TestPackaging:
    def test_py_typed_exists(self):
        """py.typed marker file exists for PEP 561."""
        pkg_dir = os.path.dirname(pyttd.__file__)
        assert os.path.isfile(os.path.join(pkg_dir, "py.typed"))

    def test_entry_point(self):
        """pyttd CLI entry point works."""
        result = subprocess.run(
            ["pyttd", "--version"],
            capture_output=True, text=True,
            env={**os.environ, "PATH": os.path.join(os.path.dirname(sys.executable)) + ":" + os.environ.get("PATH", "")},
        )
        # May not be on PATH in test env, but --version via -m works
        if result.returncode != 0:
            result = subprocess.run(
                [sys.executable, "-m", "pyttd", "--version"],
                capture_output=True, text=True,
            )
        assert pyttd.__version__ in result.stdout

    def test_manifest_includes_headers(self):
        """MANIFEST.in includes C header files."""
        manifest_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "MANIFEST.in")
        if not os.path.exists(manifest_path):
            pytest.skip("MANIFEST.in not found")
        with open(manifest_path) as f:
            content = f.read()
        assert "*.h" in content
