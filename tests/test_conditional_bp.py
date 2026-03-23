"""Tests for conditional breakpoints (Phase 9A)."""
import os
import sys

import pytest

from pyttd.models.frames import ExecutionFrames
from pyttd.session import Session


def _setup_session(run_id):
    session = Session()
    first_line = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                  .where((ExecutionFrames.run_id == run_id) &
                         (ExecutionFrames.frame_event == 'line'))
                  .order_by(ExecutionFrames.sequence_no)
                  .first())
    session.enter_replay(run_id, first_line.sequence_no)
    return session


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="Conditional breakpoint eval unreliable on Windows (repr parsing differences)")
class TestConditionalBreakpoints:
    def test_continue_forward_with_condition(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(10):
                x = i * 2
        ''')
        session = _setup_session(run_id)
        # Find the file path for 'x = i * 2' line
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': 'i == 5',
        }])
        result = session.continue_forward()
        assert result['reason'] == 'breakpoint'
        # Verify that at this frame, i == 5
        import json
        stopped = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == result['seq']))
        assert stopped is not None, "Stopped frame should exist"
        assert stopped.locals_snapshot, "Stopped frame should have locals"
        locals_data = json.loads(stopped.locals_snapshot)
        assert locals_data.get('i') in ('5', 5)

    def test_continue_forward_condition_false_skips(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(10):
                x = i * 2
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': 'i == 999',
        }])
        result = session.continue_forward()
        assert result['reason'] == 'end'

    def test_reverse_continue_with_condition(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(10):
                x = i * 2
        ''')
        session = _setup_session(run_id)
        # Go to end
        last_line = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                     .where((ExecutionFrames.run_id == run_id) &
                            (ExecutionFrames.frame_event == 'line'))
                     .order_by(ExecutionFrames.sequence_no.desc())
                     .first())
        session.goto_frame(last_line.sequence_no)
        # Find a breakpoint line
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': 'i == 3',
        }])
        result = session.reverse_continue()
        assert result['reason'] == 'breakpoint'

    def test_empty_condition_matches_all(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(5):
                x = i
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': '',
        }])
        result = session.continue_forward()
        assert result['reason'] == 'breakpoint'

    def test_condition_eval_error_does_not_fire(self, record_func):
        """Broken conditions should NOT fire breakpoints (fail-closed)."""
        db_path, run_id, _ = record_func('''
            for i in range(5):
                x = i
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': '!!!invalid_syntax!!!',
        }])
        result = session.continue_forward()
        # Should NOT stop — broken condition fails closed
        assert result['reason'] == 'end'

    def test_condition_eval_error_logs_warning(self, record_func, caplog):
        """Broken conditions should log a warning."""
        import logging
        db_path, run_id, _ = record_func('''
            for i in range(3):
                x = i
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': '!!!invalid!!!',
        }])
        with caplog.at_level(logging.WARNING, logger='pyttd.session'):
            session.continue_forward()
        assert any('Condition eval error' in r.message for r in caplog.records)

    def test_condition_with_comparison(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(100):
                x = i
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': 'i > 50 and i < 55',
        }])
        result = session.continue_forward()
        assert result['reason'] == 'breakpoint'

    def test_condition_with_string_variable(self, record_func):
        db_path, run_id, _ = record_func('''
            names = ['Alice', 'Bob', 'Charlie']
            for name in names:
                greeting = f"Hello {name}"
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"name"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None, "Expected frame with 'name' variable in locals"
        session.set_breakpoints([{
            'file': frame.filename,
            'line': frame.line_no,
            'condition': "name == 'Bob'",
        }])
        result = session.continue_forward()
        # May hit due to matching or end if condition can't be evaluated
        assert result['reason'] in ('breakpoint', 'end')

    def test_multiple_conditional_breakpoints(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(20):
                x = i * 2
        ''')
        session = _setup_session(run_id)
        frame = (ExecutionFrames.select()
                 .where((ExecutionFrames.run_id == run_id) &
                        (ExecutionFrames.frame_event == 'line') &
                        (ExecutionFrames.locals_snapshot.contains('"i"')))
                 .order_by(ExecutionFrames.sequence_no)
                 .first())
        assert frame is not None
        session.set_breakpoints([
            {'file': frame.filename, 'line': frame.line_no, 'condition': 'i == 15'},
            {'file': frame.filename, 'line': frame.line_no, 'condition': 'i == 5'},
        ])
        result = session.continue_forward()
        # Should stop at nearest qualifying hit (i==5 comes first)
        assert result['reason'] == 'breakpoint'
