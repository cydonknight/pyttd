"""Deep functional tests: edge cases that stress specific components."""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.runner import Runner
from pyttd.session import Session
from pyttd.replay import ReplayController
from pyttd.models import storage
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.checkpoints import Checkpoint
from pyttd.models.io_events import IOEvent
from pyttd.models.timeline import get_timeline_summary
import pyttd_native

passed = 0
failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  [PASS] {msg}")

def fail(msg):
    global failed
    failed += 1
    print(f"  [FAIL] {msg}")

def check(condition, msg):
    if condition:
        ok(msg)
    else:
        fail(msg)

def record_script(script_content, tmp_dir, name="test", checkpoint_interval=0):
    """Helper: write script, record it, return (db_path, run_id, stats)."""
    import textwrap
    script_path = os.path.join(tmp_dir, f"{name}.py")
    with open(script_path, 'w') as f:
        f.write(textwrap.dedent(script_content))

    db_path = os.path.join(tmp_dir, f"{name}.pyttd.db")
    storage.delete_db_files(db_path)

    config = PyttdConfig(checkpoint_interval=checkpoint_interval)
    recorder = Recorder(config)
    runner = Runner()
    recorder.start(db_path, script_path=script_path)
    try:
        runner.run_script(script_path, tmp_dir)
    except BaseException:
        pass
    stats = recorder.stop()
    run_id = recorder.run_id
    recorder.kill_checkpoints()
    # Don't close DB — needed for replay
    return db_path, run_id, stats, recorder


def cleanup_recorder(recorder):
    storage.close_db()
    from pyttd.models.base import db
    db.init(None)


# ==========================================
# TEST SUITE
# ==========================================
import tempfile

def test_repr_reentrancy():
    """Test: __repr__ that raises or calls recorded code doesn't crash."""
    print("\n--- Test: __repr__ reentrancy ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            class BadRepr:
                def __repr__(self):
                    raise RuntimeError("repr exploded")

            class NestedRepr:
                def __repr__(self):
                    return str({"nested": True, "value": 42})

            obj1 = BadRepr()
            obj2 = NestedRepr()
            x = 1  # locals should still be captured here
        ''', tmp, "repr_test")

        check(stats['frame_count'] > 0, f"Frames recorded: {stats['frame_count']}")
        check(stats['dropped_frames'] == 0, f"No dropped frames")

        # Verify locals at the last line are parseable
        frames = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no.desc()).limit(5))

        valid_json_count = 0
        for f in frames:
            if f.locals_snapshot:
                try:
                    data = json.loads(f.locals_snapshot)
                    valid_json_count += 1
                except json.JSONDecodeError:
                    fail(f"Invalid JSON at seq={f.sequence_no}: {f.locals_snapshot[:100]}")
        check(valid_json_count > 0, f"Valid JSON locals: {valid_json_count}")
        cleanup_recorder(rec)


def test_unicode_locals():
    """Test: unicode strings in locals are properly serialized."""
    print("\n--- Test: Unicode locals ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            emoji = "Hello 🌍🎉"
            japanese = "こんにちは世界"
            backslash = 'C:\\\\Users\\\\test\\\\"quoted\\\\"'
            newlines = "line1\\nline2\\nline3"
            tabs = "col1\\tcol2\\tcol3"
            null_char = "before\\x00after"
            x = 42
        ''', tmp, "unicode_test")

        check(stats['frame_count'] > 0, f"Frames recorded: {stats['frame_count']}")

        # Find the frame where x=42 is defined
        last_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no.desc()).first())

        if last_line and last_line.locals_snapshot:
            try:
                data = json.loads(last_line.locals_snapshot)
                check('emoji' in data, f"emoji in locals: {list(data.keys())}")
                check('japanese' in data, f"japanese in locals")
                check('backslash' in data, f"backslash in locals")
                check('newlines' in data, f"newlines in locals")
                ok(f"All unicode locals valid JSON, keys={list(data.keys())}")
            except json.JSONDecodeError as e:
                fail(f"JSON decode failed: {e}\n  snapshot: {last_line.locals_snapshot[:200]}")
        else:
            fail("No line frames found")
        cleanup_recorder(rec)


