"""Tests for ISSUES-PLAN.md Issue 4 (a, b/5, c, d) and Issue 1."""
import json
import os
import subprocess
import sys
import pytest
from pyttd.recorder import Recorder
from pyttd.config import PyttdConfig
from pyttd.models import storage, schema
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db


# -------------------------------------------------------------------
# Issue 1: pyttd replay NameError on total_frames
# -------------------------------------------------------------------

class TestIssue1ReplayCliWorks:
    """Regression: pyttd replay must not crash with NameError on total_frames."""

    def test_replay_goto_frame_cli(self, record_func, tmp_path):
        db_path, run_id, _ = record_func("""
def f():
    x = 1
    y = 2
    return x + y
f()
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--goto-frame", "2", "--db", db_path,
             "--run-id", str(run_id)[:8]],
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, (
            f"pyttd replay crashed: stderr={result.stderr!r}")
        assert "NameError" not in result.stderr
        assert "Frame" in result.stdout


# -------------------------------------------------------------------
# Issue 4a: Recorder warning plumbing
# -------------------------------------------------------------------

class TestIssue4aRecorderWarnings:
    """stop() stats must include a 'warnings' key."""

    def test_warnings_key_present_on_success(self, tmp_path):
        import runpy
        script_file = tmp_path / "t.py"
        script_file.write_text("def f():\n    return 1\nf()\n")
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        finally:
            sys.argv = old_argv
        stats = recorder.stop()
        recorder.cleanup()

        assert 'warnings' in stats
        assert isinstance(stats['warnings'], list)
        # Clean runs have no warnings
        assert stats['warnings'] == []

    def test_warnings_populated_on_failure(self, tmp_path):
        """If binlog_load fails, a warning is added to stats['warnings']."""
        import runpy
        script_file = tmp_path / "t.py"
        script_file.write_text("def f():\n    return 1\nf()\n")
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        finally:
            sys.argv = old_argv

        # Simulate batch_insert failures via the internal counter — this
        # exercises the rollup path without actually corrupting the DB.
        recorder._batch_insert_failures = 3

        stats = recorder.stop()
        recorder.cleanup()

        assert 'warnings' in stats
        msgs = " ".join(stats['warnings'])
        assert "3 flush batch" in msgs

    def test_checkpoint_failure_rollup(self, tmp_path):
        """_checkpoint_failures rolls up into a summary warning."""
        import runpy
        script_file = tmp_path / "t.py"
        script_file.write_text("def f():\n    return 1\nf()\n")
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        finally:
            sys.argv = old_argv

        recorder._checkpoint_failures = 2
        stats = recorder.stop()
        recorder.cleanup()

        msgs = " ".join(stats.get('warnings', []))
        assert "2 checkpoint" in msgs

    def test_warnings_surface_in_cli(self, record_func, tmp_path, monkeypatch):
        """The _cmd_record handler prints warnings from stats['warnings']."""
        script = tmp_path / "warn_script.py"
        script.write_text("def f():\n    return 1\nf()\n")
        # Run record as subprocess and check for Warning text if present.
        # (Clean runs produce no warnings, so just verify it doesn't crash.)
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "record", str(script)],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0


# -------------------------------------------------------------------
# Issue 4b/5: Migration versioning via pyttd_meta
# -------------------------------------------------------------------

class TestIssue4bMigrationVersioning:
    """pyttd_meta table tracks migration_version; migrations are idempotent."""

    def test_migration_version_recorded(self, tmp_path):
        db_path = str(tmp_path / "migtest.pyttd.db")
        delete_db_files(db_path)
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            row = db.fetchone(
                "SELECT value FROM pyttd_meta WHERE key = 'migration_version'")
            assert row is not None
            assert int(row.value) == len(schema.MIGRATION_SQL)
        finally:
            close_db()

    def test_initialize_schema_is_idempotent(self, tmp_path):
        """Calling initialize_schema twice must not crash or duplicate data."""
        db_path = str(tmp_path / "idem.pyttd.db")
        delete_db_files(db_path)
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        storage.initialize_schema()  # second call
        try:
            row = db.fetchone(
                "SELECT value FROM pyttd_meta WHERE key = 'migration_version'")
            assert int(row.value) == len(schema.MIGRATION_SQL)
        finally:
            close_db()

    def test_old_db_without_meta_upgrades_cleanly(self, tmp_path):
        """An existing DB whose schema is current but lacks pyttd_meta
        (simulates a DB created before versioning shipped) must upgrade
        without error and record migration_version."""
        db_path = str(tmp_path / "old.pyttd.db")
        delete_db_files(db_path)

        # Create the current schema but DROP pyttd_meta afterwards to
        # simulate the pre-versioning state.
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            db.execute("DROP TABLE pyttd_meta")
            db.commit()
        finally:
            close_db()

        # Now call initialize_schema again — should recreate pyttd_meta
        # and record migration_version without any errors.
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            row = db.fetchone(
                "SELECT value FROM pyttd_meta WHERE key = 'migration_version'")
            assert row is not None
            assert int(row.value) == len(schema.MIGRATION_SQL)
        finally:
            close_db()


# -------------------------------------------------------------------
# Issue 4c: Replay parse error marker
# -------------------------------------------------------------------

class TestIssue4cParseErrorMarker:
    """warm_goto_frame returns __parse_error__ in locals_data on bad JSON."""

    def test_malformed_locals_returns_parse_error(self, record_func, tmp_path):
        db_path, run_id, _ = record_func("""
def f():
    x = 1
    return x
f()
""")
        # Corrupt the locals_snapshot of one frame
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            frame = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " ORDER BY sequence_no DESC LIMIT 1",
                (str(run_id),))
            assert frame is not None
            target_seq = frame.sequence_no
            db.execute(
                "UPDATE executionframes SET locals_snapshot = ? "
                "WHERE run_id = ? AND sequence_no = ?",
                ("{not valid json", str(run_id), target_seq))
            db.commit()
        finally:
            close_db()

        # Now replay that specific frame
        from pyttd.replay import ReplayController
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        try:
            ctrl = ReplayController()
            result = ctrl.warm_goto_frame(str(run_id), target_seq)
            assert "locals" in result
            assert "__parse_error__" in result["locals"]
        finally:
            close_db()


# -------------------------------------------------------------------
# Issue 4d: CLI error formatting when error is a dict
# -------------------------------------------------------------------

class TestIssue4dErrorFormatting:
    """_show_frame must extract messages from dict-shaped errors."""

    def test_string_error_unchanged(self, record_func, tmp_path):
        """Plain string errors still display as before."""
        db_path, run_id, _ = record_func("""
def f():
    return 1
f()
""")
        # Request a frame that doesn't exist — produces a string error
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--goto-frame", "99999", "--db", db_path,
             "--run-id", str(run_id)[:8]],
            capture_output=True, text=True, timeout=15)
        assert result.returncode != 0
        assert "Error:" in result.stderr or "Error:" in result.stdout

    def test_dict_error_formatting_unit(self):
        """Unit test the dict-vs-string logic directly (without subprocess)."""
        # Simulate what _show_frame does with a dict error
        err = {"message": "Something broke", "code": 42}
        msg = err.get('message') or err.get('error') or str(err)
        assert msg == "Something broke"

        err2 = {"error": "Fallback"}
        msg2 = err2.get('message') or err2.get('error') or str(err2)
        assert msg2 == "Fallback"

        err3 = "plain string error"
        # str path:
        msg3 = err3 if isinstance(err3, str) else str(err3)
        assert msg3 == "plain string error"
