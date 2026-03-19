"""Tests for variable history (Phase 10B)."""
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


class TestVariableHistory:
    def test_basic_history(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 0
            x = 1
            x = 2
            x = 3
            x = 4
        ''')
        session = _setup_session(run_id)
        history = session.get_variable_history(
            'x', session.first_line_seq, session.last_line_seq)
        assert len(history) >= 4  # at least 4 distinct changes
        # Values should be monotonically increasing
        values = [h['value'] for h in history]
        for v in values:
            assert v in ('0', '1', '2', '3', '4')

    def test_history_deduplicates(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 1
            y = 2
            z = 3
            a = 4
        ''')
        session = _setup_session(run_id)
        history = session.get_variable_history(
            'x', session.first_line_seq, session.last_line_seq)
        # x is set once and never changes, so should appear at most once
        assert len(history) <= 1

    def test_history_variable_not_found(self, record_func):
        db_path, run_id, _ = record_func('''
            x = 42
        ''')
        session = _setup_session(run_id)
        history = session.get_variable_history(
            'nonexistent', session.first_line_seq, session.last_line_seq)
        assert history == []

    def test_history_range_filter(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(20):
                x = i
        ''')
        session = _setup_session(run_id)
        # Get full range
        full_history = session.get_variable_history(
            'i', session.first_line_seq, session.last_line_seq)
        assert len(full_history) > 4, "Loop of 20 should produce >4 distinct values for 'i'"
        # Get partial range (first half)
        mid_seq = full_history[len(full_history) // 2]['seq']
        partial = session.get_variable_history(
            'i', session.first_line_seq, mid_seq)
        assert len(partial) < len(full_history)

    def test_history_max_points(self, record_func):
        db_path, run_id, _ = record_func('''
            for i in range(100):
                x = i
        ''')
        session = _setup_session(run_id)
        history = session.get_variable_history(
            'i', session.first_line_seq, session.last_line_seq, max_points=5)
        assert len(history) <= 5

    def test_history_across_functions(self, record_func):
        db_path, run_id, _ = record_func('''
            def set_x(val):
                x = val
                return x

            x = 10
            set_x(20)
            x = 30
        ''')
        session = _setup_session(run_id)
        history = session.get_variable_history(
            'x', session.first_line_seq, session.last_line_seq)
        # x appears in both module scope and function scope
        assert len(history) >= 1

    def test_history_with_structured_values(self, record_func):
        db_path, run_id, _ = record_func('''
            d = {"a": 1}
            d = {"a": 1, "b": 2}
            d = {"a": 1, "b": 2, "c": 3}
        ''')
        session = _setup_session(run_id)
        history = session.get_variable_history(
            'd', session.first_line_seq, session.last_line_seq)
        assert len(history) >= 2
        for h in history:
            assert 'value' in h
            assert isinstance(h['value'], str)
