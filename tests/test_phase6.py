"""Phase 6 backend tests: get_traced_files, get_execution_stats, get_call_children."""
import pytest
from pyttd.session import Session
from pyttd.models.frames import ExecutionFrames


def _enter_replay(session, run_id):
    """Helper to enter replay mode for a run."""
    first_line = (ExecutionFrames.select()
                  .where((ExecutionFrames.run_id == run_id) &
                         (ExecutionFrames.frame_event == 'line'))
                  .order_by(ExecutionFrames.sequence_no).first())
    session.enter_replay(run_id, first_line.sequence_no)


def _find_user_module_call(session, script_filename_part):
    """Find the user script's <module> call in the call tree.
    The recorder captures all frames including Python internals,
    so the user's <module> is nested several levels deep."""
    # Find the <module> call event for the user script
    ev = (ExecutionFrames.select()
          .where((ExecutionFrames.run_id == session.run_id) &
                 (ExecutionFrames.frame_event == 'call') &
                 (ExecutionFrames.function_name == '<module>') &
                 (ExecutionFrames.filename.contains(script_filename_part)))
          .order_by(ExecutionFrames.sequence_no).first())
    if not ev:
        return None
    # Find matching return
    ret = (ExecutionFrames.select()
           .where((ExecutionFrames.run_id == session.run_id) &
                  (ExecutionFrames.frame_event.in_(['return', 'exception_unwind'])) &
                  (ExecutionFrames.call_depth == ev.call_depth) &
                  (ExecutionFrames.sequence_no > ev.sequence_no))
           .order_by(ExecutionFrames.sequence_no).first())
    return {
        'callSeq': ev.sequence_no,
        'returnSeq': ret.sequence_no if ret else None,
        'depth': ev.call_depth,
    }


class TestGetTracedFiles:
    def test_returns_filenames(self, record_func):
        db_path, run_id, stats = record_func('''
            def foo():
                return 42
            foo()
        ''')
        session = Session()
        _enter_replay(session, run_id)
        files = session.get_traced_files()
        assert isinstance(files, list)
        assert len(files) >= 1
        assert any('test_script.py' in f for f in files)

    def test_returns_distinct(self, record_func):
        db_path, run_id, stats = record_func('''
            def foo():
                return 1
            def bar():
                return 2
            foo()
            bar()
        ''')
        session = Session()
        _enter_replay(session, run_id)
        files = session.get_traced_files()
        assert len(files) == len(set(files))


