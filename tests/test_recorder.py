import json
import os
import textwrap
import pytest
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.storage import delete_db_files, close_db
from pyttd.models.base import db


@pytest.fixture
def record_func(tmp_path):
    """Record a script and return (db_path, run_id, stats)."""
    def _record(script_content):
        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent(script_content))
        db_path = str(tmp_path / "test.pyttd.db")
        delete_db_files(db_path)

        config = PyttdConfig()
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))

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
        # Don't cleanup — tests need to query the DB
        return db_path, run_id, stats
    yield _record
    close_db()
    db.init(None)


def test_basic_recording(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            a = 1
            b = 2
            return a + b
        foo()
    """)
    assert stats['frame_count'] > 0
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    assert len(frames) > 0


def test_sequence_no_monotonic(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            a = 1
            b = 2
            return a + b
        foo()
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    for i in range(1, len(frames)):
        assert frames[i].sequence_no > frames[i-1].sequence_no


def test_first_event_is_call(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            return 42
        foo()
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    # First user frame event should be 'call'
    assert frames[0].frame_event == 'call'


def test_last_event_is_return(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            return 42
        foo()
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    # Find events for 'foo' specifically — last should be 'return'
    foo_frames = [f for f in frames if f.function_name == 'foo']
    assert foo_frames[-1].frame_event == 'return'


def test_call_depth(record_func):
    db_path, run_id, stats = record_func("""\
        def inner():
            return 1
        def outer():
            return inner()
        outer()
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    # Find outer's call event and inner's call event
    outer_calls = [f for f in frames if f.function_name == 'outer' and f.frame_event == 'call']
    inner_calls = [f for f in frames if f.function_name == 'inner' and f.frame_event == 'call']
    assert len(outer_calls) > 0
    assert len(inner_calls) > 0
    assert inner_calls[0].call_depth > outer_calls[0].call_depth


def test_locals_snapshot_is_valid_json(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            x = 42
            y = "hello"
            return x
        foo()
    """)
    frames = list(ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no))
    assert len(frames) > 0
    for f in frames:
        if f.locals_snapshot:
            data = json.loads(f.locals_snapshot)
            assert isinstance(data, dict)


def test_stdlib_not_recorded(record_func):
    db_path, run_id, stats = record_func("""\
        import os
        x = os.path.join("a", "b")
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    # No frames should have stdlib filenames
    for f in frames:
        assert 'lib/python' not in f.filename
        assert 'site-packages' not in f.filename


def test_exception_event(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            raise ValueError("test error")
        try:
            foo()
        except ValueError:
            pass
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    exception_frames = [f for f in frames if f.frame_event == 'exception']
    assert len(exception_frames) > 0


def test_total_frames_in_run(record_func):
    db_path, run_id, stats = record_func("""\
        def foo():
            a = 1
            return a
        foo()
    """)
    run = Runs.get(Runs.run_id == run_id)
    assert run.total_frames == stats['frame_count']
    assert run.total_frames > 0
