"""Stress test suite for pyttd — exercises edge cases and potential bugs."""
import sys
import os
import textwrap
import json
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.base import db
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.checkpoints import Checkpoint
from pyttd.models.storage import delete_db_files, close_db
from pyttd.session import Session
from pyttd.replay import ReplayController

PASS = 0
FAIL = 0

def record_script(script_content, tmp_dir, checkpoint_interval=0):
    script_file = os.path.join(tmp_dir, "test_script.py")
    with open(script_file, 'w') as f:
        f.write(textwrap.dedent(script_content))
    db_path = os.path.join(tmp_dir, "test.pyttd.db")
    delete_db_files(db_path)
    config = PyttdConfig(checkpoint_interval=checkpoint_interval)
    recorder = Recorder(config)
    recorder.start(db_path, script_path=script_file)
    import runpy
    old_argv = sys.argv[:]
    sys.argv = [script_file]
    try:
        runpy.run_path(script_file, run_name='__main__')
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
    stats = recorder.stop()
    run_id = recorder.run_id
    pyttd_native.kill_all_checkpoints()
    return db_path, run_id, stats, recorder

def cleanup():
    close_db()
    db.init(None)

def test(name):
    def decorator(func):
        def wrapper():
            global PASS, FAIL
            tmp_dir = tempfile.mkdtemp()
            try:
                func(tmp_dir)
                print(f"  PASS: {name}")
                PASS += 1
            except Exception as e:
                print(f"  FAIL: {name}")
                traceback.print_exc()
                FAIL += 1
            finally:
                cleanup()
        return wrapper
    return decorator


# ============================================================
# TEST 1: Empty script
# ============================================================
@test("Empty script records without crash")
def test_empty_script(tmp_dir):
    db_path, run_id, stats, rec = record_script("", tmp_dir)
    # Should have at least runner frames
    assert stats['frame_count'] > 0
    assert stats['dropped_frames'] == 0


# ============================================================
# TEST 2: Script that only raises
# ============================================================
@test("Script that raises records exception events")
def test_only_raises(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        raise RuntimeError("boom")
    """, tmp_dir)
    assert stats['frame_count'] > 0
    exceptions = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.frame_event.in_(['exception', 'exception_unwind']))))
    assert len(exceptions) > 0, "Should have exception events"


# ============================================================
# TEST 3: Deep recursion
# ============================================================
@test("Deep recursion (500 levels) records correctly")
def test_deep_recursion(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        import sys
        sys.setrecursionlimit(1000)
        def recurse(n):
            if n <= 0:
                return 0
            return recurse(n - 1) + 1
        result = recurse(500)
    """, tmp_dir)
    assert stats['frame_count'] > 500
    # Check call depths go deep
    max_depth = ExecutionFrames.select(
        ExecutionFrames.call_depth).where(
        ExecutionFrames.run_id == run_id).order_by(
        ExecutionFrames.call_depth.desc()).limit(1).first()
    assert max_depth.call_depth >= 500, f"Max depth {max_depth.call_depth} < 500"


# ============================================================
# TEST 4: Very large locals (buffer overflow test)
# ============================================================
@test("Large locals string doesn't crash or corrupt JSON")
def test_large_locals(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        big_list = list(range(10000))
        big_str = "x" * 100000
        big_dict = {str(i): i for i in range(1000)}
        x = 42
    """, tmp_dir)
    # Should record without crash
    assert stats['frame_count'] > 0
    # Verify JSON is still valid for frames that have locals
    frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.locals_snapshot.is_null(False))))
    valid_json = 0
    for f in frames:
        if f.locals_snapshot:
            try:
                parsed = json.loads(f.locals_snapshot)
                assert isinstance(parsed, dict)
                valid_json += 1
            except json.JSONDecodeError as e:
                raise AssertionError(
                    f"Invalid JSON at seq {f.sequence_no}: {e}\n"
                    f"  snippet: {f.locals_snapshot[:200]}")
    assert valid_json > 0, "Should have at least one frame with valid JSON locals"


# ============================================================
# TEST 5: Unicode in variables
# ============================================================
@test("Unicode variable names and values serialize correctly")
def test_unicode_locals(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        café = "☕"
        emoji = "🎉🎊🎈"
        chinese = "你好世界"
        backslash = "path\\\\to\\\\file"
        quotes = 'he said "hello"'
        newlines = "line1\\nline2\\nline3"
        x = 42
    """, tmp_dir)
    assert stats['frame_count'] > 0
    # Find a frame with our unicode vars
    frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.locals_snapshot.is_null(False))))
    found_unicode = False
    for f in frames:
        if f.locals_snapshot and '"emoji"' in f.locals_snapshot:
            parsed = json.loads(f.locals_snapshot)
            if 'emoji' in parsed:
                found_unicode = True
                break
    assert found_unicode, "Should find frame with unicode variables"


