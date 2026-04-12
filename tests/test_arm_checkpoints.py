"""Tests for Issue 6: opt-in checkpoints in attach (arm) mode.

The default ``arm()`` keeps the historical behavior of force-disabling
checkpoints, because forking from a process whose pre-arm state pyttd
doesn't control is risky. ``arm(checkpoints=True)`` opts in. The C
recorder still gates the trigger on a "synthesized prefix" boundary so
the child has a real interpreter state to fast-forward into, and the
boundary is persisted in the ``runs.attach_safe_seq`` column so the
replay layer can refuse cold jumps before it.
"""
import os
import sys
import threading
import time

import pytest

import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import close_db, delete_db_files
from pyttd.replay import ReplayController


needs_fork = pytest.mark.skipif(
    sys.platform == 'win32',
    reason="checkpoints require fork() — Windows N/A"
)


@pytest.fixture
def attach_recorder(tmp_path):
    """Spin up a recorder in attach mode and clean up afterwards."""
    db_path = str(tmp_path / "arm.pyttd.db")
    delete_db_files(db_path)

    recorders = []

    def _start(checkpoint_interval):
        config = PyttdConfig(checkpoint_interval=checkpoint_interval)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(tmp_path / "armed_script.py"),
                       attach=True)
        pyttd_native.trace_current_frame()
        recorders.append(recorder)
        return recorder, db_path

    yield _start

    for recorder in recorders:
        # Use recorder.stop() (not pyttd_native.stop_recording() directly) so
        # the Python-level PYTTD_RECORDING env var is restored. The C-level
        # unsetenv() does not update Python's os.environ cache, which would
        # otherwise leak the env var into subsequent tests.
        try:
            if recorder._recording:
                recorder.stop()
        except Exception:
            pass
        try:
            pyttd_native.kill_all_checkpoints()
        except Exception:
            pass
        try:
            recorder.cleanup()
        except Exception:
            pass
    close_db()
    db.init(None)


@needs_fork
def test_arm_default_disables_checkpoints(attach_recorder):
    """arm() with default args should not create any checkpoints."""
    recorder, db_path = attach_recorder(checkpoint_interval=0)
    total = 0
    for i in range(2000):
        total += i * 2
    stats = recorder.stop()
    assert stats.get('checkpoint_count', 0) == 0


@needs_fork
def test_arm_checkpoints_opt_in_creates_checkpoints(attach_recorder):
    """arm(checkpoints=True equivalent — non-zero interval) creates checkpoints."""
    recorder, db_path = attach_recorder(checkpoint_interval=200)
    total = 0
    for i in range(2000):
        total += i * 2
    stats = recorder.stop()
    assert stats.get('checkpoint_count', 0) > 0, \
        f"expected checkpoints, got stats={stats}"


@needs_fork
def test_attach_safe_seq_recorded_in_stats(attach_recorder):
    """The synthesized-stack boundary should be exposed in stats."""
    recorder, db_path = attach_recorder(checkpoint_interval=200)
    total = 0
    for i in range(2000):
        total += i * 2
    stats = recorder.stop()
    # In attach mode at least one synthesized call event has been emitted
    # before the eval hook installed itself, so this should be > 0.
    assert stats.get('attach_safe_seq', 0) >= 1


@needs_fork
def test_attach_safe_seq_persisted_in_runs(attach_recorder):
    """recorder.stop() must write attach_safe_seq into the runs row."""
    recorder, db_path = attach_recorder(checkpoint_interval=200)
    total = 0
    for i in range(2000):
        total += i * 2
    stats = recorder.stop()  # writes the runs row
    assert stats.get('attach_safe_seq', 0) >= 1

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        row = db.fetchone(
            "SELECT attach_safe_seq FROM runs WHERE run_id = ?",
            (str(recorder.run_id),))
        assert row is not None
        assert row.attach_safe_seq is not None
        assert row.attach_safe_seq == stats['attach_safe_seq']
    finally:
        storage.close_db()


