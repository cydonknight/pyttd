"""Phase 4: Reverse navigation tests.

Tests for step_back, reverse_continue, goto_frame, goto_targets, restart_frame.
"""
import os
import pytest
from pyttd.session import Session
from pyttd.models.frames import ExecutionFrames


def _enter_replay(session, run_id):
    """Helper: set up session in replay mode."""
    first_line = (ExecutionFrames.select()
                  .where((ExecutionFrames.run_id == run_id) &
                         (ExecutionFrames.frame_event == 'line'))
                  .order_by(ExecutionFrames.sequence_no)
                  .limit(1).first())
    first_line_seq = first_line.sequence_no if first_line else 0
    session.enter_replay(run_id, first_line_seq)
    return first_line_seq


def _navigate_to_end(session):
    """Step into until we reach the end."""
    for _ in range(500):
        result = session.step_into()
        if result.get("reason") == "end":
            return result
    return session.step_into()


class TestStepBack:
    def test_step_back_basic(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
            z = 3
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)

        # Step forward a few times
        r1 = session.step_into()
        r2 = session.step_into()
        seq_at_r2 = r2["seq"]

        # Step back should go to r1's position
        rb = session.step_back()
        assert rb["seq"] == r1["seq"]
        assert rb["reason"] == "step"

    def test_step_back_at_beginning(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)

        # Step back at the very beginning
        result = session.step_back()
        assert result["reason"] == "start"
        assert result["seq"] == first_seq

    def test_step_back_stays_at_first(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)

        # Step forward once
        r1 = session.step_into()
        # Step back to first
        rb = session.step_back()
        # Step back again should stay at first
        rb2 = session.step_back()
        assert rb2["reason"] == "start"
        assert rb2["seq"] == first_seq

    def test_step_back_then_forward(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
            z = 3
        """)
        session = Session()
        _enter_replay(session, run_id)

        r1 = session.step_into()
        r2 = session.step_into()
        seq2 = r2["seq"]

        # Step back
        rb = session.step_back()
        assert rb["seq"] == r1["seq"]

        # Step forward again
        rf = session.step_into()
        assert rf["seq"] == seq2


class TestReverseContinue:
    def test_reverse_continue_with_breakpoint(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 1
                return x
            y = foo()
            z = y + 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Navigate to end
        _navigate_to_end(session)

        # Find a line event with the function foo to set breakpoint
        foo_frames = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line') &
                   (ExecutionFrames.function_name.contains('foo')))
            .order_by(ExecutionFrames.sequence_no)
            .limit(1))
        assert len(foo_frames) >= 1
        bp_file = foo_frames[0].filename
        bp_line = foo_frames[0].line_no

        session.set_breakpoints([{"file": bp_file, "line": bp_line}])

        result = session.reverse_continue()
        assert result["reason"] == "breakpoint"
        assert result["seq"] == foo_frames[0].sequence_no

    def test_reverse_continue_no_breakpoints(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)

        # Navigate forward
        session.step_into()

        # Reverse continue with no breakpoints -> start
        result = session.reverse_continue()
        assert result["reason"] == "start"
        assert result["seq"] == first_seq

    def test_reverse_continue_exception_filter(self, record_func):
        db_path, run_id, stats = record_func("""\
            try:
                raise ValueError("test")
            except ValueError:
                pass
            x = 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Navigate to end
        _navigate_to_end(session)

        session.set_exception_filters(["raised"])
        result = session.reverse_continue()
        assert result["reason"] == "exception"


class TestGotoFrame:
    def test_goto_frame_basic(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
            z = 3
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a line event to jump to
        lines = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no))
        assert len(lines) >= 2
        target = lines[-1]

        result = session.goto_frame(target.sequence_no)
        assert result["seq"] == target.sequence_no
        assert result["reason"] == "goto"

    def test_goto_frame_not_found(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        result = session.goto_frame(999999)
        assert "error" in result
        assert result["error"] == "frame_not_found"

    def test_goto_frame_snaps_to_line(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                return 42
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a call event (not a line event)
        call_frame = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'call') &
                   (ExecutionFrames.function_name.contains('foo')))
            .order_by(ExecutionFrames.sequence_no)
            .first())
        assert call_frame is not None, "Should find a call event for foo"
        result = session.goto_frame(call_frame.sequence_no)
        # Should snap to nearest line event
        assert result["reason"] == "goto"
        snapped_frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == result["seq"]))
        assert snapped_frame.frame_event == 'line'


class TestGotoTargets:
    def test_goto_targets_finds_line(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
            x = 3
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a user-code line event to get file and line
        user_lines = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no))
        assert len(user_lines) >= 1
        target = user_lines[0]

        targets = session.goto_targets(target.filename, target.line_no)
        assert len(targets) >= 1
        assert all("seq" in t for t in targets)
        assert all("function_name" in t for t in targets)

    def test_goto_targets_no_match(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        targets = session.goto_targets("/nonexistent/file.py", 999)
        assert targets == []


class TestRestartFrame:
    def test_restart_frame(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 1
                y = 2
                return x + y
            result = foo()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a line event inside foo
        foo_lines = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line') &
                   (ExecutionFrames.function_name.contains('foo')))
            .order_by(ExecutionFrames.sequence_no))
        assert len(foo_lines) >= 2, f"Should find at least 2 line events in foo, got {len(foo_lines)}"
        # Navigate to second line in foo
        session.goto_frame(foo_lines[1].sequence_no)

        # Restart frame should go back to first line in foo
        result = session.restart_frame(foo_lines[1].sequence_no)
        assert result["reason"] == "goto"
        assert result["seq"] == foo_lines[0].sequence_no

    def test_restart_frame_not_found(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        result = session.restart_frame(999999)
        assert "error" in result
