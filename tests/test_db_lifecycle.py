"""Tests for database lifecycle management (multi-run, eviction, clean, etc.)."""
import os
import textwrap
import time
import uuid
import pytest

from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import (
    compute_db_path, delete_db_files, evict_old_runs,
    _evict_old_runs_internal,
)
from pyttd.recorder import Recorder
from pyttd.query import get_all_runs, get_run_by_id


# --- Sub-task 1: compute_db_path ---

class TestComputeDbPath:
    def test_script_path(self, tmp_path):
        script = str(tmp_path / "my_script.py")
        result = compute_db_path(script)
        assert result == str(tmp_path / "my_script.pyttd.db")

    def test_module_name(self, tmp_path):
        result = compute_db_path("pkg.mod", is_module=True, cwd=str(tmp_path))
        assert result == str(tmp_path / "pkg_mod.pyttd.db")

    def test_explicit_path(self, tmp_path):
        explicit = str(tmp_path / "custom.pyttd.db")
        result = compute_db_path("ignored.py", explicit_path=explicit)
        assert result == explicit

    def test_explicit_path_overrides_script(self, tmp_path):
        explicit = str(tmp_path / "override.pyttd.db")
        result = compute_db_path("anything.py", is_module=True, cwd="/some/dir",
                                 explicit_path=explicit)
        assert result == explicit

    def test_script_in_subdir(self, tmp_path):
        subdir = tmp_path / "sub"
        subdir.mkdir()
        script = str(subdir / "app.py")
        result = compute_db_path(script)
        assert result == str(subdir / "app.pyttd.db")


# --- Sub-task 2: Multi-run append mode ---