def test_large_locals():
    """Test: very large locals don't crash (may be truncated)."""
    print("\n--- Test: Large locals ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            big_list = list(range(10000))
            big_str = "x" * 50000
            big_dict = {f"key_{i}": i for i in range(1000)}
            marker = "DONE"
        ''', tmp, "large_test")

        check(stats['frame_count'] > 0, f"Frames recorded: {stats['frame_count']}")
        check(stats['dropped_frames'] == 0, "No dropped frames")

        # Check that JSON is still valid even if truncated
        frames = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no.desc()).limit(3))

        for f in frames:
            if f.locals_snapshot:
                try:
                    data = json.loads(f.locals_snapshot)
                    ok(f"Large locals valid JSON at seq={f.sequence_no}, "
                       f"snapshot_len={len(f.locals_snapshot)}")
                except json.JSONDecodeError:
                    fail(f"Invalid JSON at seq={f.sequence_no}: "
                         f"{f.locals_snapshot[:100]}...{f.locals_snapshot[-50:]}")
        cleanup_recorder(rec)


def test_io_hooks():
    """Test: I/O hooks record and the values are stored correctly."""
    print("\n--- Test: I/O hooks ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            import time
            import random
            import os

            t1 = time.time()
            t2 = time.monotonic()
            t3 = time.perf_counter()
            r1 = random.random()
            r2 = random.randint(1, 100)
            b1 = os.urandom(16)
            marker = "done"
        ''', tmp, "io_test")

        check(stats['frame_count'] > 0, f"Frames recorded: {stats['frame_count']}")

        io_events = list(IOEvent.select()
            .where(IOEvent.run_id == run_id)
            .order_by(IOEvent.sequence_no, IOEvent.io_sequence))

        check(len(io_events) >= 6, f"IO events: {len(io_events)} (expected >= 6)")

        func_names = [e.function_name for e in io_events]
        check("time.time" in func_names, "time.time recorded")
        check("time.monotonic" in func_names, "time.monotonic recorded")
        check("time.perf_counter" in func_names, "time.perf_counter recorded")
        check("random.random" in func_names, "random.random recorded")
        check("random.randint" in func_names, "random.randint recorded")
        check("os.urandom" in func_names, "os.urandom recorded")

        # Verify return_value is non-empty bytes
        for e in io_events:
            check(len(e.return_value) > 0,
                  f"{e.function_name} return_value: {len(e.return_value)} bytes")
        cleanup_recorder(rec)


