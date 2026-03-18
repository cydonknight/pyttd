import json
import pyttd_native
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs


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


def test_repr_reentrancy(record_func):
    """Verify that user-defined __repr__ doesn't corrupt locals or cause crashes."""
    db_path, run_id, stats = record_func("""\
        class Foo:
            def __repr__(self):
                return "Foo()"
        def bar():
            f = Foo()
            return f
        bar()
    """)
    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))
    assert len(frames) > 0
    for f in frames:
        if f.locals_snapshot:
            data = json.loads(f.locals_snapshot)
            assert isinstance(data, dict)


def test_elapsed_time_accurate(record_func):
    """Verify elapsed_time reflects recording duration, not query time."""
    import time
    db_path, run_id, stats = record_func("""\
        x = 1
    """)
    time.sleep(0.1)
    stats2 = pyttd_native.get_recording_stats()
    # Both stat calls should report approximately the same elapsed_time
    # since recording is stopped — not growing with wall clock
    assert abs(stats['elapsed_time'] - stats2['elapsed_time']) < 0.01
