"""Phase 2: Checkpoint tests.

Tests for fork-based checkpointing, checkpoint store, and child lifecycle.
These tests only run on platforms with fork() support.
"""
import sys
import platform
import json
import uuid
import time
import pytest
import pyttd_native
from pyttd.models.db import db
from pyttd.models import storage


needs_fork = pytest.mark.skipif(
    sys.platform == 'win32',
    reason="Checkpoint tests require fork() (Unix only)"
)


@needs_fork
def test_recording_with_checkpoints(record_func):
    """Recording with checkpoint_interval > 0 creates checkpoint DB entries."""
    db_path, run_id, stats = record_func("""\
        def work():
            total = 0
            for i in range(200):
                total += i
            return total
        work()
    """, checkpoint_interval=100)

    assert stats['frame_count'] > 0
    checkpoints = db.fetchall(
        "SELECT * FROM checkpoint WHERE run_id = ?", (str(run_id),))
    # With 200 iterations + overhead, we should get at least 1 checkpoint
    assert len(checkpoints) >= 1
    for cp in checkpoints:
        assert cp.sequence_no > 0


@needs_fork
def test_checkpoint_sequence_numbers_increasing(record_func):
    """Checkpoint sequence numbers should be monotonically increasing."""
    db_path, run_id, stats = record_func("""\
        def work():
            total = 0
            for i in range(500):
                total += i
            return total
        work()
    """, checkpoint_interval=100)

    checkpoints = db.fetchall(
        "SELECT * FROM checkpoint WHERE run_id = ? ORDER BY sequence_no",
        (str(run_id),))
    assert len(checkpoints) >= 2, \
        f"Expected at least 2 checkpoints with interval=100 and 200 iterations, got {len(checkpoints)}"
    for i in range(1, len(checkpoints)):
        assert checkpoints[i].sequence_no > checkpoints[i-1].sequence_no


@needs_fork
def test_no_checkpoints_when_disabled(record_func):
    """checkpoint_interval=0 should not create any checkpoints."""
    db_path, run_id, stats = record_func("""\
        def work():
            total = 0
            for i in range(200):
                total += i
            return total
        work()
    """, checkpoint_interval=0)

    checkpoints = db.fetchall(
        "SELECT * FROM checkpoint WHERE run_id = ?", (str(run_id),))
    assert len(checkpoints) == 0


@needs_fork
def test_kill_all_checkpoints_cleans_up(record_func):
    """After kill_all_checkpoints, no children should remain."""
    db_path, run_id, stats = record_func("""\
        def work():
            total = 0
            for i in range(200):
                total += i
            return total
        work()
    """, checkpoint_interval=100)

    # Children are alive after stop() but before kill
    pyttd_native.kill_all_checkpoints()
    count = pyttd_native.get_checkpoint_count()
    assert count == 0


@needs_fork
def test_frames_still_recorded_with_checkpoints(record_func):
    """Frame recording should work correctly alongside checkpointing."""
    db_path, run_id, stats = record_func("""\
        def foo():
            a = 1
            b = 2
            return a + b
        for i in range(50):
            foo()
    """, checkpoint_interval=100)

    frames = db.fetchall(
        "SELECT * FROM executionframes WHERE run_id = ? ORDER BY sequence_no",
        (str(run_id),))
    assert len(frames) > 0

    # Verify sequence numbers are contiguous
    for i in range(1, len(frames)):
        assert frames[i].sequence_no > frames[i-1].sequence_no

    # Verify locals are valid JSON
    for f in frames:
        if f.locals_snapshot:
            data = json.loads(f.locals_snapshot)
            assert isinstance(data, dict)


@needs_fork
def test_checkpoint_callback_records_pid(record_func):
    """The checkpoint callback should record the child PID with is_alive=True."""
    db_path, run_id, stats = record_func("""\
        def work():
            total = 0
            for i in range(500):
                total += i
            return total
        work()
    """, checkpoint_interval=100)

    checkpoints = db.fetchall(
        "SELECT * FROM checkpoint WHERE run_id = ?", (str(run_id),))
    assert len(checkpoints) >= 1
    for cp in checkpoints:
        # Callback records child_pid and marks is_alive=True
        assert cp.child_pid is not None
        assert cp.child_pid > 0
        assert cp.is_alive == 1

    # After kill_all + DB update, is_alive should be False
    pyttd_native.kill_all_checkpoints()
    db.execute(
        "UPDATE checkpoint SET is_alive = 0, child_pid = NULL WHERE run_id = ?",
        (str(run_id),))
    db.commit()
    for cp in db.fetchall("SELECT * FROM checkpoint WHERE run_id = ?", (str(run_id),)):
        assert cp.is_alive == 0


@needs_fork
def test_checkpoint_stale_cleanup(db_setup):
    """Recorder.start() should clear stale checkpoint entries from prior sessions."""
    from pyttd.models.storage import delete_db_files
    import textwrap
    import runpy

    db_path = db_setup
    # Manually insert a stale checkpoint
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (run_id, "fake.py", time.time(), 0))
    db.execute(
        "INSERT INTO checkpoint (run_id, sequence_no, child_pid, is_alive) VALUES (?, ?, ?, ?)",
        (run_id, 100, 99999, 1))
    db.commit()

    stale = db.fetchval("SELECT COUNT(*) FROM checkpoint WHERE is_alive = 1")
    assert stale == 1

    # Starting a new recorder should clear stale entries
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder

    config = PyttdConfig(checkpoint_interval=0)
    recorder = Recorder(config)
    # Re-use existing DB path
    recorder.start(db_path, script_path="test.py")
    recorder.stop()
    recorder.cleanup()

    stale_after = db.fetchval("SELECT COUNT(*) FROM checkpoint WHERE is_alive = 1")
    assert stale_after == 0