def test_session_navigation_edge_cases():
    """Test: session navigation edge cases."""
    print("\n--- Test: Session navigation edge cases ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            def a():
                return b()

            def b():
                return c()

            def c():
                x = 1
                y = 2
                return x + y

            result = a()

            for i in range(3):
                pass
        ''', tmp, "nav_test")

        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Test step_over — should skip function calls
        result = session.step_over()
        check(result.get("reason") != "error", f"step_over: seq={result.get('seq')}")

        # Navigate into c() and check stack
        while True:
            result = session.step_into()
            if result.get("reason") == "end":
                break
            frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == result["seq"]))
            if frame and "c" in frame.function_name and frame.frame_event == 'line':
                break

        if result.get("reason") != "end":
            stack = session.get_stack_at(result["seq"])
            stack_names = [f["name"] for f in stack]
            check(len(stack) >= 3, f"Stack depth in c(): {len(stack)}, names={stack_names}")

            # step_out from c should go to b's caller
            out_result = session.step_out()
            check(out_result.get("reason") != "error",
                  f"step_out from c: seq={out_result.get('seq')}")

        # Test goto_frame with line-snapping
        # Find a 'call' event (not a 'line')
        call_event = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'call'))
            .order_by(ExecutionFrames.sequence_no).first())
        if call_event:
            result = session.goto_frame(call_event.sequence_no)
            # Should snap to a nearby line event
            check(result.get("reason") == "goto",
                  f"goto_frame(call event) snapped: seq={result.get('seq')}")
            if "seq" in result:
                snapped_frame = ExecutionFrames.get_or_none(
                    (ExecutionFrames.run_id == run_id) &
                    (ExecutionFrames.sequence_no == result["seq"]))
                # The snapped target should be a 'line' event
                # (goto_frame snaps non-line events to nearest line)
                check(snapped_frame is not None and snapped_frame.frame_event == 'line',
                      f"Snapped to line event (was call), event={snapped_frame.frame_event if snapped_frame else 'None'}")

        # Test restart_frame
        # Navigate into a function first
        session.goto_frame(first_line.sequence_no)
        while True:
            result = session.step_into()
            if result.get("reason") == "end":
                break
            frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == result["seq"]))
            if frame and frame.call_depth > 0:
                # We're inside a function — restart this frame
                restart_result = session.restart_frame(result["seq"])
                check(restart_result.get("reason") == "goto",
                      f"restart_frame: reason={restart_result.get('reason')}")
                break

        # Test continue_forward to end
        while True:
            result = session.continue_forward()
            if result.get("reason") == "end":
                break
        check(result.get("reason") == "end", "continue to end")

        # Test step_back from beginning
        session.goto_frame(first_line.sequence_no)
        result = session.step_back()
        check(result.get("reason") == "start",
              f"step_back from start: reason={result.get('reason')}")

        cleanup_recorder(rec)


def test_exception_flow():
    """Test: exception events are correctly recorded and navigable."""
    print("\n--- Test: Exception flow ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            def throws():
                raise ValueError("test error")

            def catches():
                try:
                    throws()
                except ValueError:
                    return "caught"

            result = catches()

            # Uncaught exception propagation
            def nested_throw():
                def inner():
                    raise RuntimeError("inner boom")
                inner()

            try:
                nested_throw()
            except RuntimeError:
                pass
        ''', tmp, "exc_test")

        # Verify exception events exist
        exceptions = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'exception'))
            .order_by(ExecutionFrames.sequence_no))
        check(len(exceptions) >= 2, f"Exception events: {len(exceptions)}")

        # Verify exception_unwind events
        unwinds = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'exception_unwind'))
            .order_by(ExecutionFrames.sequence_no))
        check(len(unwinds) >= 2, f"Exception_unwind events: {len(unwinds)}")

        # Verify exception_unwind has correct depth
        for uw in unwinds:
            # Find the matching call event
            call = (ExecutionFrames.select()
                .where((ExecutionFrames.run_id == run_id) &
                       (ExecutionFrames.frame_event == 'call') &
                       (ExecutionFrames.call_depth == uw.call_depth) &
                       (ExecutionFrames.sequence_no < uw.sequence_no))
                .order_by(ExecutionFrames.sequence_no.desc()).first())
            check(call is not None,
                  f"exception_unwind seq={uw.sequence_no} depth={uw.call_depth} "
                  f"has matching call at seq={call.sequence_no if call else 'None'}")

        # Navigate with exception filter
        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)
        session.set_exception_filters(["raised"])

        result = session.continue_forward()
        check(result.get("reason") == "exception",
              f"continue with raised filter: reason={result.get('reason')}")

        cleanup_recorder(rec)


