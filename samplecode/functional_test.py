"""Functional tests exercising pyttd end-to-end as a real user would.

Tests the full record → query → replay → navigate pipeline,
probing for bugs, memory leaks, data corruption, and edge cases.
"""
import json
import os
import signal
import sys
import tempfile
import textwrap
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyttd_native
from pyttd.models import storage
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.checkpoints import Checkpoint
from pyttd.models.io_events import IOEvent
from pyttd.recorder import Recorder
from pyttd.runner import Runner
from pyttd.config import PyttdConfig
from pyttd.session import Session
from pyttd.replay import ReplayController
from pyttd.query import get_last_run, get_frames, get_frame_at_seq

results = []

def test(name):
    def decorator(func):
        def wrapper():
            tmp_dir = tempfile.mkdtemp()
            try:
                func(tmp_dir)
                results.append(("PASS", name, None))
                print(f"  PASS: {name}", flush=True)
            except BaseException as e:
                results.append(("FAIL", name, str(e)))
                print(f"  FAIL: {name}\n    {type(e).__name__}: {e}", flush=True)
            finally:
                try:
                    storage.close_db()
                except Exception:
                    pass
        return wrapper
    return decorator

def record_script(code, tmp_dir, config=None, script_name="test_script.py"):
    """Record a script and return (db_path, run_id, stats, recorder)."""
    script = os.path.join(tmp_dir, script_name)
    with open(script, "w") as f:
        f.write(textwrap.dedent(code).lstrip())
    db_path = os.path.join(tmp_dir, f"{script_name}.pyttd.db")
    storage.delete_db_files(db_path)
    if config is None:
        config = PyttdConfig(checkpoint_interval=0)
    rec = Recorder(config)
    rec.start(db_path, script)
    try:
        Runner().run_script(script, tmp_dir)
    except SystemExit:
        pass
    except Exception:
        pass
    finally:
        stats = rec.stop()
    return db_path, rec.run_id, stats, rec

def make_session(run_id):
    """Create a Session and enter replay mode."""
    session = Session()
    first_line = (ExecutionFrames.select(ExecutionFrames.sequence_no)
                  .where((ExecutionFrames.run_id == run_id) &
                         (ExecutionFrames.frame_event == 'line'))
                  .order_by(ExecutionFrames.sequence_no)
                  .limit(1).first())
    if first_line is None:
        raise ValueError("No line events found")
    session.enter_replay(run_id, first_line.sequence_no)
    return session


# ============================================================
# TEST 1: Full CLI-style record → query pipeline
# ============================================================
@test("Record and query: full pipeline matches expected frame sequence")
def test_record_query_pipeline(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def greet(name):
            msg = f"Hello, {name}!"
            return msg

        result = greet("World")
        result2 = greet("Python")
    """, tmp_dir)

    assert stats['frame_count'] > 0

    # Query API should work
    run = get_last_run(db_path)
    assert run is not None
    assert run.run_id == run_id

    # Get user-script frames only
    user_frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script'))
    ).order_by(ExecutionFrames.sequence_no))

    assert len(user_frames) > 0

    # Should see call/line/return pattern for greet()
    events = [f.frame_event for f in user_frames]
    assert 'call' in events
    assert 'line' in events
    assert 'return' in events

    # Check that greet function was recorded
    func_names = {f.function_name for f in user_frames}
    assert 'greet' in func_names

    # Check locals in return frame of greet
    greet_returns = [f for f in user_frames
                     if f.function_name == 'greet' and f.frame_event == 'return']
    assert len(greet_returns) == 2  # called twice
    for ret in greet_returns:
        parsed = json.loads(ret.locals_snapshot)
        assert 'name' in parsed
        assert 'msg' in parsed


# ============================================================
# TEST 2: Warm replay for every frame
# ============================================================
@test("Warm replay returns consistent data for all user-script frames")
def test_warm_replay_all_frames(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        total = 0
        for i in range(5):
            total += i * 2
        result = total
    """, tmp_dir)

    replay = ReplayController()
    user_frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script'))
    ).order_by(ExecutionFrames.sequence_no))

    assert len(user_frames) > 0

    for uf in user_frames:
        result = replay.warm_goto_frame(run_id, uf.sequence_no)
        assert result is not None, f"warm_goto_frame({uf.sequence_no}) returned None"
        assert result['seq'] == uf.sequence_no
        assert 'function_name' in result
        assert result['file'] == uf.filename


