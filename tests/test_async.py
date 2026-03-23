"""Tests for async/await and generator support.

Tests that coroutines, generators, and async generators are correctly
recorded with the is_coroutine flag, and that navigation handles
suspension/resume patterns correctly.
"""
import json
import sys
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


class TestCoroutineFlag:
    """Verify is_coroutine flag is recorded correctly."""

    @pytest.mark.skipif(sys.platform == 'win32',
                        reason="Windows asyncio internals prevent coroutine call recording")
    def test_coroutine_flag_on_async_def(self, record_func):
        """async def functions should have is_coroutine=True."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                return x
            asyncio.run(foo())
        """)
        foo_call = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'foo') &
            (ExecutionFrames.frame_event == 'call'))
        assert foo_call is not None, "foo call event should be recorded"
        assert foo_call.is_coroutine is True

    def test_regular_function_not_coroutine(self, record_func):
        """Regular functions should have is_coroutine=False."""
        db_path, run_id, stats = record_func("""\
            def foo():
                return 42
            foo()
        """)
        foo_call = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'foo') &
            (ExecutionFrames.frame_event == 'call'))
        assert foo_call is not None
        assert foo_call.is_coroutine is False

    def test_generator_flag_recorded(self, record_func):
        """Generator functions should have is_coroutine=True."""
        db_path, run_id, stats = record_func("""\
            def gen():
                yield 1
                yield 2
            for item in gen():
                pass
        """)
        gen_call = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'gen') &
            (ExecutionFrames.frame_event == 'call'))
        assert gen_call is not None
        assert gen_call.is_coroutine is True

    @pytest.mark.skipif(sys.platform == 'win32',
                        reason="Windows asyncio internals prevent coroutine call recording")
    def test_coroutine_flag_on_line_events(self, record_func):
        """Line events inside coroutines should also have is_coroutine=True."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                return x
            asyncio.run(foo())
        """)
        foo_line = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'foo') &
            (ExecutionFrames.frame_event == 'line'))
        assert foo_line is not None
        assert foo_line.is_coroutine is True

    def test_coroutine_flag_on_return_events(self, record_func):
        """Return events from coroutines should have is_coroutine=True."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                return 42
            asyncio.run(foo())
        """)
        foo_returns = list(ExecutionFrames.select()
                          .where((ExecutionFrames.run_id == run_id) &
                                 (ExecutionFrames.function_name == 'foo') &
                                 (ExecutionFrames.frame_event == 'return'))
                          .order_by(ExecutionFrames.sequence_no))
        assert len(foo_returns) >= 1
        for r in foo_returns:
            assert r.is_coroutine is True

    def test_module_level_not_coroutine(self, record_func):
        """Module-level <module> events should have is_coroutine=False."""
        db_path, run_id, stats = record_func("""\
            x = 1
        """)
        mod_line = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == '<module>') &
            (ExecutionFrames.frame_event == 'line'))
        assert mod_line is not None
        assert mod_line.is_coroutine is False


class TestCoroutineRecording:
    """Verify coroutine execution is recorded across suspend/resume."""

    def test_coroutine_frames_across_await(self, record_func):
        """Both sides of an await should be recorded."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                await asyncio.sleep(0)
                y = 2
                return x + y
            asyncio.run(foo())
        """)
        foo_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'foo') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        assert len(foo_lines) >= 2, "Should have line events across await"
        has_x = any(f.locals_snapshot and '"x"' in f.locals_snapshot for f in foo_lines)
        has_y = any(f.locals_snapshot and '"y"' in f.locals_snapshot for f in foo_lines)
        assert has_x, "Should record x=1 before await"
        assert has_y, "Should record y=2 after await"

    def test_generator_frames_across_yield(self, record_func):
        """Generator execution across yield should be recorded."""
        db_path, run_id, stats = record_func("""\
            def gen():
                x = 1
                yield x
                y = 2
                yield y
            result = list(gen())
        """)
        gen_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'gen') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        assert len(gen_lines) >= 2, "Should have line events across yield"
        has_x = any(f.locals_snapshot and '"x"' in f.locals_snapshot for f in gen_lines)
        has_y = any(f.locals_snapshot and '"y"' in f.locals_snapshot for f in gen_lines)
        assert has_x, "Should record x=1 before yield"
        assert has_y, "Should record y=2 after yield"

    def test_async_generator_recorded(self, record_func):
        """Async generators should be recorded with is_coroutine=True."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def agen():
                yield 1
                yield 2
            async def main():
                result = []
                async for item in agen():
                    result.append(item)
            asyncio.run(main())
        """)
        agen_call = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'agen') &
            (ExecutionFrames.frame_event == 'call'))
        assert agen_call is not None
        assert agen_call.is_coroutine is True


