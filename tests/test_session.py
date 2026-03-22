"""Phase 3: Session navigation tests.

Tests for step_into, step_over, step_out, continue_forward,
stack reconstruction, variable queries, and evaluate.
"""
import json
import pytest
from pyttd.session import Session, _infer_type, SAFE_BUILTINS
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


class TestSessionStepInto:
    def test_step_into_basic(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 1
                y = 2
                return x + y
            foo()
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)

        result = session.step_into()
        assert result["seq"] > first_seq
        assert result["reason"] == "step"

    def test_step_into_enters_function(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Verify foo exists in the recording (step_into would need hundreds of
        # steps due to internal frames being recorded — known limitation)
        foo_frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'foo') &
            (ExecutionFrames.frame_event == 'line'))
        assert foo_frame is not None, "foo should be recorded"

        # Jump near foo and verify step_into reaches it
        pre_foo = (ExecutionFrames.select()
                   .where((ExecutionFrames.run_id == run_id) &
                          (ExecutionFrames.frame_event == 'line') &
                          (ExecutionFrames.sequence_no < foo_frame.sequence_no))
                   .order_by(ExecutionFrames.sequence_no.desc())
                   .limit(1).first())
        if pre_foo:
            session.current_frame_seq = pre_foo.sequence_no
        result = session.step_into()
        assert result.get("function_name") == "foo"

    def test_step_into_reaches_end(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        for _ in range(1000):
            result = session.step_into()
            if result["reason"] == "end":
                break
        assert result["reason"] == "end"
        assert result["seq"] == session.last_line_seq


class TestSessionStepOver:
    def test_step_over_skips_calls(self, record_func):
        db_path, run_id, stats = record_func("""\
            def inner():
                a = 1
                b = 2
                return a + b
            def outer():
                x = inner()
                y = x + 1
                return y
            outer()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a frame in outer
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'outer') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no)
                      .limit(1))
        assert frames, "Should find at least one line event in outer"
        session.current_frame_seq = frames[0].sequence_no
        outer_depth = frames[0].call_depth

        result = session.step_over()
        assert result["reason"] in ("step", "end")
        # Should stay at same or shallower depth
        if result["reason"] == "step":
            stepped_frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == result["seq"]))
            assert stepped_frame is not None
            assert stepped_frame.call_depth <= outer_depth


class TestSessionStepOut:
    def test_step_out_exits_function(self, record_func):
        db_path, run_id, stats = record_func("""\
            def inner():
                x = 1
                return x
            def outer():
                y = inner()
                z = y + 1
                return z
            outer()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Navigate to inner
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'inner') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no)
                      .limit(1))
        assert frames, "Should find at least one line event in inner"
        session.current_frame_seq = frames[0].sequence_no
        session.current_stack = session._build_stack_at(frames[0].sequence_no)
        inner_depth = frames[0].call_depth

        result = session.step_out()
        assert result["reason"] in ("step", "end")
        if result["reason"] == "step":
            stepped_frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == result["seq"]))
            assert stepped_frame is not None
            assert stepped_frame.call_depth < inner_depth

    def test_step_out_at_top_level(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a depth-0 frame
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.call_depth == 0) &
            (ExecutionFrames.frame_event == 'line'))
        assert frame is not None, "Should find a depth-0 line event"
        session.current_frame_seq = frame.sequence_no
        session.current_stack = session._build_stack_at(frame.sequence_no)
        result = session.step_out()
        assert result["reason"] == "end"


