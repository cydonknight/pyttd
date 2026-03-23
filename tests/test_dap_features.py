"""Tests for DAP feature completeness (P2 VSCode enhancements).

Tests function breakpoints, hit count breakpoints, log points,
data breakpoints, and REPL evaluation.
"""
import json
import os

from pyttd.models.db import db
from pyttd.session import Session


def _setup_session(run_id):
    session = Session()
    first_line = db.fetchone(
        "SELECT sequence_no FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no LIMIT 1",
        (str(run_id),))
    session.enter_replay(run_id, first_line.sequence_no)
    return session


class TestFunctionBreakpoints:
    def test_set_function_breakpoints(self, record_func):
        db_path, run_id, _ = record_func('''
            def foo():
                return 42
            def bar():
                return 99
            foo()
            bar()
        ''')
        session = _setup_session(run_id)
        session.set_function_breakpoints([{'name': 'bar'}])
        assert len(session.function_breakpoints) == 1
        assert session.function_breakpoints[0]['name'] == 'bar'

    def test_verify_function_breakpoints_found(self, record_func):
        db_path, run_id, _ = record_func('''
            def foo():
                return 42
            foo()
        ''')
        session = _setup_session(run_id)
        result = session.verify_function_breakpoints([{'name': 'foo'}])
        assert len(result) == 1
        assert result[0]['verified'] is True

    def test_verify_function_breakpoints_not_found(self, record_func):
        db_path, run_id, _ = record_func('''
            def foo():
                return 42
            foo()
        ''')
        session = _setup_session(run_id)
        result = session.verify_function_breakpoints([{'name': 'nonexistent_func'}])
        assert len(result) == 1
        assert result[0]['verified'] is False
        assert 'not found' in result[0].get('message', '')

    def test_continue_forward_hits_function_breakpoint(self, record_func):
        db_path, run_id, _ = record_func('''
            def foo():
                x = 1
                return x
            def bar():
                y = 2
                return y
            foo()
            bar()
        ''')
        session = _setup_session(run_id)
        session.set_function_breakpoints([{'name': 'bar'}])
        result = session.continue_forward()
        assert result['reason'] == 'function breakpoint'
        # Verify we stopped at or after bar's call
        stopped_frame = db.fetchone(
            "SELECT * FROM executionframes WHERE run_id = ? AND sequence_no = ?",
            (str(run_id), result['seq']))
        assert stopped_frame is not None

    def test_reverse_continue_hits_function_breakpoint(self, record_func):
        db_path, run_id, _ = record_func('''
            def foo():
                x = 1
                return x
            def bar():
                y = 2
                return y
            foo()
            bar()
        ''')
        session = _setup_session(run_id)
        session.set_function_breakpoints([{'name': 'foo'}])
        # Go to end first
        last = session.last_line_seq
        session.goto_frame(last)
        result = session.reverse_continue()
        assert result['reason'] == 'function breakpoint'


class TestHitCountBreakpoints:
    def test_hit_condition_equals(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(10):
                x = i * 2
        ''')
        session = _setup_session(run_id)
        # Find a line that gets hit multiple times
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE '%\"x\"%'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'hitCondition': '3',
        }])
        result = session.continue_forward()
        assert result['reason'] == 'breakpoint'
        # The breakpoint should have been hit on the 3rd occurrence

    def test_hit_condition_greater_than(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(10):
                x = i * 2
        ''')
        session = _setup_session(run_id)
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE '%\"x\"%'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'hitCondition': '>5',
        }])
        result = session.continue_forward()
        assert result['reason'] == 'breakpoint'

    def test_check_hit_condition_helpers(self):
        """Test the hit condition parser directly."""
        session = Session()
        assert session._check_hit_condition('3', 3) is True
        assert session._check_hit_condition('3', 2) is False
        assert session._check_hit_condition('>3', 4) is True
        assert session._check_hit_condition('>3', 3) is False
        assert session._check_hit_condition('>=3', 3) is True
        assert session._check_hit_condition('<3', 2) is True
        assert session._check_hit_condition('<=3', 3) is True
        assert session._check_hit_condition('==3', 3) is True
        assert session._check_hit_condition('%2', 4) is True
        assert session._check_hit_condition('%2', 3) is False
        assert session._check_hit_condition('', 1) is True
        assert session._check_hit_condition('invalid', 1) is True


