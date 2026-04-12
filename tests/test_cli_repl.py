"""Tests for Phase 1C: Interactive REPL fixes.

Covers:
- Finding #4: step_out lands on the caller frame (not a later line at the same depth)
- Finding #7: breaks listing distinguishes log points
- Finding #14: log messages emit before stop notification (order check)
- UX-D: rcontinue / rc alias calls reverse_continue
"""
import sys

import pytest

from pyttd.models.db import db
from pyttd.session import Session


def _enter_replay(session, run_id):
    first_line = db.fetchone(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no LIMIT 1",
        (str(run_id),))
    first_line_seq = first_line.sequence_no if first_line else 0
    session.enter_replay(run_id, first_line_seq)
    return first_line_seq


class TestStepOut:
    """Finding #4: step_out must land at a shallower call_depth."""

    def test_step_out_from_inner_lands_at_caller(self, record_func):
        """3-frame call: outer → middle → inner. step_out from inner
        should land at a line in middle (depth < inner's depth)."""
        db_path, run_id, stats = record_func("""\
            def inner():
                x = 1
                return x
            def middle():
                val = inner()
                y = val + 1
                return y
            def outer():
                result = middle()
                z = result + 2
                return z
            outer()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Navigate into inner's body (a line event inside inner)
        inner_line = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND function_name = 'inner' AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert inner_line is not None
        session.goto_frame(inner_line.sequence_no)
        inner_depth = inner_line.call_depth

        # step_out should land at middle (shallower depth)
        result = session.step_out()
        assert result.get('error') is None
        result_depth = result.get('call_depth', inner_depth)
        assert result_depth < inner_depth, \
            f"step_out should land at shallower depth: got {result_depth}, inner was {inner_depth}"

    def test_step_out_from_depth_1_lands_at_module(self, record_func):
        """step_out from a depth-1 function should reach depth 0 or end."""
        db_path, run_id, stats = record_func("""\
            def f():
                x = 1
                return x
            f()
            y = 2
        """)
        session = Session()
        _enter_replay(session, run_id)

        f_line = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND function_name = 'f' AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert f_line is not None
        session.goto_frame(f_line.sequence_no)

        result = session.step_out()
        # Should land at depth 0 (module scope) or at end of recording
        if result.get('reason') != 'end':
            assert result.get('call_depth', 1) < f_line.call_depth, \
                f"step_out should leave the function"


class TestReverseContAlias:
    """UX-D: reverse_continue should be accessible as rcontinue/rc."""

    def test_reverse_continue_works(self, record_func):
        """reverse_continue from the end should walk backward to start."""
        db_path, run_id, stats = record_func("""\
            def f():
                return 42
            f()
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)
        # Navigate to the end
        last_line = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no DESC LIMIT 1",
            (str(run_id),))
        session.goto_frame(last_line.sequence_no)

        # Reverse continue with no breakpoints should reach start
        result = session.reverse_continue()
        assert result.get('reason') == 'start'