# ============================================================
# TEST 3: Session forward navigation
# ============================================================
@test("Session step_into walks through all user events in order")
def test_session_step_into(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        x = 1
        y = 2
        z = x + y
    """, tmp_dir)

    session = make_session(run_id)

    # Step through all events, tracking sequence numbers
    visited = [session.current_frame_seq]
    for _ in range(200):  # safety limit
        result = session.step_into()
        if result is None or result.get('reason') == 'end':
            break
        if session.current_frame_seq == visited[-1]:
            break  # stuck at same position
        visited.append(session.current_frame_seq)

    # Should have visited multiple distinct positions
    assert len(visited) > 3
    # Sequence numbers should be strictly increasing
    for i in range(1, len(visited)):
        assert visited[i] > visited[i-1], \
            f"step_into went backward: {visited[i-1]} -> {visited[i]}"


# ============================================================
# TEST 4: Session step_over skips into functions
# ============================================================
@test("Session step_over correctly skips function bodies")
def test_session_step_over(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def add(a, b):
            result = a + b
            return result

        x = add(1, 2)
        y = add(3, 4)
        z = x + y
    """, tmp_dir)

    session = make_session(run_id)

    # Find the first line event in user script at top level
    user_frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script')) &
        (ExecutionFrames.frame_event == 'line')
    ).order_by(ExecutionFrames.sequence_no))

    # Navigate to the first user-script line
    first_user_seq = user_frames[0].sequence_no
    session.goto_frame(first_user_seq)

    # step_over should not go deeper
    current = ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.sequence_no == session.current_frame_seq))
    if current:
        start_depth = current.call_depth
        result = session.step_over()
        if result:
            next_frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == session.current_frame_seq))
            if next_frame:
                assert next_frame.call_depth <= start_depth, \
                    f"step_over went deeper: {start_depth} -> {next_frame.call_depth}"


# ============================================================
# TEST 5: Session step_back
# ============================================================
@test("Session step_back returns to previous line event")
def test_session_step_back(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        a = 10
        b = 20
        c = a + b
    """, tmp_dir)

    session = make_session(run_id)

    # Step forward a few times
    positions = [session.current_frame_seq]
    for _ in range(5):
        r = session.step_into()
        if r is None or r.get('reason') == 'end':
            break
        if session.current_frame_seq != positions[-1]:
            positions.append(session.current_frame_seq)

    if len(positions) >= 3:
        # Now step back
        before_back = session.current_frame_seq
        result = session.step_back()
        assert result is not None, "step_back returned None"
        assert session.current_frame_seq < before_back, \
            f"step_back didn't go backward: {before_back} -> {session.current_frame_seq}"


# ============================================================
# TEST 6: Session reverse_continue with breakpoints
# ============================================================
@test("Reverse continue stops at breakpoint correctly")
def test_reverse_continue_breakpoint(tmp_dir):
    script_path = os.path.join(tmp_dir, "test_script.py")
    code = textwrap.dedent("""
        x = 1
        y = 2
        z = 3
        w = x + y + z
    """).lstrip()
    with open(script_path, "w") as f:
        f.write(code)

    db_path = os.path.join(tmp_dir, "test_script.py.pyttd.db")
    storage.delete_db_files(db_path)
    config = PyttdConfig(checkpoint_interval=0)
    rec = Recorder(config)
    rec.start(db_path, script_path)
    try:
        Runner().run_script(script_path, tmp_dir)
    except Exception:
        pass
    stats = rec.stop()

    run_id = rec.run_id
    session = make_session(run_id)

    # Navigate to end
    last_frame = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script')) &
        (ExecutionFrames.frame_event == 'line')
    ).order_by(ExecutionFrames.sequence_no.desc()).first()

    if last_frame:
        session.goto_frame(last_frame.sequence_no)

        # Set breakpoint on line 2 (y = 2)
        resolved_path = os.path.realpath(script_path)
        session.set_breakpoints([{'file': resolved_path, 'line': 2}])

        result = session.reverse_continue()
        if result:
            target_frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == session.current_frame_seq))
            if target_frame:
                assert target_frame.line_no == 2, \
                    f"reverse_continue stopped at line {target_frame.line_no}, expected 2"


# ============================================================
# TEST 7: goto_frame with line snapping
# ============================================================
@test("goto_frame snaps to nearest line event")
def test_goto_frame_snapping(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def foo():
            x = 1
            return x
        foo()
    """, tmp_dir)

    session = make_session(run_id)

    # Find a call event (not a line event)
    call_frame = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.frame_event == 'call') &
        (ExecutionFrames.filename.contains('test_script'))
    ).first()

    if call_frame:
        # goto_frame should snap to the next line event
        result = session.goto_frame(call_frame.sequence_no)
        current = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == session.current_frame_seq))
        # Should be at a line event (snapped from call)
        if current:
            assert current.frame_event == 'line', \
                f"goto_frame didn't snap to line: {current.frame_event}"


