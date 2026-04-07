"""Tests for Phase 3: variable modification while paused.

Tests run in subprocesses for clean monitoring state.
"""
import subprocess
import sys
import os
import pytest

PYTHON = sys.executable


def _run_test(code, tmp_path):
    script = os.path.join(str(tmp_path), "_set_var_test.py")
    with open(script, "w") as f:
        f.write(code)
    result = subprocess.run(
        [PYTHON, script],
        capture_output=True, text=True, timeout=30,
        cwd=str(tmp_path),
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Subprocess failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )
    return result.stdout.strip()


class TestSetVariable:
    def test_set_variable_paused(self, tmp_path):
        """Modify x=1 to x=99 while paused, verify set_variable returns correctly."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "target.py")
with open(script, "w") as f:
    f.write("import time\\nx = 1\\nfor _i in range(100):\\n    time.sleep(0.005)\\nresult = x * 2\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
done = threading.Event()
def run():
    pyttd_native.set_recording_thread()
    old_argv = sys.argv[:]
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
        rec.stop()
        done.set()
t = threading.Thread(target=run, daemon=True)
t.start()
for _ in range(50):
    time.sleep(0.02)
    if pyttd_native.get_sequence_counter() > 5:
        break

success = pyttd_native.request_pause()
assert success, "pause failed"

result = pyttd_native.set_variable("x", "99")
assert result is not None, "set_variable returned None"
assert "value" in result, f"no value in result: {{result}}"
assert "99" in result["value"], f"expected 99 in value: {{result}}"

pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_set_variable_type_change(self, tmp_path):
        """Modify x=1 to x='hello' — verify type change works."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "target.py")
with open(script, "w") as f:
    f.write("import time\\nx = 1\\nfor _i in range(100):\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
done = threading.Event()
def run():
    pyttd_native.set_recording_thread()
    old_argv = sys.argv[:]
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
        rec.stop()
        done.set()
t = threading.Thread(target=run, daemon=True)
t.start()
for _ in range(50):
    time.sleep(0.02)
    if pyttd_native.get_sequence_counter() > 5:
        break

success = pyttd_native.request_pause()
assert success
result = pyttd_native.set_variable("x", "'hello'")
assert result is not None
assert "'hello'" in result["value"], f"expected hello: {{result}}"
pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_set_variable_invalid_expression(self, tmp_path):
        """Invalid expression should raise, not crash."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "target.py")
with open(script, "w") as f:
    f.write("import time\\nx = 1\\nfor _i in range(100):\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
done = threading.Event()
def run():
    pyttd_native.set_recording_thread()
    old_argv = sys.argv[:]
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
        rec.stop()
        done.set()
t = threading.Thread(target=run, daemon=True)
t.start()
for _ in range(50):
    time.sleep(0.02)
    if pyttd_native.get_sequence_counter() > 5:
        break

success = pyttd_native.request_pause()
assert success
try:
    pyttd_native.set_variable("x", "not valid python!!!")
    print("FAIL")
except (SyntaxError, Exception):
    print("PASS")
pyttd_native.resume()
done.wait(timeout=10)
close_db()
''', tmp_path)
        assert output == "PASS"

    def test_set_variable_safe_builtins(self, tmp_path):
        """Dangerous expressions (import, exec, open) should be blocked."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "target.py")
with open(script, "w") as f:
    f.write("import time\\nx = 1\\nfor _i in range(100):\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
done = threading.Event()
def run():
    pyttd_native.set_recording_thread()
    old_argv = sys.argv[:]
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
        rec.stop()
        done.set()
t = threading.Thread(target=run, daemon=True)
t.start()
for _ in range(50):
    time.sleep(0.02)
    if pyttd_native.get_sequence_counter() > 5:
        break

success = pyttd_native.request_pause()
assert success
blocked = 0
for expr in ["__import__('os')", "exec('x=1')", "open('/etc/passwd')"]:
    try:
        pyttd_native.set_variable("x", expr)
    except Exception:
        blocked += 1
assert blocked == 3, f"expected 3 blocked, got {{blocked}}"
pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_set_variable_not_paused(self, tmp_path):
        """set_variable should fail when not paused."""
        output = _run_test('''
import pyttd_native
try:
    pyttd_native.set_variable("x", "1")
    print("FAIL: should have raised")
except RuntimeError as e:
    assert "not paused" in str(e)
    print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_set_variable_new_variable(self, tmp_path):
        """Setting a variable that doesn't exist yet should work."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "target.py")
with open(script, "w") as f:
    f.write("import time\\nx = 1\\nfor _i in range(100):\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
done = threading.Event()
def run():
    pyttd_native.set_recording_thread()
    old_argv = sys.argv[:]
    sys.argv = [script]
    try:
        runpy.run_path(script, run_name="__main__")
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
        rec.stop()
        done.set()
t = threading.Thread(target=run, daemon=True)
t.start()
for _ in range(50):
    time.sleep(0.02)
    if pyttd_native.get_sequence_counter() > 5:
        break

success = pyttd_native.request_pause()
assert success
result = pyttd_native.set_variable("new_var", "42")
assert result is not None
assert "42" in result["value"]
pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"