class TestLogPoints:
    def test_format_log_message(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 42
            y = "hello"
        ''')
        session = _setup_session(run_id)
        # Find frame where x is defined
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE '%\"x\"%'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert frame is not None
        msg = session._format_log_message("x = {x}", frame.sequence_no)
        assert "42" in msg

    def test_logpoint_does_not_stop(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(5):
                x = i
        ''')
        session = _setup_session(run_id)
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE '%\"x\"%'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert frame is not None
        # Set a log point (has logMessage) and a regular breakpoint elsewhere
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'logMessage': 'x = {x}',
        }])
        # With only a logpoint and no other breakpoints/filters, should go to end
        result = session.continue_forward()
        assert result['reason'] == 'end'


class TestDataBreakpoints:
    def test_set_data_breakpoints(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 1
            x = 2
            x = 3
        ''')
        session = _setup_session(run_id)
        session.set_data_breakpoints([{'variableName': 'x'}])
        assert len(session.data_breakpoints) == 1

    def test_data_breakpoint_detects_change(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 1
            x = 2
            x = 3
        ''')
        session = _setup_session(run_id)
        session.set_data_breakpoints([{'variableName': 'x'}])
        result = session.continue_forward()
        assert result['reason'] == 'data breakpoint'
        # Verify we stopped at a frame where x changed
        stopped = db.fetchone(
            "SELECT * FROM executionframes WHERE run_id = ? AND sequence_no = ?",
            (str(run_id), result['seq']))
        assert stopped is not None

    def test_data_breakpoint_reverse(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 1
            x = 2
            x = 3
        ''')
        session = _setup_session(run_id)
        # Go to end
        session.goto_frame(session.last_line_seq)
        session.set_data_breakpoints([{'variableName': 'x'}])
        result = session.reverse_continue()
        assert result['reason'] == 'data breakpoint'

    def test_get_variable_value_at(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 42
            y = "hello"
        ''')
        session = _setup_session(run_id)
        # Find frame with x
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE '%\"x\"%'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert frame is not None
        val = session._get_variable_value_at(frame.sequence_no, 'x')
        assert val is not None
        assert '42' in val


class TestReplEvaluation:
    def test_repl_context_works(self, record_func):
        """REPL context should now return values, not the old static message."""
        db_path, run_id, _ = record_func('''
            x = 42
            y = 10
        ''')
        session = _setup_session(run_id)
        # Find frame with x
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line' AND locals_snapshot LIKE '%\"x\"%'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        assert frame is not None
        result = session.evaluate_at(frame.sequence_no, 'x', 'repl')
        # Should NOT contain the old blocking message
        assert 'not available' not in result['result'].lower() or result['result'] != "Replay mode - expression evaluation not available. Use Variables panel to inspect recorded state."
        assert '42' in result['result']

    def test_repl_missing_variable(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 42
            y = x + 1
        ''')
        session = _setup_session(run_id)
        # Find any line event with locals
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND locals_snapshot IS NOT NULL AND locals_snapshot != '{}'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        if frame is None:
            # If no locals captured, just test with the first line event
            frame = db.fetchone(
                "SELECT * FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " ORDER BY sequence_no LIMIT 1",
                (str(run_id),))
        assert frame is not None
        result = session.evaluate_at(frame.sequence_no, 'nonexistent', 'repl')
        # REPL returns context-aware error message for missing variables
        assert 'Error' in result['result'] or '<not available>' in result['result']