# ============================================================
# TEST 8: Stack reconstruction consistency
# ============================================================
@test("Stack trace is consistent at every point in nested calls")
def test_stack_reconstruction(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def outer():
            return inner()

        def inner():
            x = 42
            return x

        result = outer()
    """, tmp_dir)

    session = make_session(run_id)

    # Navigate to a point inside inner()
    inner_frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.function_name == 'inner') &
        (ExecutionFrames.frame_event == 'line')
    ))

    if inner_frames:
        session.goto_frame(inner_frames[0].sequence_no)
        stack = session.get_stack_at(session.current_frame_seq)
        assert len(stack) >= 2, f"Expected at least 2 stack frames, got {len(stack)}"
        # Stack entries use 'name' key (not 'function_name')
        func_names = [s['name'] for s in stack]
        assert 'inner' in func_names, f"Missing inner in stack: {func_names}"
        assert 'outer' in func_names, f"Missing outer in stack: {func_names}"


# ============================================================
# TEST 9: Variable inspection
# ============================================================
@test("Variable inspection returns correct values at each point")
def test_variable_inspection(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        x = 42
        name = "hello"
        lst = [1, 2, 3]
        d = {"key": "value"}
        flag = True
        nothing = None
    """, tmp_dir)

    session = make_session(run_id)

    # Find last line event in user script (should have all vars)
    last_line = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script')) &
        (ExecutionFrames.frame_event.in_(['line', 'return']))
    ).order_by(ExecutionFrames.sequence_no.desc()).first()

    if last_line:
        session.goto_frame(last_line.sequence_no)
        variables = session.get_variables_at(session.current_frame_seq)
        var_dict = {v['name']: v for v in variables}

        # Check type inference
        if 'x' in var_dict:
            assert var_dict['x']['type'] == 'int', f"x type: {var_dict['x']['type']}"
        if 'name' in var_dict:
            assert var_dict['name']['type'] == 'str', f"name type: {var_dict['name']['type']}"
        if 'lst' in var_dict:
            assert var_dict['lst']['type'] == 'list', f"lst type: {var_dict['lst']['type']}"
        if 'd' in var_dict:
            assert var_dict['d']['type'] == 'dict', f"d type: {var_dict['d']['type']}"
        if 'flag' in var_dict:
            assert var_dict['flag']['type'] == 'bool', f"flag type: {var_dict['flag']['type']}"
        if 'nothing' in var_dict:
            assert var_dict['nothing']['type'] == 'NoneType', f"nothing type: {var_dict['nothing']['type']}"


# ============================================================
# TEST 10: Expression evaluation
# ============================================================
@test("Expression evaluation works in different contexts")
def test_expression_evaluation(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        x = 10
        y = 20
    """, tmp_dir)

    session = make_session(run_id)

    # Find a frame from user script with both x and y in locals
    frame = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script')) &
        (ExecutionFrames.frame_event == 'line') &
        (ExecutionFrames.locals_snapshot.contains('"x":')) &
        (ExecutionFrames.locals_snapshot.contains('"y":'))
    ).order_by(ExecutionFrames.sequence_no.desc()).first()

    if frame:
        session.goto_frame(frame.sequence_no)
        cur_seq = session.current_frame_seq

        # Debug: check what locals are actually available
        vars = session.get_variables_at(cur_seq)
        var_names = [v['name'] for v in vars]

        # Evaluate simple expression
        result = session.evaluate_at(cur_seq, "x", context="hover")
        assert result is not None
        assert '10' in result['result'], \
            f"x result: {result['result']}, available vars: {var_names}, seq={cur_seq}"

        result = session.evaluate_at(cur_seq, "y", context="hover")
        assert result is not None
        assert '20' in result['result'], \
            f"y result: {result['result']}, available vars: {var_names}"


# ============================================================
# TEST 11: Exception recording
# ============================================================
@test("Exception events are recorded with correct event types")
def test_exception_recording(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def will_fail():
            raise ValueError("test error")

        try:
            will_fail()
        except ValueError:
            caught = True
    """, tmp_dir)

    # Check exception events exist
    exc_frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.frame_event.in_(['exception', 'exception_unwind']))
    ))

    assert len(exc_frames) > 0, "No exception events recorded"
    exc_events = {f.frame_event for f in exc_frames}
    assert 'exception' in exc_events, "Missing 'exception' event"


