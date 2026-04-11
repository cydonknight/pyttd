"""Tests for CLI output and diagnostics polish (Item 1).

Covers:
- _format_stats human-readable output
- --search flag for query subcommand
- --thread and --list-threads flags for query subcommand
- Standardized exit codes
"""
import subprocess
import sys

import pytest

from pyttd.cli import _format_stats, EXIT_OK, EXIT_USER_ERROR


# ---------------------------------------------------------------------------
# _format_stats tests
# ---------------------------------------------------------------------------

def test_format_stats_normal():
    stats = {
        'frame_count': 1000,
        'dropped_frames': 0,
        'elapsed_time': 2.0,
        'pool_overflows': 0,
        'checkpoint_count': 0,
        'checkpoint_memory_bytes': 0,
    }
    result = _format_stats(stats)
    assert "1,000 frames" in result
    assert "2.0s" in result
    assert "500" in result  # 500 frames/sec
    assert "WARNING" not in result
    assert "checkpoint" not in result.lower()


def test_format_stats_with_drops():
    stats = {
        'frame_count': 500,
        'dropped_frames': 50,
        'elapsed_time': 1.0,
        'pool_overflows': 0,
        'checkpoint_count': 0,
        'checkpoint_memory_bytes': 0,
    }
    result = _format_stats(stats)
    assert "WARNING" in result
    assert "50" in result
    assert "dropped" in result


def test_format_stats_with_pool_overflows():
    stats = {
        'frame_count': 200,
        'dropped_frames': 0,
        'elapsed_time': 1.0,
        'pool_overflows': 3,
        'checkpoint_count': 0,
        'checkpoint_memory_bytes': 0,
    }
    result = _format_stats(stats)
    assert "WARNING" in result
    assert "pool overflow" in result.lower()
    assert "truncated" in result


def test_format_stats_with_checkpoints():
    stats = {
        'frame_count': 2000,
        'dropped_frames': 0,
        'elapsed_time': 4.0,
        'pool_overflows': 0,
        'checkpoint_count': 5,
        'checkpoint_memory_bytes': 10 * 1024 * 1024,  # 10 MB
    }
    result = _format_stats(stats)
    assert "5 checkpoint(s)" in result
    assert "10.0 MB" in result


def test_format_stats_zero_elapsed():
    """No division by zero when elapsed_time is 0."""
    stats = {
        'frame_count': 100,
        'dropped_frames': 0,
        'elapsed_time': 0.0,
        'pool_overflows': 0,
        'checkpoint_count': 0,
        'checkpoint_memory_bytes': 0,
    }
    result = _format_stats(stats)
    assert "100 frames" in result
    assert "0 frames/sec" in result


def test_format_stats_missing_keys():
    """_format_stats handles empty dict gracefully."""
    result = _format_stats({})
    assert "0 frames" in result


# ---------------------------------------------------------------------------
# query --search tests
# ---------------------------------------------------------------------------

def test_search_frames(record_func):
    db_path, run_id, _stats = record_func("""
        def search_target():
            x = 1
            return x

        def other_func():
            return 2

        search_target()
        other_func()
    """)

    from pyttd.query import search_frames
    from pyttd.models import storage

    try:
        results = search_frames(run_id, 'search_target', limit=50)
        assert len(results) > 0
        names = [r.function_name for r in results]
        assert all(n == 'search_target' for n in names), f"Unexpected names: {names}"

        # Should not return other_func
        other = search_frames(run_id, 'other_func', limit=50)
        other_names = [r.function_name for r in other]
        assert all(n == 'other_func' for n in other_names)

        # Non-matching search returns empty list
        nothing = search_frames(run_id, 'zzz_no_match', limit=50)
        assert nothing == []
    finally:
        storage.close_db()


def test_search_frames_by_filename(record_func):
    db_path, run_id, _stats = record_func("""
        def fn():
            return 1
        fn()
    """)

    from pyttd.query import search_frames
    from pyttd.models import storage

    try:
        # The temp script is named test_script.py
        results = search_frames(run_id, 'test_script', limit=50)
        assert len(results) > 0
    finally:
        storage.close_db()


# ---------------------------------------------------------------------------
# query --thread / --list-threads tests
# ---------------------------------------------------------------------------

def test_get_frames_by_thread(record_func):
    db_path, run_id, _stats = record_func("""
        def work():
            return 42
        work()
    """)

    from pyttd.query import get_frames_by_thread
    from pyttd.models.db import db
    from pyttd.models import storage

    try:
        # Find the actual thread ID used in this run
        row = db.fetchone(
            "SELECT thread_id FROM executionframes WHERE run_id = ? LIMIT 1",
            (str(run_id),)
        )
        assert row is not None, "No frames found in DB"
        tid = row.thread_id

        frames = get_frames_by_thread(run_id, tid, limit=50)
        assert len(frames) > 0
        for f in frames:
            assert f.thread_id == tid

        # Non-existent thread returns empty list
        nothing = get_frames_by_thread(run_id, -999, limit=50)
        assert nothing == []
    finally:
        storage.close_db()


def test_list_threads(record_func):
    db_path, run_id, _stats = record_func("""
        def fn():
            return 1
        fn()
    """)

    from pyttd.models.db import db
    from pyttd.models import storage

    try:
        rows = db.fetchall(
            "SELECT thread_id, COUNT(*) as cnt FROM executionframes"
            " WHERE run_id = ? GROUP BY thread_id ORDER BY cnt DESC",
            (str(run_id),)
        )
        assert len(rows) >= 1
        for row in rows:
            assert isinstance(row.thread_id, int)
            assert row.cnt > 0
    finally:
        storage.close_db()


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

def test_exit_code_missing_script():
    """CLI exits with EXIT_USER_ERROR (1) when the script file doesn't exist."""
    result = subprocess.run(
        [sys.executable, '-m', 'pyttd', 'record', '/nonexistent/path/script.py'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_USER_ERROR
    assert "Error" in result.stderr


def test_exit_code_no_command():
    """CLI exits with 0 when no command is given (shows help)."""
    result = subprocess.run(
        [sys.executable, '-m', 'pyttd'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_exit_code_query_missing_db():
    """CLI exits with EXIT_USER_ERROR when querying a non-existent DB."""
    result = subprocess.run(
        [sys.executable, '-m', 'pyttd', 'query', '--db', '/nonexistent/path.pyttd.db'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_USER_ERROR


def test_exit_codes_defined():
    """Exit code constants have expected values."""
    assert EXIT_OK == 0
    assert EXIT_USER_ERROR == 1