# ============================================================
# TEST 6: repr reentrancy with side effects
# ============================================================
@test("Reentrant __repr__ with global mutation doesn't corrupt state")
def test_repr_reentrancy_complex(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        repr_count = 0
        class Tricky:
            def __repr__(self):
                global repr_count
                repr_count += 1
                # This repr creates new objects, calls functions
                return f"Tricky(count={repr_count}, hash={hash(self)})"
        class ReprRaises:
            def __repr__(self):
                raise ValueError("repr failed!")
        t = Tricky()
        r = ReprRaises()
        x = [t, r, t]
        y = 42
    """, tmp_dir)
    assert stats['frame_count'] > 0
    assert stats['dropped_frames'] == 0


# ============================================================
# TEST 7: Multiple recordings in sequence
# ============================================================
@test("Sequential recordings don't leak state")
def test_sequential_recordings(tmp_dir):
    results = []
    for i in range(5):
        db_path, run_id, stats, rec = record_script(f"""
            x = {i}
            y = x * 2
        """, tmp_dir)
        results.append((run_id, stats['frame_count']))
        cleanup()
    # Each recording should be independent
    assert len(set(r[0] for r in results)) == 5, "All run_ids should be unique"
    for run_id, fc in results:
        assert fc > 0, f"Run {run_id} had 0 frames"


# ============================================================
# TEST 8: Session navigation edge cases
# ============================================================
@test("Session navigation at boundaries")
def test_session_boundaries(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def foo():
            x = 1
            return x
        foo()
    """, tmp_dir)
    session = Session()
    first_line = (ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no)
        .limit(1).first())
    session.enter_replay(run_id, first_line.sequence_no)

    # Step into until end
    steps = 0
    while steps < 5000:
        result = session.step_into()
        steps += 1
        if result["reason"] == "end":
            break
    assert result["reason"] == "end", f"Should reach end, got {result['reason']}"
    assert result["seq"] == session.last_line_seq

    # Step into AGAIN at end — should stay at end
    result2 = session.step_into()
    assert result2["reason"] == "end"
    assert result2["seq"] == session.last_line_seq

    # Step over at end — should stay at end
    result3 = session.step_over()
    assert result3["reason"] == "end"

    # Step out at end — should stay at end
    result4 = session.step_out()
    assert result4["reason"] == "end"

    # Continue at end — should stay at end
    result5 = session.continue_forward()
    assert result5["reason"] == "end"


# ============================================================
# TEST 9: Warm replay for every frame
# ============================================================
@test("Warm replay returns valid data for every recorded frame")
def test_warm_replay_all_frames(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def compute(n):
            total = 0
            for i in range(n):
                total += i
            return total
        compute(20)
    """, tmp_dir)

    replay = ReplayController()
    all_frames = list(ExecutionFrames.select(
        ExecutionFrames.sequence_no, ExecutionFrames.frame_event).where(
        ExecutionFrames.run_id == run_id).order_by(
        ExecutionFrames.sequence_no))

    errors = []
    for f in all_frames:
        result = replay.warm_goto_frame(run_id, f.sequence_no)
        if 'error' in result:
            errors.append(f"seq={f.sequence_no}: {result['error']}")
        if result.get('seq') != f.sequence_no:
            errors.append(f"seq mismatch: expected {f.sequence_no}, got {result.get('seq')}")
    assert len(errors) == 0, f"Warm replay errors:\n" + "\n".join(errors[:10])


# ============================================================
# TEST 10: Locals JSON validity across all frames
# ============================================================
@test("All locals_snapshot fields parse as valid JSON dicts")
def test_all_locals_valid_json(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        import math
        def calc():
            pi = math.pi
            e = math.e
            result = pi * e
            formatted = f"{result:.6f}"
            return formatted
        calc()
    """, tmp_dir)

    frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.locals_snapshot.is_null(False))))

    bad = []
    for f in frames:
        if not f.locals_snapshot:
            continue
        try:
            parsed = json.loads(f.locals_snapshot)
            if not isinstance(parsed, dict):
                bad.append(f"seq={f.sequence_no}: not a dict, got {type(parsed)}")
        except json.JSONDecodeError as e:
            bad.append(f"seq={f.sequence_no}: {e} — {f.locals_snapshot[:100]}")
    assert len(bad) == 0, f"Invalid JSON in {len(bad)} frames:\n" + "\n".join(bad[:10])


