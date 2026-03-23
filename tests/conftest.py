import sys
import textwrap
import pytest
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db


@pytest.fixture(autouse=True)
def _reset_trace_state():
    """Reset CPython trace/monitoring state between tests.

    PyEval_SetTrace on 3.12+ sets persistent monitoring events on code objects.
    sys.settrace(None) + sys.monitoring.restart_events() clears them so that
    tests that use attach mode (which calls PyEval_SetTrace) don't contaminate
    subsequent tests.
    """
    yield
    sys.settrace(None)
    if hasattr(sys, 'monitoring'):
        sys.monitoring.restart_events()

@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary DB path for test isolation."""
    return str(tmp_path / "test.pyttd.db")

@pytest.fixture
def db_setup(db_path):
    """Connect to a temp DB, create tables, and close after test."""
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    yield db_path
    storage.close_db()

@pytest.fixture
def record_func(tmp_path):
    """Record a script and return (db_path, run_id, stats).

    Usage: db_path, run_id, stats = record_func('''
        def foo():
            return 42
        foo()
    ''')
    """
    recorders = []

    def _record(script_content, checkpoint_interval=0):
        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent(script_content))
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=checkpoint_interval)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        recorders.append(recorder)

        import runpy
        import sys
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        stats = recorder.stop()
        run_id = recorder.run_id
        return db_path, run_id, stats
    yield _record
    # Kill any checkpoint children and close DB
    for rec in recorders:
        try:
            pyttd_native.kill_all_checkpoints()
        except Exception:
            pass
    close_db()