def test_execution_stats_accuracy():
    """Test: Phase 6 get_execution_stats returns accurate counts."""
    print("\n--- Test: Execution stats accuracy ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            def normal():
                return 1

            def sometimes_fails(x):
                if x < 0:
                    raise ValueError("negative")
                return x

            # Exact call counts
            for _ in range(7):
                normal()

            for x in [1, 2, -1, 3, -2, 4, -3]:
                try:
                    sometimes_fails(x)
                except ValueError:
                    pass
        ''', tmp, "stats_test")

        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        files = session.get_traced_files()
        target = [f for f in files if "stats_test" in f][0]

        result = session.get_execution_stats(target)
        normal_stats = [r for r in result if r['functionName'] == 'normal']
        sf_stats = [r for r in result if r['functionName'] == 'sometimes_fails']

        check(len(normal_stats) == 1, "normal() in stats")
        check(normal_stats[0]['callCount'] == 7,
              f"normal() calls={normal_stats[0]['callCount']} (expected 7)")
        check(normal_stats[0]['exceptionCount'] == 0,
              f"normal() exceptions={normal_stats[0]['exceptionCount']} (expected 0)")

        check(len(sf_stats) == 1, "sometimes_fails() in stats")
        check(sf_stats[0]['callCount'] == 7,
              f"sometimes_fails() calls={sf_stats[0]['callCount']} (expected 7)")
        check(sf_stats[0]['exceptionCount'] == 3,
              f"sometimes_fails() exceptions={sf_stats[0]['exceptionCount']} (expected 3)")

        cleanup_recorder(rec)


def test_call_tree_correctness():
    """Test: Phase 6 get_call_children returns correct tree structure."""
    print("\n--- Test: Call tree correctness ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            def leaf():
                return 1

            def branch():
                leaf()
                leaf()
                return 2

            def root():
                branch()
                leaf()
                branch()
                return 3

            root()
        ''', tmp, "tree_test")

        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Root level: should have <module>
        root_children = session.get_call_children()
        mod = [c for c in root_children if c['functionName'] == '<module>']
        check(len(mod) == 1, "One <module> at root")

        # Module children: should have root()
        mod_children = session.get_call_children(mod[0]['callSeq'], mod[0]['returnSeq'])
        root_calls = [c for c in mod_children if c['functionName'] == 'root']
        check(len(root_calls) == 1, f"One root() call in <module>")

        # root() children: should have branch, leaf, branch
        if root_calls:
            root_kids = session.get_call_children(
                root_calls[0]['callSeq'], root_calls[0]['returnSeq'])
            names = [c['functionName'] for c in root_kids]
            check(names == ['branch', 'leaf', 'branch'],
                  f"root() children: {names} (expected ['branch', 'leaf', 'branch'])")

            # First branch() children: should have leaf, leaf
            if root_kids:
                branch1 = root_kids[0]
                b1_kids = session.get_call_children(
                    branch1['callSeq'], branch1['returnSeq'])
                b1_names = [c['functionName'] for c in b1_kids]
                check(b1_names == ['leaf', 'leaf'],
                      f"branch() children: {b1_names} (expected ['leaf', 'leaf'])")

                # leaf() children: should be empty
                if b1_kids:
                    leaf_kids = session.get_call_children(
                        b1_kids[0]['callSeq'], b1_kids[0]['returnSeq'])
                    check(len(leaf_kids) == 0,
                          f"leaf() has no children: {len(leaf_kids)}")

        cleanup_recorder(rec)


def test_timeline_with_breakpoints():
    """Test: Timeline summary correctly marks breakpoint buckets."""
    print("\n--- Test: Timeline with breakpoints ---")
    with tempfile.TemporaryDirectory() as tmp:
        db_path, run_id, stats, rec = record_script('''
            def target_func():
                x = 1
                y = 2
                return x + y

            for _ in range(10):
                target_func()
        ''', tmp, "timeline_test")

        files = list(ExecutionFrames.select(ExecutionFrames.filename)
            .where(ExecutionFrames.run_id == run_id).distinct())
        target_file = [f.filename for f in files if "timeline_test" in f.filename][0]

        total = ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id).count()

        # Timeline with breakpoint on line 3 (y = 2)
        breakpoints = [{"file": target_file, "line": 3}]
        buckets = get_timeline_summary(run_id, 0, total, 10, breakpoints=breakpoints)
        has_bp = [b for b in buckets if b['hasBreakpoint']]
        check(len(has_bp) > 0, f"Breakpoint marked in {len(has_bp)} buckets")

        # Timeline without breakpoints
        buckets_no_bp = get_timeline_summary(run_id, 0, total, 10)
        has_bp_none = [b for b in buckets_no_bp if b['hasBreakpoint']]
        check(len(has_bp_none) == 0, "No breakpoints when none set")

        cleanup_recorder(rec)


# ==========================================
# RUN ALL TESTS
# ==========================================
if __name__ == "__main__":
    print("=" * 60)
    print("DEEP FUNCTIONAL TESTS")
    print("=" * 60)

    test_repr_reentrancy()
    test_unicode_locals()
    test_large_locals()
    test_io_hooks()
    test_session_navigation_edge_cases()
    test_exception_flow()
    test_execution_stats_accuracy()
    test_call_tree_correctness()
    test_timeline_with_breakpoints()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)