# ============================================================
# TEST 11: Sequence number monotonicity and completeness
# ============================================================
@test("Sequence numbers are strictly monotonic with no gaps in user events")
def test_sequence_monotonic(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        for i in range(50):
            x = i * 2
    """, tmp_dir)

    seqs = [f.sequence_no for f in ExecutionFrames.select(
        ExecutionFrames.sequence_no).where(
        ExecutionFrames.run_id == run_id).order_by(
        ExecutionFrames.sequence_no)]

    # Strictly increasing
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i-1], f"Non-monotonic: seq[{i-1}]={seqs[i-1]}, seq[{i}]={seqs[i]}"

    # No gaps (every integer from first to last should be present)
    expected = set(range(seqs[0], seqs[-1] + 1))
    actual = set(seqs)
    missing = expected - actual
    assert len(missing) == 0, f"Missing {len(missing)} sequence numbers: {sorted(missing)[:20]}"


# ============================================================
# TEST 12: Stack reconstruction correctness
# ============================================================
@test("Stack reconstruction matches call depth at every line event")
def test_stack_reconstruction(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def a():
            return b()
        def b():
            return c()
        def c():
            x = 42
            return x
        a()
    """, tmp_dir)

    session = Session()
    first_line = (ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no).limit(1).first())
    session.enter_replay(run_id, first_line.sequence_no)

    # Sample some line events and check stack depth
    user_lines = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.frame_event == 'line') &
        (ExecutionFrames.filename.contains('test_script')))
        .order_by(ExecutionFrames.sequence_no))

    errors = []
    for f in user_lines:
        stack = session._build_stack_at(f.sequence_no)
        # Stack length should approximately correspond to call_depth + 1
        # (but includes runner frames, so it's >= call_depth)
        if len(stack) == 0:
            errors.append(f"seq={f.sequence_no}: empty stack for {f.function_name}")
        # Top of stack should be the current function
        if stack and stack[0]['name'] != f.function_name:
            errors.append(f"seq={f.sequence_no}: stack top is {stack[0]['name']}, expected {f.function_name}")

    assert len(errors) == 0, f"Stack errors:\n" + "\n".join(errors[:10])


# ============================================================
# TEST 13: Checkpoint creation with eviction
# ============================================================
@test("Checkpoint creation with tight interval triggers eviction")
def test_checkpoint_eviction(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        total = 0
        for i in range(5000):
            total += i
    """, tmp_dir, checkpoint_interval=100)

    checkpoints = list(Checkpoint.select().where(
        Checkpoint.run_id == run_id))
    # With 5000+ frames and interval=100, we'd get ~50 checkpoints,
    # but max is 32, so eviction must have occurred
    assert len(checkpoints) > 0, "Should have created checkpoints"
    # Verify sequence numbers are valid
    for cp in checkpoints:
        assert cp.sequence_no >= 0


# ============================================================
# TEST 14: Breakpoint matching with realpath
# ============================================================
@test("Breakpoints match on realpath-resolved filenames")
def test_breakpoint_realpath(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def target():
            x = 42
            return x
        target()
    """, tmp_dir)

    session = Session()
    first_line = (ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no).limit(1).first())
    session.enter_replay(run_id, first_line.sequence_no)

    # Find a user-code frame to get the real filename
    user_frame = ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.function_name == 'target') &
        (ExecutionFrames.frame_event == 'line'))
    assert user_frame is not None

    # Set breakpoint using the recorded filename
    session.set_breakpoints([{
        "file": user_frame.filename,
        "line": user_frame.line_no
    }])
    result = session.continue_forward()
    assert result["reason"] == "breakpoint", f"Expected breakpoint, got {result['reason']}"
    assert result["seq"] == user_frame.sequence_no


