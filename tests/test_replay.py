"""Phase 2: Replay controller tests.

Tests for warm-only navigation via SQLite reads.
Cold navigation tests require live checkpoint children (server mode only).
"""
import json
import pytest
from pyttd.replay import ReplayController
from pyttd.models.frames import ExecutionFrames


def test_warm_goto_frame_basic(record_func):
    """warm_goto_frame returns correct frame data from SQLite."""
    db_path, run_id, stats = record_func("""\
        def foo():
            x = 42
            y = "hello"
            return x + 1
        foo()
    """)

    controller = ReplayController()

    # Get a line event for foo
    frames = list(ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.function_name == 'foo') &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no)
        .limit(1))
    assert len(frames) > 0

    result = controller.warm_goto_frame(run_id, frames[0].sequence_no)
    assert result['seq'] == frames[0].sequence_no
    assert result['function_name'] == 'foo'
    assert result['warm_only'] == True
    assert isinstance(result['locals'], dict)


def test_warm_goto_frame_not_found(record_func):
    """warm_goto_frame with invalid seq returns error."""
    db_path, run_id, stats = record_func("""\
        x = 1
    """)

    controller = ReplayController()
    result = controller.warm_goto_frame(run_id, 999999)
    assert 'error' in result
    assert result['error'] == 'frame_not_found'


def test_warm_goto_frame_return_event(record_func):
    """warm_goto_frame on a return event includes __return__ in locals."""
    db_path, run_id, stats = record_func("""\
        def foo():
            return 42
        foo()
    """)

    controller = ReplayController()

    frames = list(ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.function_name == 'foo') &
               (ExecutionFrames.frame_event == 'return'))
        .order_by(ExecutionFrames.sequence_no)
        .limit(1))
    assert len(frames) > 0

    result = controller.warm_goto_frame(run_id, frames[0].sequence_no)
    assert result['seq'] == frames[0].sequence_no
    assert '__return__' in result['locals']


def test_warm_goto_frame_exception_event(record_func):
    """warm_goto_frame on an exception event includes __exception__ in locals."""
    db_path, run_id, stats = record_func("""\
        def foo():
            raise ValueError("test error")
        try:
            foo()
        except ValueError:
            pass
    """)

    controller = ReplayController()

    frames = list(ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.function_name == 'foo') &
               (ExecutionFrames.frame_event == 'exception'))
        .order_by(ExecutionFrames.sequence_no)
        .limit(1))
    assert len(frames) > 0

    result = controller.warm_goto_frame(run_id, frames[0].sequence_no)
    assert result['seq'] == frames[0].sequence_no
    assert '__exception__' in result['locals']


def test_warm_goto_frame_all_sequences(record_func):
    """Every recorded frame should be reachable via warm_goto_frame."""
    db_path, run_id, stats = record_func("""\
        def foo():
            a = 1
            b = 2
            return a + b
        foo()
    """)

    controller = ReplayController()

    frames = list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no))

    for f in frames:
        result = controller.warm_goto_frame(run_id, f.sequence_no)
        assert result['seq'] == f.sequence_no
        assert result['file'] == f.filename
        assert result['line'] == f.line_no
        assert result['function_name'] == f.function_name


def test_warm_goto_frame_locals_valid_json(record_func):
    """Locals from warm_goto_frame should always be valid dicts."""
    db_path, run_id, stats = record_func("""\
        def foo():
            x = [1, 2, 3]
            y = {"a": 1}
            z = "hello\\nworld"
            return x
        foo()
    """)

    controller = ReplayController()

    frames = list(ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no))

    for f in frames:
        result = controller.warm_goto_frame(run_id, f.sequence_no)
        assert isinstance(result['locals'], dict)