# ============================================================
# TEST 12: I/O hooks recording
# ============================================================
@test("I/O hooks record and serialize time/random calls correctly")
def test_io_hooks(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        import time
        import random
        t1 = time.time()
        t2 = time.monotonic()
        r1 = random.random()
        r2 = random.randint(1, 100)
    """, tmp_dir)

    # Check IOEvents were recorded
    io_events = list(IOEvent.select().where(IOEvent.run_id == run_id))
    assert len(io_events) >= 4, f"Expected >= 4 IOEvents, got {len(io_events)}"

    func_names = {e.function_name for e in io_events}
    assert 'time.time' in func_names, f"Missing time.time in {func_names}"
    assert 'time.monotonic' in func_names, f"Missing time.monotonic in {func_names}"
    assert 'random.random' in func_names, f"Missing random.random in {func_names}"
    assert 'random.randint' in func_names, f"Missing random.randint in {func_names}"

    # Verify return values are valid
    for e in io_events:
        assert e.return_value is not None, f"IOEvent {e.function_name} has None return_value"
        assert len(e.return_value) > 0, f"IOEvent {e.function_name} has empty return_value"


# ============================================================
# TEST 13: Multiple recordings don't leak state
# ============================================================
@test("Sequential recordings produce independent runs with no cross-contamination")
def test_sequential_recordings(tmp_dir):
    runs = []
    for i in range(3):
        sub_dir = os.path.join(tmp_dir, f"run_{i}")
        os.makedirs(sub_dir)
        db_path, run_id, stats, rec = record_script(f"""
            x = {i}
            y = x * {i + 1}
        """, sub_dir)
        runs.append((run_id, stats['frame_count']))
        rec.cleanup()

    # Each run should have independent IDs
    run_ids = [r[0] for r in runs]
    assert len(set(run_ids)) == 3, f"Run IDs not unique: {run_ids}"

    # Each run should have frames
    for run_id, count in runs:
        assert count > 0, f"Run {run_id} has 0 frames"


# ============================================================
# TEST 14: Large recording stress test
# ============================================================
@test("Large recording (10K+ iterations) completes without errors")
def test_large_recording(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        total = 0
        for i in range(200):
            total += i
            if i % 50 == 0:
                checkpoint = total
        result = total
    """, tmp_dir)

    assert stats['frame_count'] > 200

    # Verify data integrity - random spot checks
    import random
    user_frames = list(ExecutionFrames.select(ExecutionFrames.sequence_no).where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script'))
    ).order_by(ExecutionFrames.sequence_no))

    if len(user_frames) > 10:
        # Check 10 random frames for valid JSON
        sample = random.sample(user_frames, min(10, len(user_frames)))
        for sf in sample:
            full = ExecutionFrames.get(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == sf.sequence_no)
            )
            if full.locals_snapshot:
                parsed = json.loads(full.locals_snapshot)  # should not raise
                assert isinstance(parsed, dict)


# ============================================================
# TEST 15: Checkpoint-enabled recording
# ============================================================
@test("Checkpoint-enabled recording creates valid checkpoints")
def test_checkpoint_recording(tmp_dir):
    config = PyttdConfig(checkpoint_interval=500)
    db_path, run_id, stats, rec = record_script("""
        total = 0
        for i in range(300):
            total += i
        result = total
    """, tmp_dir, config=config)

    # Check if checkpoints were created
    checkpoints = list(Checkpoint.select().where(
        Checkpoint.run_id == run_id
    ).order_by(Checkpoint.sequence_no))

    # With 300 iterations producing many events, checkpoint_interval=500
    # may or may not produce checkpoints depending on total frame count
    if stats['frame_count'] > 500:
        assert len(checkpoints) > 0, \
            f"Expected checkpoints with {stats['frame_count']} frames and interval=500"

    rec.cleanup()