class TestGetExecutionStats:
    def test_basic_stats(self, record_func):
        db_path, run_id, stats = record_func('''
            def foo():
                return 42
            foo()
            foo()
            foo()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        files = session.get_traced_files()
        script_file = [f for f in files if 'test_script.py' in f][0]

        result = session.get_execution_stats(script_file)
        assert isinstance(result, list)
        foo_stats = [r for r in result if r['functionName'] == 'foo']
        assert len(foo_stats) == 1
        assert foo_stats[0]['callCount'] == 3
        assert foo_stats[0]['exceptionCount'] == 0
        assert foo_stats[0]['firstCallSeq'] is not None
        assert foo_stats[0]['defLine'] is not None

    def test_exception_counts(self, record_func):
        db_path, run_id, stats = record_func('''
            def exploder():
                raise ValueError("boom")
            try:
                exploder()
            except ValueError:
                pass
            try:
                exploder()
            except ValueError:
                pass
        ''')
        session = Session()
        _enter_replay(session, run_id)

        files = session.get_traced_files()
        script_file = [f for f in files if 'test_script.py' in f][0]

        result = session.get_execution_stats(script_file)
        exploder_stats = [r for r in result if r['functionName'] == 'exploder']
        assert len(exploder_stats) == 1
        assert exploder_stats[0]['callCount'] == 2
        assert exploder_stats[0]['exceptionCount'] == 2

    def test_no_results_for_untraced_file(self, record_func):
        db_path, run_id, stats = record_func('''
            x = 1
        ''')
        session = Session()
        _enter_replay(session, run_id)
        result = session.get_execution_stats("/nonexistent/file.py")
        assert result == []

    def test_def_line_is_first_call_line(self, record_func):
        db_path, run_id, stats = record_func('''
            def alpha():
                return 1
            def beta():
                return 2
            alpha()
            beta()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        files = session.get_traced_files()
        script_file = [f for f in files if 'test_script.py' in f][0]

        result = session.get_execution_stats(script_file)
        alpha_stats = [r for r in result if r['functionName'] == 'alpha']
        beta_stats = [r for r in result if r['functionName'] == 'beta']
        assert len(alpha_stats) == 1
        assert len(beta_stats) == 1
        assert alpha_stats[0]['defLine'] < beta_stats[0]['defLine']


class TestGetCallChildren:
    def test_root_calls(self, record_func):
        db_path, run_id, stats = record_func('''
            def foo():
                return 42
            foo()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        children = session.get_call_children()
        assert isinstance(children, list)
        assert len(children) >= 1
        for child in children:
            assert child['depth'] == 0
            assert child['callSeq'] is not None
            assert 'functionName' in child

    def test_nested_calls(self, record_func):
        db_path, run_id, stats = record_func('''
            def inner():
                return 1
            def outer():
                inner()
                return 2
            outer()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        # Find the user's <module> call
        mod = _find_user_module_call(session, 'test_script.py')
        assert mod is not None

        # Get children of user's <module>
        children = session.get_call_children(mod['callSeq'], mod['returnSeq'])
        outer_calls = [c for c in children if c['functionName'] == 'outer']
        assert len(outer_calls) >= 1

        outer_call = outer_calls[0]
        assert outer_call['isComplete'] is True

        # Get children of outer() — should contain inner()
        inner_calls = session.get_call_children(
            outer_call['callSeq'], outer_call['returnSeq'])
        inner_found = [c for c in inner_calls if c['functionName'] == 'inner']
        assert len(inner_found) == 1

    def test_exception_call(self, record_func):
        db_path, run_id, stats = record_func('''
            def exploder():
                raise ValueError("boom")
            try:
                exploder()
            except ValueError:
                pass
        ''')
        session = Session()
        _enter_replay(session, run_id)

        mod = _find_user_module_call(session, 'test_script.py')
        assert mod is not None

        children = session.get_call_children(mod['callSeq'], mod['returnSeq'])
        exploder_calls = [c for c in children if c['functionName'] == 'exploder']
        assert len(exploder_calls) == 1
        assert exploder_calls[0]['hasException'] is True

    def test_recursive_calls(self, record_func):
        db_path, run_id, stats = record_func('''
            def countdown(n):
                if n <= 0:
                    return
                countdown(n - 1)
            countdown(3)
        ''')
        session = Session()
        _enter_replay(session, run_id)

        mod = _find_user_module_call(session, 'test_script.py')
        assert mod is not None

        # Get children of <module> — should have countdown(3)
        level1 = session.get_call_children(mod['callSeq'], mod['returnSeq'])
        countdowns = [c for c in level1 if c['functionName'] == 'countdown']
        assert len(countdowns) == 1

        # Expand countdown(3) — should have countdown(2)
        level2 = session.get_call_children(
            countdowns[0]['callSeq'], countdowns[0]['returnSeq'])
        inner_countdowns = [c for c in level2 if c['functionName'] == 'countdown']
        assert len(inner_countdowns) == 1

        # Expand countdown(2) — should have countdown(1)
        level3 = session.get_call_children(
            inner_countdowns[0]['callSeq'], inner_countdowns[0]['returnSeq'])
        deeper = [c for c in level3 if c['functionName'] == 'countdown']
        assert len(deeper) == 1

    def test_nonexistent_parent(self, record_func):
        db_path, run_id, stats = record_func('''
            x = 1
        ''')
        session = Session()
        _enter_replay(session, run_id)
        result = session.get_call_children(parent_call_seq=999999)
        assert result == []

    def test_complete_flag(self, record_func):
        db_path, run_id, stats = record_func('''
            def foo():
                return 42
            foo()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        mod = _find_user_module_call(session, 'test_script.py')
        assert mod is not None

        children = session.get_call_children(mod['callSeq'], mod['returnSeq'])
        foo_calls = [c for c in children if c['functionName'] == 'foo']
        assert len(foo_calls) >= 1
        # foo() should be complete (has a return event)
        assert foo_calls[0]['isComplete'] is True
        assert foo_calls[0]['returnSeq'] is not None

    def test_multiple_calls_same_function(self, record_func):
        db_path, run_id, stats = record_func('''
            def greet(name):
                return f"hello {name}"
            greet("alice")
            greet("bob")
            greet("charlie")
        ''')
        session = Session()
        _enter_replay(session, run_id)

        mod = _find_user_module_call(session, 'test_script.py')
        assert mod is not None

        children = session.get_call_children(mod['callSeq'], mod['returnSeq'])
        greet_calls = [c for c in children if c['functionName'] == 'greet']
        assert len(greet_calls) == 3
        # All should be complete and sequential
        for i in range(len(greet_calls) - 1):
            assert greet_calls[i]['callSeq'] < greet_calls[i + 1]['callSeq']
