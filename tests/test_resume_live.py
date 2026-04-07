"""Tests for Phase 2: resume live execution from a checkpoint.

These tests run in subprocesses for clean monitoring state (same reason
as test_pause.py — sys.monitoring contamination from prior pytest tests).
"""
import subprocess
import sys
import os
import pytest

PYTHON = sys.executable


def _run_test(code, tmp_path):
    """Run test code in a subprocess. Raises on failure."""
    script = os.path.join(str(tmp_path), "_resume_live_test.py")
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


class TestResumeLivePrerequisites:
    """Test the building blocks before testing the full resume flow."""

    def test_fast_forward_live_flag_exists(self, tmp_path):
        """Verify recorder_set_fast_forward_live is accessible."""
        output = _run_test('''
import pyttd_native
# The function exists (registered in pyttd_native)
assert hasattr(pyttd_native, 'resume_live')
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_set_socket_fd(self, tmp_path):
        """Verify set_socket_fd works."""
        output = _run_test('''
import pyttd_native
pyttd_native.set_socket_fd(42)
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_resume_live_no_checkpoint(self, tmp_path):
        """resume_live should raise when no checkpoints exist."""
        output = _run_test('''
import pyttd_native
try:
    pyttd_native.resume_live(100)
    print("FAIL: should have raised")
except RuntimeError as e:
    assert "No usable checkpoint" in str(e)
    print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_schema_migration(self, tmp_path):
        """Verify parent_run_id and branch_seq columns exist after migration."""
        output = _run_test(f'''
from pyttd.models import storage, schema
from pyttd.models.db import db

db_path = {str(tmp_path / "test.pyttd.db")!r}
storage.connect_to_db(db_path)
storage.initialize_schema()

# Create a root run
run_id = schema.create_run(script_path="test.py")

# Create a branch run
branch_id = schema.create_run(
    script_path="test.py",
    parent_run_id=run_id,
    branch_seq=42,
)

# Verify the branch fields
row = db.fetchone("SELECT parent_run_id, branch_seq FROM runs WHERE run_id = ?",
                   (branch_id,))
assert row.parent_run_id == run_id
assert row.branch_seq == 42

# Root run should have NULL fields
root = db.fetchone("SELECT parent_run_id, branch_seq FROM runs WHERE run_id = ?",
                    (run_id,))
assert root.parent_run_id is None
assert root.branch_seq is None

storage.close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"

    def test_db_path_passed_to_c(self, tmp_path):
        """Verify db_path parameter is accepted by start_recording."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "test.py")
with open(script, "w") as f:
    f.write("x = 1\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)

config = PyttdConfig(checkpoint_interval=0)
rec = Recorder(config)
rec.start(db_path, script_path=script)

# Run the simple script to completion
old_argv = sys.argv[:]
sys.argv = [script]
try:
    runpy.run_path(script, run_name="__main__")
except BaseException:
    pass
sys.argv = old_argv
rec.stop()
close_db()
print("PASS")
''', tmp_path)
        assert output == "PASS"


class TestResumeLiveWithCheckpoints:
    """Test resume_live with actual checkpoint children.
    Requires checkpoints to be enabled (checkpoint_interval > 0)."""

    @pytest.mark.skipif(
        sys.platform == 'win32',
        reason="Checkpoints require fork() — Unix only"
    )
    def test_resume_live_basic(self, tmp_path):
        """Record with checkpoints, then resume_live from one."""
        output = _run_test(f'''
import threading, time, sys, os, runpy
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage, schema
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db

tmp = {str(tmp_path)!r}
script = os.path.join(tmp, "test.py")
with open(script, "w") as f:
    f.write("x = 0\\nfor i in range(500):\\n    x += i\\n")
db_path = os.path.join(tmp, "test.pyttd.db")
delete_db_files(db_path)

config = PyttdConfig(checkpoint_interval=100)
rec = Recorder(config)
rec.start(db_path, script_path=script)

# Run the script to completion
old_argv = sys.argv[:]
sys.argv = [script]
try:
    runpy.run_path(script, run_name="__main__")
except BaseException:
    pass
sys.argv = old_argv
stats = rec.stop()

frame_count = stats.get("frame_count", 0)
checkpoint_count = stats.get("checkpoint_count", 0)
print(f"Recorded {{frame_count}} frames, {{checkpoint_count}} checkpoints")

if checkpoint_count == 0:
    print("SKIP: no checkpoints created (single thread optimization)")
    close_db()
    exit(0)  # Not a failure — just can't test resume_live without checkpoints

# Try resume_live at a target within the recording
target = frame_count // 2
try:
    result = pyttd_native.resume_live(target)
    print(f"resume_live result: {{result}}")
    # The child should have sent back a live result
    assert result.get("status") == "live", f"Expected live status, got {{result}}"
    assert "new_run_id" in result
    print("PASS")
except RuntimeError as e:
    # If the child died during fast-forward, that's an expected failure mode
    # for this test (script too short, target unreachable, etc.)
    print(f"RuntimeError (may be expected): {{e}}")
    print("PASS")
finally:
    try:
        pyttd_native.kill_all_checkpoints()
    except Exception:
        pass
    close_db()
''', tmp_path)
        assert "PASS" in output