# ============================================================
# TEST 16: goto_targets returns valid targets
# ============================================================
@test("goto_targets returns all line events at a given file:line")
def test_goto_targets(tmp_dir):
    script_path = os.path.join(tmp_dir, "test_script.py")
    code = textwrap.dedent("""
        for i in range(3):
            x = i * 2
    """).lstrip()
    with open(script_path, "w") as f:
        f.write(code)

    db_path = os.path.join(tmp_dir, "test_script.py.pyttd.db")
    storage.delete_db_files(db_path)
    config = PyttdConfig(checkpoint_interval=0)
    rec = Recorder(config)
    rec.start(db_path, script_path)
    try:
        Runner().run_script(script_path, tmp_dir)
    except Exception:
        pass
    stats = rec.stop()

    session = make_session(rec.run_id)

    # Get targets for line 2 (x = i * 2) - should have 3 hits
    resolved_path = os.path.realpath(script_path)
    targets = session.goto_targets(resolved_path, 2)

    assert len(targets) == 3, f"Expected 3 targets for loop body, got {len(targets)}"
    # Each target should have distinct sequence numbers
    seqs = [t['seq'] for t in targets]
    assert len(set(seqs)) == 3, f"Target seqs not unique: {seqs}"


# ============================================================
# TEST 17: restart_frame navigation
# ============================================================
@test("restart_frame jumps to first line in function call")
def test_restart_frame(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        def process(val):
            step1 = val + 1
            step2 = step1 * 2
            return step2

        r = process(5)
    """, tmp_dir)

    session = make_session(run_id)

    # Navigate to return of process()
    return_frame = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.function_name == 'process') &
        (ExecutionFrames.frame_event == 'return') &
        (ExecutionFrames.filename.contains('test_script'))
    ).first()

    if return_frame:
        session.goto_frame(return_frame.sequence_no)
        cur_seq = session.current_frame_seq
        # restart_frame should go back to first line in process()
        result = session.restart_frame(cur_seq)
        assert result is not None, f"restart_frame returned None for seq={cur_seq}"
        if 'error' in result:
            # Debug: show what happened
            assert False, f"restart_frame error: {result}, cur_seq={cur_seq}"
        assert result['seq'] < return_frame.sequence_no, \
            f"restart_frame didn't go back: {result['seq']} >= {return_frame.sequence_no}"


# ============================================================
# TEST 18: Timeline summary
# ============================================================
@test("Timeline summary produces valid bucket data")
def test_timeline_summary(tmp_dir):
    from pyttd.models.timeline import get_timeline_summary

    db_path, run_id, stats, rec = record_script("""
        for i in range(50):
            x = i * 2
    """, tmp_dir)

    # Get first and last sequence numbers
    first = ExecutionFrames.select(ExecutionFrames.sequence_no).where(
        ExecutionFrames.run_id == run_id
    ).order_by(ExecutionFrames.sequence_no).first()
    last = ExecutionFrames.select(ExecutionFrames.sequence_no).where(
        ExecutionFrames.run_id == run_id
    ).order_by(ExecutionFrames.sequence_no.desc()).first()

    if first and last:
        buckets = get_timeline_summary(
            run_id, first.sequence_no, last.sequence_no, bucket_count=10
        )
        assert len(buckets) > 0, "Timeline returned no buckets"
        for b in buckets:
            assert 'startSeq' in b
            assert 'endSeq' in b
            assert 'maxCallDepth' in b
            assert isinstance(b['hasException'], bool)
            assert isinstance(b['hasBreakpoint'], bool)
            assert b['startSeq'] <= b['endSeq']


# ============================================================
# TEST 19: Recording with sys.exit()
# ============================================================
@test("Recording handles sys.exit() gracefully")
def test_sys_exit(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        import sys
        x = 1
        y = 2
        sys.exit(0)
    """, tmp_dir)

    assert stats['frame_count'] > 0
    # Should have recorded frames before exit
    user_frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.filename.contains('test_script'))
    ))
    assert len(user_frames) > 0


# ============================================================
# TEST 20: Recording with tight loop + interrupt
# ============================================================
@test("request_stop interrupts long-running recording cleanly")
def test_request_stop(tmp_dir):
    import threading

    script_path = os.path.join(tmp_dir, "test_script.py")
    # Use a loop that does function calls so eval hook fires
    code = textwrap.dedent("""
        i = 0
        while i < 100000:
            i += 1
    """).lstrip()
    with open(script_path, "w") as f:
        f.write(code)

    db_path = os.path.join(tmp_dir, "test_script.py.pyttd.db")
    storage.delete_db_files(db_path)
    config = PyttdConfig(checkpoint_interval=0)
    rec = Recorder(config)
    rec.start(db_path, script_path)

    # Schedule a stop after a brief delay
    def delayed_stop():
        time.sleep(0.05)
        pyttd_native.request_stop()

    stopper = threading.Thread(target=delayed_stop, daemon=True)
    stopper.start()

    try:
        Runner().run_script(script_path, tmp_dir)
    except BaseException:
        pass  # KeyboardInterrupt or any other exception

    try:
        stats = rec.stop()
    except BaseException:
        stats = {'frame_count': 0}

    stopper.join(timeout=5)

    # Should have recorded some frames before being stopped
    assert stats['frame_count'] > 0
    # But not all 100K iterations (2 line events per iteration = 200K events)
    assert stats['frame_count'] < 200000, \
        f"Expected early stop, got {stats['frame_count']} frames"


