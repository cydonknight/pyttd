import textwrap
import pytest
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.base import db
from pyttd.models.storage import delete_db_files, close_db


@pytest.fixture
def bench_record(tmp_path):
    """Record a script and return (db_path, run_id, stats).

    Usage: db_path, run_id, stats = bench_record('''
        def foo():
            return 42
        foo()
    ''')
    """
    recorders = []

    def _record(script_content, checkpoint_interval=0):
        script_file = tmp_path / "bench_script.py"
        script_file.write_text(textwrap.dedent(script_content))
        db_path = str(tmp_path / "bench.pyttd.db")
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

    for rec in recorders:
        try:
            pyttd_native.kill_all_checkpoints()
        except Exception:
            pass
    close_db()
    db.init(None)