class TestSessionContinue:
    def test_continue_no_breakpoints(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
            y = 2
            z = 3
        """)
        session = Session()
        _enter_replay(session, run_id)

        result = session.continue_forward()
        assert result["reason"] == "end"

    def test_continue_with_breakpoint(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                a = 1
                b = 2
                c = 3
                return a + b + c
            foo()
        """)
        session = Session()
        first_seq = _enter_replay(session, run_id)

        # Find a line in foo to set as breakpoint
        foo_lines = list(ExecutionFrames.select()
                         .where((ExecutionFrames.run_id == run_id) &
                                (ExecutionFrames.function_name == 'foo') &
                                (ExecutionFrames.frame_event == 'line'))
                         .order_by(ExecutionFrames.sequence_no))
        assert len(foo_lines) >= 2, "Should find at least 2 line events in foo"
        target = foo_lines[1]  # second line in foo
        session.set_breakpoints([{"file": target.filename, "line": target.line_no}])
        result = session.continue_forward()
        assert result["reason"] == "breakpoint"
        assert result["seq"] == target.sequence_no

    def test_continue_with_exception_filter_raised(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                raise ValueError("test")
            try:
                foo()
            except ValueError:
                pass
        """)
        session = Session()
        _enter_replay(session, run_id)
        session.set_exception_filters(["raised"])

        result = session.continue_forward()
        assert result["reason"] == "exception", "Should hit exception with 'raised' filter"
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == result["seq"]))
        assert frame is not None
        # continue_forward snaps exception events to the nearest line event
        assert frame.frame_event == 'line'


class TestSessionStack:
    def test_stack_single_frame(self, record_func):
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        session = Session()
        _enter_replay(session, run_id)

        stack = session.get_stack_at(session.current_frame_seq)
        assert len(stack) >= 1
        assert "name" in stack[0]
        assert "file" in stack[0]
        assert "line" in stack[0]

    def test_stack_nested_calls(self, record_func):
        db_path, run_id, stats = record_func("""\
            def inner():
                x = 1
                return x
            def outer():
                y = inner()
                return y
            outer()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Navigate to inside inner
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'inner') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no)
                      .limit(1))
        assert frames, "Should find at least one line event in inner"
        session.current_frame_seq = frames[0].sequence_no
        session.current_stack = session._build_stack_at(frames[0].sequence_no)
        stack = session.get_stack_at(frames[0].sequence_no)
        # Stack should have at least inner + outer + module
        assert len(stack) >= 2
        assert stack[0]["name"] == "inner"


