import json
import textwrap
import pytest
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.storage import delete_db_files, close_db
from pyttd.models.base import db
import pyttd_native


@pytest.fixture
def record_simple(tmp_path):
    """Record a simple script and return (db_path, run_id, stats)."""
    script_file = tmp_path / "simple.py"
    script_file.write_text("x = 1\ny = 2\nz = x + y\n")
    db_path = str(tmp_path / "simple.pyttd.db")
    delete_db_files(db_path)

    config = PyttdConfig()
    recorder = Recorder(config)
    recorder.start(db_path, script_path=str(script_file))

    import runpy, sys
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
    yield db_path, run_id, stats
    close_db()
    db.init(None)


def test_frames_in_db_after_stop(record_simple):
    db_path, run_id, stats = record_simple
    count = ExecutionFrames.select().where(ExecutionFrames.run_id == run_id).count()
    assert count > 0
    assert count == stats['frame_count']


def test_flush_dict_keys(record_simple):
    db_path, run_id, stats = record_simple
    frame = ExecutionFrames.select().where(ExecutionFrames.run_id == run_id).first()
    assert frame is not None
    assert frame.sequence_no is not None
    assert frame.timestamp is not None
    assert frame.line_no is not None
    assert frame.filename is not None
    assert frame.function_name is not None
    assert frame.frame_event is not None
    assert frame.call_depth is not None


def test_recording_stats(record_simple):
    db_path, run_id, stats = record_simple
    assert 'frame_count' in stats
    assert 'dropped_frames' in stats
    assert 'elapsed_time' in stats
    assert 'flush_count' in stats
    assert 'pool_overflows' in stats
    assert stats['frame_count'] > 0
    assert stats['dropped_frames'] == 0