class TestMultiRunAppend:
    def test_two_recordings_same_db(self, tmp_path):
        """Recording twice to the same DB should accumulate 2 runs."""
        import pyttd_native
        from pyttd.recorder import Recorder

        script = tmp_path / "s.py"
        script.write_text("x = 1\n")
        db_path = str(tmp_path / "test.pyttd.db")

        for _ in range(2):
            config = PyttdConfig(checkpoint_interval=0)
            rec = Recorder(config)
            rec.start(db_path, script_path=str(script))
            import runpy, sys
            old_argv = sys.argv[:]
            sys.argv = [str(script)]
            try:
                runpy.run_path(str(script), run_name='__main__')
            except BaseException:
                pass
            finally:
                sys.argv = old_argv
            rec.stop()
            rec.cleanup()

        storage.connect_to_db(db_path)
        try:
            runs = db.fetchall("SELECT * FROM runs")
            assert len(runs) == 2
        finally:
            storage.close_db()
            db.init(None)

    def test_stale_checkpoint_cleanup_by_pid(self, tmp_path):
        """Stale checkpoints from dead PIDs should be cleaned up."""
        db_path = str(tmp_path / "test.pyttd.db")
        storage.connect_to_db(db_path)
        storage.initialize_schema()

        run_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
            (run_id, "test.py", time.time(), 0))
        # Create a checkpoint with a definitely-dead PID
        db.execute(
            "INSERT INTO checkpoint (run_id, sequence_no, child_pid, is_alive) VALUES (?, ?, ?, ?)",
            (run_id, 100, 999999, 1))
        db.commit()

        storage.close_db()
        db.init(None)

        # Starting a new recorder should clean up stale checkpoints
        config = PyttdConfig(checkpoint_interval=0)
        rec = Recorder(config)
        script = tmp_path / "s2.py"
        script.write_text("y = 2\n")
        rec.start(db_path, script_path=str(script))

        # Check the stale checkpoint was cleaned
        stale = db.fetchone(
            "SELECT * FROM checkpoint WHERE child_pid = 999999")
        assert stale is None or stale.is_alive == 0

        import runpy, sys
        old_argv = sys.argv[:]
        sys.argv = [str(script)]
        try:
            runpy.run_path(str(script), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        rec.stop()
        rec.cleanup()
        db.init(None)


# --- Sub-task 3: Multi-run query ---

@pytest.fixture
def multi_run_db(tmp_path):
    """Create a DB with 3 runs."""
    db_path = str(tmp_path / "multi.pyttd.db")
    storage.connect_to_db(db_path)
    storage.initialize_schema()

    run_ids = []
    for i in range(3):
        run_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO runs (run_id, script_path, total_frames, timestamp_start)"
            " VALUES (?, ?, ?, ?)",
            (run_id, f"script{i}.py", 10 * (i + 1), time.time() + i))
        # Add a frame so queries work
        db.execute(
            "INSERT INTO executionframes"
            " (run_id, sequence_no, timestamp, line_no, filename,"
            "  function_name, frame_event, call_depth, locals_snapshot,"
            "  thread_id, is_coroutine)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, 0, 0.0, 1, f"script{i}.py", "main", "line", 0, "{}", 0, 0))
        run_ids.append(run_id)

    db.commit()
    storage.close_db()
    db.init(None)
    return db_path, run_ids


class TestMultiRunQuery:
    def test_get_all_runs(self, multi_run_db):
        db_path, run_ids = multi_run_db
        runs = get_all_runs(db_path)
        assert len(runs) == 3
        # Should be sorted by timestamp_start desc (newest first)
        assert runs[0].run_id == run_ids[2]
        storage.close_db()
        db.init(None)

    def test_get_run_by_id_exact(self, multi_run_db):
        db_path, run_ids = multi_run_db
        run = get_run_by_id(db_path, str(run_ids[1]))
        assert run.run_id == run_ids[1]
        storage.close_db()
        db.init(None)

    def test_get_run_by_id_prefix(self, multi_run_db):
        db_path, run_ids = multi_run_db
        prefix = str(run_ids[0])[:8]
        run = get_run_by_id(db_path, prefix)
        assert run.run_id == run_ids[0]
        storage.close_db()
        db.init(None)

    def test_get_run_by_id_not_found(self, multi_run_db):
        db_path, _ = multi_run_db
        with pytest.raises(ValueError, match="No run found"):
            get_run_by_id(db_path, "00000000-0000-0000-0000-000000000000")
        storage.close_db()
        db.init(None)

    def test_get_all_runs_empty(self, tmp_path):
        db_path = str(tmp_path / "empty.pyttd.db")
        runs = get_all_runs(db_path)
        assert runs == []
        storage.close_db()
        db.init(None)


# --- Sub-task 4: Size monitoring ---

class TestSizeMonitoring:
    def test_config_max_db_size(self):
        config = PyttdConfig(max_db_size_mb=100)
        assert config.max_db_size_mb == 100

    def test_config_max_db_size_negative(self):
        with pytest.raises(ValueError):
            PyttdConfig(max_db_size_mb=-1)


# --- Sub-task 5: Eviction ---

class TestEviction:
    def test_evict_keeps_last_n(self, multi_run_db):
        db_path, run_ids = multi_run_db
        evicted = evict_old_runs(db_path, keep=1)
        assert len(evicted) == 2
        # The newest run should be kept
        assert run_ids[2] not in evicted
        # Check DB state
        storage.connect_to_db(db_path)
        remaining = db.fetchall("SELECT * FROM runs")
        assert len(remaining) == 1
        assert remaining[0].run_id == run_ids[2]
        storage.close_db()
        db.init(None)

    def test_evict_dry_run(self, multi_run_db):
        db_path, run_ids = multi_run_db
        evicted = evict_old_runs(db_path, keep=1, dry_run=True)
        assert len(evicted) == 2
        # DB should be unchanged
        storage.connect_to_db(db_path)
        remaining = db.fetchall("SELECT * FROM runs")
        assert len(remaining) == 3
        storage.close_db()
        db.init(None)

    def test_evict_keep_all(self, multi_run_db):
        db_path, run_ids = multi_run_db
        evicted = evict_old_runs(db_path, keep=10)
        assert evicted == []
        storage.connect_to_db(db_path)
        remaining = db.fetchall("SELECT * FROM runs")
        assert len(remaining) == 3
        storage.close_db()
        db.init(None)

    def test_evict_zero_keeps_none(self, multi_run_db):
        db_path, run_ids = multi_run_db
        evicted = evict_old_runs(db_path, keep=0)
        assert len(evicted) == 3
        storage.connect_to_db(db_path)
        remaining = db.fetchall("SELECT * FROM runs")
        assert len(remaining) == 0
        storage.close_db()
        db.init(None)

    def test_evict_deletes_related_records(self, tmp_path):
        """Eviction should delete frames, checkpoints, and IO events."""
        db_path = str(tmp_path / "evict.pyttd.db")
        storage.connect_to_db(db_path)
        storage.initialize_schema()

        run_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
            (run_id, "old.py", time.time(), 5))
        for i in range(5):
            db.execute(
                "INSERT INTO executionframes"
                " (run_id, sequence_no, timestamp, line_no, filename,"
                "  function_name, frame_event, call_depth, locals_snapshot,"
                "  thread_id, is_coroutine)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, i, float(i), 1, "old.py", "f", "line", 0, "{}", 0, 0))
        db.execute(
            "INSERT INTO checkpoint (run_id, sequence_no, child_pid, is_alive)"
            " VALUES (?, ?, ?, ?)",
            (run_id, 0, None, 0))
        db.execute(
            "INSERT INTO ioevent (run_id, sequence_no, io_sequence, function_name, return_value)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, 0, 0, "time.time", b"1.0"))

        # Add a second (newer) run to keep
        run_id2 = uuid.uuid4().hex
        db.execute(
            "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
            (run_id2, "new.py", time.time() + 100, 0))
        db.commit()

        storage.close_db()
        db.init(None)

        evicted = evict_old_runs(db_path, keep=1)
        assert len(evicted) == 1
        assert evicted[0] == run_id

        storage.connect_to_db(db_path)
        assert db.fetchval(
            "SELECT COUNT(*) FROM executionframes WHERE run_id = ?", (run_id,)) == 0
        assert db.fetchval(
            "SELECT COUNT(*) FROM checkpoint WHERE run_id = ?", (run_id,)) == 0
        assert db.fetchval(
            "SELECT COUNT(*) FROM ioevent WHERE run_id = ?", (run_id,)) == 0
        storage.close_db()
        db.init(None)

    def test_config_keep_runs(self):
        config = PyttdConfig(keep_runs=5)
        assert config.keep_runs == 5

    def test_config_keep_runs_negative(self):
        with pytest.raises(ValueError):
            PyttdConfig(keep_runs=-1)

    def test_keep_runs_auto_evict(self, tmp_path):
        """Recorder with keep_runs should auto-evict on start()."""
        from pyttd.recorder import Recorder
        import runpy, sys

        db_path = str(tmp_path / "auto.pyttd.db")
        script = tmp_path / "s.py"
        script.write_text("z = 1\n")

        # Record 3 runs without keep_runs
        for _ in range(3):
            config = PyttdConfig(checkpoint_interval=0)
            rec = Recorder(config)
            rec.start(db_path, script_path=str(script))
            old_argv = sys.argv[:]
            sys.argv = [str(script)]
            try:
                runpy.run_path(str(script), run_name='__main__')
            except BaseException:
                pass
            finally:
                sys.argv = old_argv
            rec.stop()
            rec.cleanup()

        # Now record with keep_runs=2
        config = PyttdConfig(checkpoint_interval=0, keep_runs=2)
        rec = Recorder(config)
        rec.start(db_path, script_path=str(script))
        old_argv = sys.argv[:]
        sys.argv = [str(script)]
        try:
            runpy.run_path(str(script), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        rec.stop()
        rec.cleanup()

        # Should have 2 runs (keep_runs=2 evicts older ones)
        storage.connect_to_db(db_path)
        runs = db.fetchall("SELECT * FROM runs")
        assert len(runs) == 2
        storage.close_db()
        db.init(None)


# --- CLI integration ---

class TestCLIIntegration:
    def test_list_runs_flag(self, multi_run_db, capsys):
        """--list-runs should print all runs."""
        db_path, run_ids = multi_run_db
        import argparse
        args = argparse.Namespace(
            db=db_path, list_runs=True, run_id=None,
            frames=False, limit=50, last_run=False,
        )
        from pyttd.cli import _cmd_query
        _cmd_query(args)
        captured = capsys.readouterr()
        assert "Run ID" in captured.out
        assert "script0.py" in captured.out
        assert "script1.py" in captured.out
        assert "script2.py" in captured.out
        db.init(None)

    def test_query_run_id(self, multi_run_db, capsys):
        """--run-id should select specific run."""
        db_path, run_ids = multi_run_db
        import argparse
        args = argparse.Namespace(
            db=db_path, list_runs=False, run_id=str(run_ids[1]),
            frames=False, limit=50, last_run=False,
        )
        from pyttd.cli import _cmd_query
        _cmd_query(args)
        captured = capsys.readouterr()
        # Banner goes to stderr (#9 fix: always on stderr for pipeable stdout)
        assert str(run_ids[1]) in captured.err
        db.init(None)

    def test_clean_keep(self, multi_run_db, capsys):
        """clean --keep should evict old runs."""
        db_path, run_ids = multi_run_db
        import argparse
        args = argparse.Namespace(
            db=db_path, all=False, keep=1, dry_run=False,
        )
        from pyttd.cli import _cmd_clean
        _cmd_clean(args)
        captured = capsys.readouterr()
        assert "Evicted 2 run(s)" in captured.out
        db.init(None)

    def test_clean_keep_dry_run(self, multi_run_db, capsys):
        """clean --keep --dry-run should not modify DB."""
        db_path, run_ids = multi_run_db
        import argparse
        args = argparse.Namespace(
            db=db_path, all=False, keep=1, dry_run=True,
        )
        from pyttd.cli import _cmd_clean
        _cmd_clean(args)
        captured = capsys.readouterr()
        assert "Would evict 2 run(s)" in captured.out

        storage.connect_to_db(db_path)
        assert db.fetchval("SELECT COUNT(*) FROM runs") == 3
        storage.close_db()
        db.init(None)

    def test_clean_db_file(self, tmp_path, capsys):
        """clean --db should delete specific DB file."""
        db_file = tmp_path / "todelete.pyttd.db"
        db_file.write_text("")
        import argparse
        args = argparse.Namespace(
            db=str(db_file), all=False, keep=None, dry_run=False,
        )
        from pyttd.cli import _cmd_clean
        _cmd_clean(args)
        assert not db_file.exists()

    def test_clean_no_files(self, tmp_path, capsys, monkeypatch):
        """clean with no DB files should print message."""
        monkeypatch.chdir(tmp_path)
        import argparse
        args = argparse.Namespace(
            db=None, all=False, keep=None, dry_run=False,
        )
        from pyttd.cli import _cmd_clean
        _cmd_clean(args)
        captured = capsys.readouterr()
        assert "No .pyttd.db files found" in captured.out


# --- main.py API ---

class TestMainAPI:
    def test_ttdbg_appends(self, tmp_path, monkeypatch):
        """@ttdbg decorator should not delete existing DB."""
        from pyttd.main import ttdbg

        # Create a function in a known file
        source = tmp_path / "deco_test.py"
        source.write_text("pass\n")

        db_path = str(tmp_path / "deco_test.pyttd.db")

        # Create an existing run in the DB
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        run_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
            (run_id, str(source), time.time(), 5))
        db.commit()
        storage.close_db()
        db.init(None)

        # Use ttdbg - it should NOT delete the existing run
        @ttdbg
        def my_func():
            return 42

        # Patch inspect.getfile to return our known path
        import inspect
        monkeypatch.setattr(inspect, 'getfile', lambda f: str(source))
        my_func()

        storage.connect_to_db(db_path)
        runs = db.fetchall("SELECT * FROM runs")
        assert len(runs) == 2  # Original + new
        storage.close_db()
        db.init(None)