class TestSessionVariables:
    def test_variables_basic(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 42
                y = "hello"
                return x
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find a line in foo where x is set
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        # Find a frame where locals_snapshot has 'x'
        found = False
        for f in frames:
            if f.locals_snapshot and '"x"' in f.locals_snapshot:
                variables = session.get_variables_at(f.sequence_no)
                names = [v["name"] for v in variables]
                assert "x" in names
                x_var = next(v for v in variables if v["name"] == "x")
                assert x_var["value"] == "42"
                assert x_var["variablesReference"] == 0
                found = True
                break
        assert found, "No frame found with 'x' in locals_snapshot"

    def test_variables_empty_for_missing_frame(self, record_func):
        db_path, run_id, stats = record_func("x = 1\n")
        session = Session()
        _enter_replay(session, run_id)
        variables = session.get_variables_at(999999)
        assert variables == []


class TestSessionEvaluate:
    def test_evaluate_known_variable(self, record_func):
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)

        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot and '"x"' in f.locals_snapshot:
                result = session.evaluate_at(f.sequence_no, "x", "hover")
                assert result["result"] == "42"
                found = True
                break
        assert found, "No frame found with 'x' in locals_snapshot"

    def test_evaluate_repl_context(self, record_func):
        """REPL context now supports eval — should return value or error, not static message."""
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)
        # Find a frame where x is set
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot and '"x"' in f.locals_snapshot:
                result = session.evaluate_at(f.sequence_no, "x", "repl")
                assert result["result"] == "42"
                found = True
                break
        assert found, "No frame found with 'x'"

    def test_evaluate_repl_expression(self, record_func):
        """REPL supports full expressions with builtins."""
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 3
                y = 5
                return x + y
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot and '"x"' in f.locals_snapshot and '"y"' in f.locals_snapshot:
                result = session.evaluate_at(f.sequence_no, "x + y", "repl")
                assert result["result"] == "8"
                found = True
                break
        assert found, "No frame found with both x and y"

    def test_evaluate_repl_error(self, record_func):
        """REPL eval errors return descriptive message."""
        db_path, run_id, stats = record_func("x = 1\n")
        session = Session()
        _enter_replay(session, run_id)
        # Eval at a frame that exists but expression fails
        first_line = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.frame_event == 'line'))
        if first_line:
            result = session.evaluate_at(first_line.sequence_no, "__import__('os')", "repl")
            assert "Error" in result["result"] or "<not available>" in result["result"]

    def test_evaluate_arithmetic(self, record_func):
        """Hover/watch supports arithmetic expressions."""
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 3
                y = 5
                return x + y
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot and '"x"' in f.locals_snapshot and '"y"' in f.locals_snapshot:
                result = session.evaluate_at(f.sequence_no, "x + y", "hover")
                assert result["result"] == "8"
                found = True
                break
        assert found

    def test_evaluate_builtin_len(self, record_func):
        """Eval supports len() and other builtins."""
        db_path, run_id, stats = record_func("""\
            def foo():
                msg = "hello"
                return msg
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot and '"msg"' in f.locals_snapshot:
                result = session.evaluate_at(f.sequence_no, "len(msg)", "hover")
                assert result["result"] == "5"
                found = True
                break
        assert found

    def test_evaluate_isinstance(self, record_func):
        """Eval supports isinstance()."""
        db_path, run_id, stats = record_func("""\
            def foo():
                x = 42
                return x
            foo()
        """)
        session = Session()
        _enter_replay(session, run_id)
        frames = list(ExecutionFrames.select()
                      .where((ExecutionFrames.run_id == run_id) &
                             (ExecutionFrames.function_name == 'foo') &
                             (ExecutionFrames.frame_event == 'line'))
                      .order_by(ExecutionFrames.sequence_no))
        found = False
        for f in frames:
            if f.locals_snapshot and '"x"' in f.locals_snapshot:
                result = session.evaluate_at(f.sequence_no, "isinstance(x, int)", "hover")
                assert result["result"] == "True"
                found = True
                break
        assert found

    def test_evaluate_no_import(self, record_func):
        """__import__ is NOT in SAFE_BUILTINS."""
        db_path, run_id, stats = record_func("x = 1\n")
        session = Session()
        _enter_replay(session, run_id)
        first_line = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.frame_event == 'line'))
        if first_line:
            result = session.evaluate_at(first_line.sequence_no, "__import__('os')", "hover")
            assert result["result"] == "<not available>"


class TestInferType:
    def test_int(self):
        assert _infer_type("42") == "int"

    def test_float(self):
        assert _infer_type("3.14") == "float"

    def test_bool(self):
        assert _infer_type("True") == "bool"
        assert _infer_type("False") == "bool"

    def test_none(self):
        assert _infer_type("None") == "NoneType"

    def test_list(self):
        assert _infer_type("[1, 2, 3]") == "list"

    def test_dict(self):
        assert _infer_type("{'a': 1}") == "dict"

    def test_string(self):
        assert _infer_type("hello") == "str"


class TestSafeBuiltins:
    def test_safe_builtins_has_essentials(self):
        for name in ['len', 'str', 'int', 'float', 'bool', 'list', 'dict',
                      'tuple', 'set', 'type', 'isinstance', 'abs', 'min',
                      'max', 'sum', 'sorted', 'repr', 'round']:
            assert name in SAFE_BUILTINS, f"{name} should be in SAFE_BUILTINS"

    def test_safe_builtins_has_iteration(self):
        for name in ['any', 'all', 'enumerate', 'zip', 'map', 'filter',
                      'reversed', 'iter', 'next']:
            assert name in SAFE_BUILTINS, f"{name} should be in SAFE_BUILTINS"

    def test_safe_builtins_has_attribute_access(self):
        assert 'hasattr' in SAFE_BUILTINS
        assert 'getattr' in SAFE_BUILTINS

    def test_safe_builtins_excludes_dangerous(self):
        for name in ['eval', 'exec', 'compile', '__import__', 'open',
                      'input', 'breakpoint', 'exit', 'quit',
                      'globals', 'locals', 'vars', 'setattr', 'delattr']:
            assert name not in SAFE_BUILTINS, f"{name} should NOT be in SAFE_BUILTINS"

    def test_safe_builtins_has_constants(self):
        assert SAFE_BUILTINS['True'] is True
        assert SAFE_BUILTINS['False'] is False
        assert SAFE_BUILTINS['None'] is None