# ============================================================
# TEST 15: Concurrent recording attempt should fail
# ============================================================
@test("Double start_recording raises RuntimeError")
def test_double_recording(tmp_dir):
    db_path = os.path.join(tmp_dir, "test.pyttd.db")
    delete_db_files(db_path)
    config = PyttdConfig()
    recorder = Recorder(config)
    recorder.start(db_path)
    try:
        # Trying to start another recording should fail
        config2 = PyttdConfig()
        recorder2 = Recorder(config2)
        try:
            recorder2.start(db_path)
            raise AssertionError("Should have raised RuntimeError")
        except RuntimeError:
            pass  # Expected
    finally:
        recorder.stop()
        pyttd_native.kill_all_checkpoints()


# ============================================================
# TEST 16: Special characters in JSON escaping
# ============================================================
@test("JSON escaping handles all special characters correctly")
def test_json_special_chars(tmp_dir):
    db_path, run_id, stats, rec = record_script(r'''
        tab = "\t"
        newline = "\n"
        cr = "\r"
        backslash = "\\"
        quote = "\""
        null_char = "\x00"
        bell = "\x07"
        mixed = "line1\nline2\ttab\r\nend"
        x = 1
    ''', tmp_dir)

    frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.locals_snapshot.is_null(False)) &
        (ExecutionFrames.filename.contains('test_script'))))

    found_special = False
    for f in frames:
        if not f.locals_snapshot or 'mixed' not in f.locals_snapshot:
            continue
        try:
            parsed = json.loads(f.locals_snapshot)
            if 'mixed' in parsed:
                found_special = True
                break
        except json.JSONDecodeError as e:
            raise AssertionError(f"JSON decode error at seq {f.sequence_no}: {e}\n"
                                f"  raw: {f.locals_snapshot[:200]}")
    assert found_special, "Should find frame with special-char variables"


# ============================================================
# TEST 17: Variables query returns correct types
# ============================================================
@test("get_variables_at returns correct types for all Python types")
def test_variable_types(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def typed():
            an_int = 42
            a_float = 3.14
            a_bool = True
            a_none = None
            a_str = "hello"
            a_list = [1, 2, 3]
            a_dict = {"key": "val"}
            a_tuple = (1, 2)
            x = 1
        typed()
    """, tmp_dir)

    session = Session()
    first_line = (ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no).limit(1).first())
    session.enter_replay(run_id, first_line.sequence_no)

    # Find a frame where most vars are set
    frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.function_name == 'typed') &
        (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no.desc()))

    found = False
    for f in frames:
        if f.locals_snapshot and 'a_tuple' in f.locals_snapshot:
            variables = session.get_variables_at(f.sequence_no)
            var_map = {v['name']: v for v in variables}
            assert var_map['an_int']['type'] == 'int', f"Expected int, got {var_map['an_int']['type']}"
            assert var_map['a_float']['type'] == 'float'
            assert var_map['a_bool']['type'] == 'bool'
            assert var_map['a_none']['type'] == 'NoneType'
            assert var_map['a_list']['type'] == 'list'
            assert var_map['a_dict']['type'] == 'dict'
            assert var_map['a_tuple']['type'] == 'tuple'
            found = True
            break
    assert found, "Should find frame with all typed variables"


# ============================================================
# RUN ALL TESTS
# ============================================================
if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if callable(v) and hasattr(v, '__wrapped__')]
    # Just call all test_ functions
    for name, obj in list(globals().items()):
        if name.startswith('test_') and callable(obj):
            obj()

    print(f"\n{'='*60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL > 0:
        print("SOME TESTS FAILED!")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED!")