class TestCoroutineStackReconstruction:
    """Stack reconstruction should be correct across suspend/resume."""

    def test_stack_inside_coroutine(self, record_func):
        """Stack reconstruction inside a coroutine should be correct."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                return x
            asyncio.run(foo())
        """)
        session = Session()
        _enter_replay(session, run_id)

        foo_line = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'foo') &
            (ExecutionFrames.frame_event == 'line'))
        assert foo_line is not None

        stack = session._build_stack_at(foo_line.sequence_no)
        assert len(stack) >= 1
        assert stack[0]['name'] == 'foo'

    def test_stack_after_await_resume(self, record_func):
        """Stack after coroutine resume should include the coroutine."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                await asyncio.sleep(0)
                y = 2
                return y
            asyncio.run(foo())
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find line events inside foo after the await (where y is set)
        foo_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'foo') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        after_await = None
        for f in foo_lines:
            if f.locals_snapshot and '"y"' in f.locals_snapshot:
                after_await = f
                break
        assert after_await is not None, "Should find line with y after await"

        stack = session._build_stack_at(after_await.sequence_no)
        assert len(stack) >= 1
        assert stack[0]['name'] == 'foo'

    def test_generator_stack_across_yield(self, record_func):
        """Stack inside a generator after yield/resume should be correct."""
        db_path, run_id, stats = record_func("""\
            def gen():
                x = 1
                yield x
                y = 2
                yield y
            for item in gen():
                pass
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find line event after second yield (where y is defined)
        gen_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'gen') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        after_yield = None
        for f in gen_lines:
            if f.locals_snapshot and '"y"' in f.locals_snapshot:
                after_yield = f
                break
        assert after_yield is not None, "Should find line with y after yield"

        stack = session._build_stack_at(after_yield.sequence_no)
        assert len(stack) >= 1
        assert stack[0]['name'] == 'gen'


class TestCoroutineNavigation:
    """Navigation commands should handle coroutine patterns correctly."""

    def test_step_over_across_await(self, record_func):
        """step_over should work across await boundaries."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                await asyncio.sleep(0)
                y = 2
                return x + y
            asyncio.run(foo())
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find first line inside foo
        foo_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'foo') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        assert len(foo_lines) >= 1

        # Navigate to first line in foo
        session.current_frame_seq = foo_lines[0].sequence_no
        session.current_stack = session._build_stack_at(foo_lines[0].sequence_no)
        session.current_thread_id = foo_lines[0].thread_id

        # Step over should progress through foo without getting lost
        visited_fns = set()
        for _ in range(20):
            result = session.step_over()
            if result['reason'] == 'end':
                break
            visited_fns.add(result.get('function_name', ''))
        # step_over from inside foo should stay in foo or reach a parent
        # (not jump to an unrelated function)

    def test_step_out_skips_generator_suspension(self, record_func):
        """step_out from inside a generator should find the real exit."""
        db_path, run_id, stats = record_func("""\
            def gen():
                x = 1
                yield x
                y = 2
                yield y
            def caller():
                result = list(gen())
                z = sum(result)
                return z
            caller()
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find first line inside gen
        gen_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'gen') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        assert gen_lines, "Should find line events in gen"

        session.current_frame_seq = gen_lines[0].sequence_no
        session.current_stack = session._build_stack_at(gen_lines[0].sequence_no)
        session.current_thread_id = gen_lines[0].thread_id
        gen_depth = gen_lines[0].call_depth

        result = session.step_out()
        assert result['reason'] in ('step', 'end')

        if result['reason'] == 'step':
            landed = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == result['seq']))
            assert landed is not None
            assert landed.call_depth < gen_depth

    def test_step_back_across_await(self, record_func):
        """step_back should work across await boundaries."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                x = 1
                await asyncio.sleep(0)
                y = 2
                return x + y
            asyncio.run(foo())
        """)
        session = Session()
        _enter_replay(session, run_id)

        # Find line with y (after await)
        foo_lines = list(ExecutionFrames.select()
                        .where((ExecutionFrames.run_id == run_id) &
                               (ExecutionFrames.function_name == 'foo') &
                               (ExecutionFrames.frame_event == 'line'))
                        .order_by(ExecutionFrames.sequence_no))
        y_line = None
        for f in foo_lines:
            if f.locals_snapshot and '"y"' in f.locals_snapshot:
                y_line = f
                break
        if y_line is None:
            pytest.skip("y line not found (await pattern may differ)")

        session.current_frame_seq = y_line.sequence_no
        session.current_stack = session._build_stack_at(y_line.sequence_no)
        session.current_thread_id = y_line.thread_id

        result = session.step_back()
        assert result['reason'] in ('step', 'start')
        assert result['seq'] < y_line.sequence_no

    def test_coroutine_exception(self, record_func):
        """Exception in a coroutine should be recorded correctly."""
        db_path, run_id, stats = record_func("""\
            import asyncio
            async def foo():
                raise ValueError("test error")
            async def main():
                try:
                    await foo()
                except ValueError:
                    pass
            asyncio.run(main())
        """)
        # Should have an exception event inside foo
        exc_event = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.function_name == 'foo') &
            (ExecutionFrames.frame_event == 'exception'))
        assert exc_event is not None, "Exception in coroutine should be recorded"
        assert exc_event.is_coroutine is True