@needs_fork
def test_non_attach_recordings_have_null_attach_safe_seq(record_func):
    """Sanity: non-attach recordings should have attach_safe_seq = NULL."""
    db_path, run_id, stats = record_func("""\
        def f():
            return 42
        for _ in range(10):
            f()
    """, checkpoint_interval=100)
    # Stats from the parent's perspective: attach_safe_seq stays at 0
    # (the C global was reset on start_recording and never advanced).
    assert stats.get('attach_safe_seq', 0) == 0
    row = db.fetchone(
        "SELECT attach_safe_seq FROM runs WHERE run_id = ?",
        (str(run_id),))
    assert row is not None
    # The runs row has attach_safe_seq IS NULL because update_run() was
    # called without that column.
    assert row.attach_safe_seq is None


@needs_fork
def test_replay_refuses_cold_jump_inside_synth_prefix(attach_recorder, tmp_path):
    """ReplayController.goto_frame must serve targets < attach_safe_seq
    from SQLite (warm) regardless of whether checkpoints exist."""
    recorder, db_path = attach_recorder(checkpoint_interval=200)
    total = 0
    for i in range(2000):
        total += i * 2
    stats = recorder.stop()
    assert stats['attach_safe_seq'] >= 1
    safe = stats['attach_safe_seq']

    # The DB is still open from recorder.stop()
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        ctrl = ReplayController()
        # Pick a sequence_no inside the synth prefix that exists in the DB
        row = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND sequence_no < ?"
            " ORDER BY sequence_no LIMIT 1",
            (str(recorder.run_id), safe))
        if row is None:
            pytest.skip("no frames inside synthesized prefix")
        result = ctrl.goto_frame(recorder.run_id, row.sequence_no)
        assert result.get('warm_only') is True, \
            f"expected warm fallback, got {result}"
        assert result.get('error') is None
    finally:
        storage.close_db()


@needs_fork
def test_arm_with_background_thread_still_skips_checkpoints(tmp_path):
    """The Issue 5 multi-thread skip guard must still apply in attach
    mode, even with checkpoints opted in."""
    db_path = str(tmp_path / "arm_thread.pyttd.db")
    delete_db_files(db_path)

    stop_event = threading.Event()

    # Background worker repeatedly enters NEW user-code frames so the
    # eval hook fires for the worker thread (it only fires on frame
    # entry, so a single hot loop body won't be enough — we need calls).
    def helper(n):
        return n + 1

    def background():
        x = 0
        while not stop_event.is_set():
            for _ in range(50):
                x = helper(x)

    t = threading.Thread(target=background, daemon=True)
    t.start()
    try:
        # Wait until the worker is producing real events
        time.sleep(0.05)
        config = PyttdConfig(checkpoint_interval=100)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(tmp_path / "armed.py"),
                       attach=True)
        pyttd_native.trace_current_frame()
        try:
            for i in range(5000):
                _ = i * 3
        finally:
            stats = recorder.stop()
        # Either checkpoints didn't fire (skip counter > 0) or none were
        # created at all — the multi-thread guard prevents fork.
        skipped = stats.get('checkpoints_skipped_threads', 0)
        cp_count = stats.get('checkpoint_count', 0)
        assert skipped > 0 or cp_count == 0, (
            f"expected multi-thread skip in attach mode, "
            f"got skipped={skipped} cp_count={cp_count}"
        )
    finally:
        stop_event.set()
        t.join(timeout=1.0)
        try:
            pyttd_native.kill_all_checkpoints()
        except Exception:
            pass
        try:
            recorder.cleanup()
        except Exception:
            pass
        close_db()
        db.init(None)


def test_schema_migration_adds_attach_safe_seq(tmp_path):
    """Opening a DB created without attach_safe_seq must upgrade it."""
    import sqlite3

    db_path = str(tmp_path / "legacy.pyttd.db")
    # Create a legacy schema (pre-Issue 6) by hand
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            timestamp_start REAL,
            timestamp_end REAL,
            script_path TEXT,
            total_frames INTEGER DEFAULT 0,
            is_attach INTEGER DEFAULT 0
        )
    """)
    conn.execute("INSERT INTO runs (run_id, timestamp_start, total_frames)"
                 " VALUES ('legacy', 0.0, 0)")
    conn.commit()
    conn.close()

    # Now connect via storage — initialize_schema runs MIGRATION_SQL
    storage.connect_to_db(db_path)
    try:
        storage.initialize_schema()
        # Column should now exist
        row = db.fetchone("SELECT attach_safe_seq FROM runs WHERE run_id = 'legacy'")
        assert row is not None
        assert row.attach_safe_seq is None  # default for legacy rows
    finally:
        storage.close_db()