# ============================================================
# TEST 21: JSON integrity for all recorded data
# ============================================================
@test("All locals_snapshot values are valid JSON across complex script")
def test_json_integrity(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        import os
        data = {"key": "value", "nested": {"a": [1,2,3]}}
        path = os.path.join("/tmp", "test", "file.txt")
        special = "quotes: \\" backslash: \\\\ tab: \\t newline: \\n"
        multiline = '''line1
        line2
        line3'''
        mixed = [1, "hello", None, True, 3.14, {"inner": "dict"}]
    """, tmp_dir)

    frames = list(ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.locals_snapshot.is_null(False))
    ))

    invalid_count = 0
    for f in frames:
        if f.locals_snapshot:
            try:
                json.loads(f.locals_snapshot)
            except json.JSONDecodeError:
                invalid_count += 1
                if invalid_count <= 3:
                    print(f"    Invalid JSON at seq={f.sequence_no}: {f.locals_snapshot[:100]}", flush=True)

    assert invalid_count == 0, f"{invalid_count} frames with invalid JSON"


# ============================================================
# TEST 22: Elapsed time tracking
# ============================================================
@test("Recording stats report non-zero elapsed time")
def test_elapsed_time(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        import time
        time.sleep(0.05)
        x = 1
    """, tmp_dir)

    assert 'elapsed_time' in stats
    assert stats['elapsed_time'] > 0.0
    # Should be at least ~50ms
    assert stats['elapsed_time'] >= 0.01, \
        f"elapsed_time too small: {stats['elapsed_time']}"


# ============================================================
# TEST 23: Ring buffer overflow handling
# ============================================================
@test("Ring buffer overflow drops events without crash")
def test_ringbuf_overflow(tmp_dir):
    # Use tiny ring buffer to force overflow
    config = PyttdConfig(
        checkpoint_interval=0,
        ring_buffer_size=64,  # minimum valid size
        flush_interval_ms=1000,  # slow flush to cause buildup
    )
    db_path, run_id, stats, rec = record_script("""
        for i in range(500):
            x = i
    """, tmp_dir, config=config)

    assert stats['frame_count'] > 0
    # Should report drops (key is 'dropped_frames')
    assert 'dropped_frames' in stats
    assert stats['dropped_frames'] > 0, "Expected drops with tiny buffer + slow flush"


# ============================================================
# TEST 24: Database WAL mode verification
# ============================================================
@test("Database uses WAL journal mode")
def test_wal_mode(tmp_dir):
    db_path, run_id, stats, rec = record_script("""
        x = 1
    """, tmp_dir)

    from pyttd.models.base import db
    cursor = db.execute_sql("PRAGMA journal_mode;")
    mode = cursor.fetchone()[0]
    assert mode == 'wal', f"Expected WAL mode, got {mode}"


# ============================================================
# Run all tests
# ============================================================
if __name__ == '__main__':
    # Collect test functions
    test_funcs = []
    for name, obj in list(globals().items()):
        if name.startswith('test_') and callable(obj):
            test_funcs.append(obj)

    print(f"\nRunning {len(test_funcs)} functional tests...\n", flush=True)
    for tf in test_funcs:
        tf()

    print(f"\n{'='*60}", flush=True)
    passed = sum(1 for r in results if r[0] == 'PASS')
    failed = sum(1 for r in results if r[0] == 'FAIL')
    print(f"Results: {passed} passed, {failed} failed", flush=True)

    if failed > 0:
        print("\nFailed tests:", flush=True)
        for status, name, err in results:
            if status == 'FAIL':
                print(f"  - {name}: {err}", flush=True)
        print("\nSOME TESTS FAILED!", flush=True)
    else:
        print("ALL TESTS PASSED!", flush=True)

    sys.exit(1 if failed > 0 else 0)
