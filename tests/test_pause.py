"""Tests for live debugging pause/resume functionality.

Tests the user pause mechanism: pausing the recording thread, snapshotting
the binlog into SQLite, navigating while paused, and resuming recording.

These tests run in subprocesses because the C extension's trace function
installation via PyEval_SetTrace is affected by sys.monitoring state left
by previous tests (conftest._reset_trace_state). A subprocess guarantees
clean monitoring state.
"""
import subprocess
import sys
import os
import pytest


PYTHON = sys.executable


def _run_pause_test(code, tmp_path):
    """Run a pause test in a subprocess. Raises on failure."""
    script = os.path.join(str(tmp_path), "_pause_test_runner.py")
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


class TestPauseBasic:
    def test_pause_and_resume(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nx = 0\\nfor i in range(200):\\n    x += i\\n    time.sleep(0.005)\\n")

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
    if pyttd_native.get_sequence_counter() > 10:
        break

success = pyttd_native.request_pause()
assert success, "request_pause should return True"
assert pyttd_native.is_paused(), "should be paused"
seq_at_pause = pyttd_native.get_sequence_counter()
assert seq_at_pause > 0

pyttd_native.resume()
assert not pyttd_native.is_paused()
done.wait(timeout=10)

final_seq = pyttd_native.get_sequence_counter()
assert final_seq > seq_at_pause, f"{{final_seq}} should > {{seq_at_pause}}"
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_pause_timeout_when_not_recording(self, tmp_path):
        output = _run_pause_test('''
import pyttd_native
result = pyttd_native.request_pause()
assert result == False
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_is_paused_default(self, tmp_path):
        output = _run_pause_test('''
import pyttd_native
assert pyttd_native.is_paused() == False
print("PASS")
''', tmp_path)
        assert output == "PASS"


class TestPauseBinlogSnapshot:
    def test_binlog_partial_load(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nx = 0\\nfor i in range(200):\\n    x += i\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
run_id = rec.run_id

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
    if pyttd_native.get_sequence_counter() > 10:
        break

success = pyttd_native.request_pause()
assert success
pyttd_native.flush_and_wait()
pyttd_native.binlog_flush()
pyttd_native.binlog_load_partial(db_path)

storage.connect_to_db(db_path)
storage.initialize_schema()
row = db.fetchone("SELECT COUNT(*) as cnt FROM executionframes WHERE run_id = ?", (str(run_id),))
assert row.cnt > 0, f"expected frames, got {{row.cnt}}"

user_line = db.fetchone(
    "SELECT * FROM executionframes WHERE run_id = ? AND frame_event = 'line' AND filename LIKE '%pause_test%' ORDER BY sequence_no LIMIT 1",
    (str(run_id),))
assert user_line is not None
assert "pause_test" in user_line.filename

pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_incremental_load(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nx = 0\\nfor i in range(200):\\n    x += i\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
run_id = rec.run_id

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
    if pyttd_native.get_sequence_counter() > 10:
        break

# First pause
success = pyttd_native.request_pause()
assert success
pyttd_native.flush_and_wait()
pyttd_native.binlog_flush()
pyttd_native.binlog_load_partial(db_path)

storage.connect_to_db(db_path)
storage.initialize_schema()
row1 = db.fetchone("SELECT COUNT(*) as cnt FROM executionframes WHERE run_id = ?", (str(run_id),))
count1 = row1.cnt

# Resume briefly
pyttd_native.resume()
time.sleep(0.15)

# Second pause
success = pyttd_native.request_pause()
assert success
pyttd_native.flush_and_wait()
pyttd_native.binlog_flush()
pyttd_native.binlog_load_partial(db_path)

row2 = db.fetchone("SELECT COUNT(*) as cnt FROM executionframes WHERE run_id = ?", (str(run_id),))
count2 = row2.cnt
assert count2 > count1, f"second load should have more: {{count2}} vs {{count1}}"

pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"


class TestPausedNavigation:
    def test_step_back_while_paused(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.session import Session
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nx = 0\\nfor i in range(200):\\n    x += i\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
run_id = rec.run_id

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
    if pyttd_native.get_sequence_counter() > 10:
        break

success = pyttd_native.request_pause()
assert success
pyttd_native.flush_and_wait()
pyttd_native.binlog_flush()
pyttd_native.binlog_load_partial(db_path)

storage.connect_to_db(db_path)
storage.initialize_schema()
session = Session()
first_line = db.fetchone(
    "SELECT * FROM executionframes WHERE run_id = ? AND frame_event = 'line' ORDER BY sequence_no LIMIT 1",
    (str(run_id),))
assert first_line is not None
paused_seq = pyttd_native.get_sequence_counter() - 1
session.enter_paused_replay(run_id, first_line.sequence_no)

last_line = db.fetchone(
    "SELECT * FROM executionframes WHERE run_id = ? AND frame_event = 'line' ORDER BY sequence_no DESC LIMIT 1",
    (str(run_id),))
if last_line and last_line.sequence_no != first_line.sequence_no:
    session.goto_frame(last_line.sequence_no)
    result = session.step_back()
    assert result["reason"] in ("step", "start")
    assert result["seq"] < last_line.sequence_no

pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_pause_boundary_blocks_forward(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.session import Session
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nx = 0\\nfor i in range(200):\\n    x += i\\n    time.sleep(0.005)\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)
config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)
run_id = rec.run_id

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
    if pyttd_native.get_sequence_counter() > 10:
        break

success = pyttd_native.request_pause()
assert success
pyttd_native.flush_and_wait()
pyttd_native.binlog_flush()
pyttd_native.binlog_load_partial(db_path)

storage.connect_to_db(db_path)
storage.initialize_schema()
session = Session()
first_line = db.fetchone(
    "SELECT * FROM executionframes WHERE run_id = ? AND frame_event = 'line' ORDER BY sequence_no LIMIT 1",
    (str(run_id),))
assert first_line is not None
session.enter_paused_replay(run_id, first_line.sequence_no)

last_line = db.fetchone(
    "SELECT * FROM executionframes WHERE run_id = ? AND frame_event = 'line' ORDER BY sequence_no DESC LIMIT 1",
    (str(run_id),))
if last_line:
    session.goto_frame(last_line.sequence_no)

result = session.step_into()
assert result["reason"] in ("pause_boundary", "end")

pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"


class TestPauseResumeRecording:
    def test_resume_continues_recording(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nx = 0\\nfor i in range(200):\\n    x += i\\n    time.sleep(0.005)\\n")
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
    if pyttd_native.get_sequence_counter() > 10:
        break

success = pyttd_native.request_pause()
assert success
seq_at_pause = pyttd_native.get_sequence_counter()

pyttd_native.resume()
done.wait(timeout=15)

storage.connect_to_db(db_path)
storage.initialize_schema()
row = db.fetchone(
    "SELECT COUNT(*) as cnt FROM executionframes WHERE run_id = ?",
    (str(rec.run_id),))
total = row.cnt
assert total > seq_at_pause, f"total {{total}} should > pause point {{seq_at_pause}}"
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_get_sequence_counter(self, tmp_path):
        output = _run_pause_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "pause_test.py")
with open(script, "w") as f:
    f.write("import time\\nfor i in range(200):\\n    x = i\\n    time.sleep(0.005)\\n")
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
    if pyttd_native.get_sequence_counter() > 10:
        break

seq = pyttd_native.get_sequence_counter()
assert isinstance(seq, int)
assert seq > 0

success = pyttd_native.request_pause()
assert success
seq2 = pyttd_native.get_sequence_counter()
assert seq2 >= seq

pyttd_native.resume()
done.wait(timeout=10)
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"
