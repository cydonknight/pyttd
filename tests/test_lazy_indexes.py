"""Tests for lazy secondary index build (Fix 1 from PLAN).

Recorder.stop() no longer eagerly builds secondary indexes. Indexes are
built on demand by storage.ensure_secondary_indexes(), called from CLI
read paths (query, replay, diff, export).
"""
import os
import subprocess
import sys
import time
import pytest
from pyttd.models import storage, schema
from pyttd.models.db import db
from pyttd.models.storage import (
    ensure_secondary_indexes,
    close_db,
    delete_db_files,
)


_EXPECTED_INDEXES = {
    'executionframes_run_id_filename_line_no',
    'executionframes_run_id_function_name',
    'executionframes_run_id_frame_event_sequence_no',
    'executionframes_run_id_call_depth_sequence_no',
    'executionframes_run_id_thread_id_sequence_no',
}


def _list_indexes(db_path: str) -> set:
    """Return the set of indexes currently on executionframes."""
    import sqlite3 as _sq
    conn = _sq.connect(db_path)
    try:
        rows = conn.execute("PRAGMA index_list(executionframes)").fetchall()
        return {r[1] for r in rows}  # name is col 1
    finally:
        conn.close()


class TestStopLeavesIndexesAbsent:
    """Recorder.stop() must not rebuild secondary indexes anymore."""

    def test_indexes_absent_after_stop(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    x = 1
    return x
f()
""")
        present = _list_indexes(db_path)
        # None of the 5 secondary indexes should be present after stop()
        leaked = _EXPECTED_INDEXES & present
        assert leaked == set(), (
            f"Recorder.stop() should no longer build secondary indexes, "
            f"but found: {leaked}")


class TestEnsureBuilds:
    """ensure_secondary_indexes() builds indexes when missing, is idempotent."""

    def test_builds_when_missing(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    return 1
f()
""")
        # Confirm indexes are absent pre-build
        assert _list_indexes(db_path) & _EXPECTED_INDEXES == set()

        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            built = ensure_secondary_indexes(quiet=True)
            assert built is True
            present = {r.name for r in db.fetchall(
                "PRAGMA index_list(executionframes)")}
            assert _EXPECTED_INDEXES.issubset(present)
        finally:
            close_db()

    def test_idempotent_when_present(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    return 1
f()
""")
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            # First call builds
            assert ensure_secondary_indexes(quiet=True) is True
            # Second call is a no-op and fast
            t0 = time.perf_counter()
            built = ensure_secondary_indexes(quiet=True)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert built is False
            # Loose bound: the PRAGMA probe alone should be <50ms even
            # on slow CI hardware. Typically <1ms.
            assert elapsed_ms < 50, (
                f"Idempotent path too slow: {elapsed_ms:.1f}ms")
        finally:
            close_db()


class TestCliQueryBuildsIndexes:
    """pyttd query ends up building indexes via ensure_secondary_indexes()."""

    def test_query_frames_builds_indexes(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    x = 1
    y = 2
    return x + y
f()
""")
        # Confirm indexes absent before query
        assert _list_indexes(db_path) & _EXPECTED_INDEXES == set()

        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "query",
             "--last-run", "--frames", "--limit", "5",
             "--db", db_path],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0

        # After query, indexes should now be present
        present = _list_indexes(db_path)
        assert _EXPECTED_INDEXES.issubset(present), (
            f"Expected indexes missing after pyttd query: "
            f"{_EXPECTED_INDEXES - present}")

    def test_query_list_runs_does_not_build(self, record_func):
        """--list-runs only queries the runs table; shouldn't trigger build."""
        db_path, run_id, _ = record_func("""
x = 1
""")
        assert _list_indexes(db_path) & _EXPECTED_INDEXES == set()

        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "query",
             "--list-runs", "--db", db_path],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0

        # --list-runs returns before reaching ensure_secondary_indexes()
        present = _list_indexes(db_path)
        leaked = _EXPECTED_INDEXES & present
        assert leaked == set(), (
            f"--list-runs should not trigger index build; found: {leaked}")


class TestCliReplayBuildsIndexes:
    """pyttd replay --goto-frame ends up with indexes present."""

    def test_replay_builds_indexes(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    return 1
f()
""")
        assert _list_indexes(db_path) & _EXPECTED_INDEXES == set()

        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--goto-frame", "2", "--db", db_path,
             "--run-id", str(run_id)[:8]],
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0

        present = _list_indexes(db_path)
        assert _EXPECTED_INDEXES.issubset(present)


class TestRecordExitSpeed:
    """pyttd record should exit faster than before the fix.

    This is a soft regression test: we just verify it completes quickly
    for a small workload. The benchmark-level improvement is covered by
    benchmarks/ rather than the test suite.
    """

    def test_record_completes(self, tmp_path):
        script = tmp_path / "t.py"
        script.write_text("""
def f():
    x = 0
    for i in range(100):
        x += i
    return x
f()
""")
        t0 = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "record", str(script)],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path))
        elapsed = time.perf_counter() - t0
        assert result.returncode == 0
        # Very loose bound — if this ever fails, the regression is huge
        assert elapsed < 10.0, f"pyttd record too slow: {elapsed:.2f}s"

        # And indexes must be absent after record (lazy: only built on
        # subsequent query)
        db_path = str(tmp_path / "t.pyttd.db")
        assert _list_indexes(db_path) & _EXPECTED_INDEXES == set()


class TestGracefulFailure:
    """ensure_secondary_indexes returns False on failure rather than raising."""

    def test_missing_table_returns_false(self, tmp_path):
        """If executionframes doesn't exist, ensure should not crash."""
        db_path = str(tmp_path / "empty.pyttd.db")
        # Just create a DB without any tables
        import sqlite3 as _sq
        conn = _sq.connect(db_path)
        conn.close()

        storage.connect_to_db(db_path)
        try:
            # No executionframes table — expect False and no exception.
            # PRAGMA index_list returns empty rows for missing table,
            # CREATE INDEX will fail, we catch and return False.
            built = ensure_secondary_indexes(quiet=True)
            assert built is False
        finally:
            close_db()


class TestServerPathStillBuilds:
    """Server paths (recording_stopped, request_pause) build indexes themselves."""

    def test_server_code_still_calls_create_index(self):
        """Smoke test: verify the server code still has its own rebuild
        block. The plan explicitly keeps these in place since interactive
        DAP needs indexes immediately."""
        import pyttd.server as server
        src = open(server.__file__).read()
        # Both pause and recording-stopped paths should still mention
        # SECONDARY_INDEX_CREATE
        assert src.count('SECONDARY_INDEX_CREATE') >= 2, (
            "Server paths should still rebuild indexes synchronously "
            "so interactive replay stepping isn't slow.")
