"""Test for resource leaks: file descriptors, processes, DB connections.
Runs multiple record/replay cycles and checks for accumulating resources."""
import gc
import os
import sys
import subprocess
import tempfile
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.runner import Runner
from pyttd.session import Session
from pyttd.replay import ReplayController
from pyttd.models import storage
from pyttd.models.base import db
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.checkpoints import Checkpoint
from pyttd.models.io_events import IOEvent
import pyttd_native

passed = 0
failed = 0

def ok(msg):
    global passed; passed += 1; print(f"  [PASS] {msg}")
def fail(msg):
    global failed; failed += 1; print(f"  [FAIL] {msg}")
def check(cond, msg):
    ok(msg) if cond else fail(msg)

def count_open_fds():
    """Count open file descriptors for current process."""
    count = 0
    for fd in range(1024):
        try:
            os.fstat(fd)
            count += 1
        except OSError:
            pass
    return count

def count_child_processes():
    """Count child processes."""
    result = subprocess.run(
        ["pgrep", "-P", str(os.getpid())],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return 0
    return len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0

def full_record_cycle(tmp_dir, script_content, name, checkpoint_interval=0):
    """Full record + navigate + cleanup cycle."""
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

    # Navigate
    session = Session()
    first_line = (ExecutionFrames.select()
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.frame_event == 'line'))
        .order_by(ExecutionFrames.sequence_no).first())
    if first_line:
        session.enter_replay(run_id, first_line.sequence_no)
        for _ in range(20):
            r = session.step_into()
            if r.get("reason") == "end":
                break
        for _ in range(10):
            r = session.step_back()
            if r.get("reason") == "start":
                break

    # Full cleanup
    recorder.cleanup()
    db.init(None)
    gc.collect()
    return stats


def test_fd_leak():
    """Test: file descriptors don't leak across multiple record cycles."""
    print("\n--- Test: FD leak detection ---")
    gc.collect()
    baseline_fds = count_open_fds()
    print(f"  Baseline FDs: {baseline_fds}")

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(5):
            full_record_cycle(tmp, '''
                def work():
                    return sum(range(100))
                for _ in range(10):
                    work()
            ''', f"fd_test_{i}")

        gc.collect()
        after_fds = count_open_fds()
        print(f"  After 5 cycles FDs: {after_fds}")
        leaked = after_fds - baseline_fds
        check(leaked <= 2, f"FD leak: {leaked} (baseline={baseline_fds}, after={after_fds})")


def test_process_leak():
    """Test: checkpoint children don't leak (all killed on cleanup)."""
    print("\n--- Test: Process leak detection ---")
    baseline_children = count_child_processes()
    print(f"  Baseline children: {baseline_children}")

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(3):
            full_record_cycle(tmp, '''
                def work():
                    total = 0
                    for j in range(200):
                        total += j
                    return total
                for _ in range(20):
                    work()
            ''', f"proc_test_{i}", checkpoint_interval=100)

        gc.collect()
        time.sleep(0.5)  # Give processes time to exit
        after_children = count_child_processes()
        print(f"  After 3 cycles with checkpoints: {after_children} children")
        leaked = after_children - baseline_children
        check(leaked == 0, f"Process leak: {leaked} children leaked")


def test_db_connection_leak():
    """Test: DB connections are properly closed."""
    print("\n--- Test: DB connection leak ---")
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(10):
            full_record_cycle(tmp, '''
                x = 1
                y = 2
                z = x + y
            ''', f"db_test_{i}")

        # After all cycles, DB should be disconnected
        check(not db.is_connection_usable(),
              "DB connection closed after all cycles")


def test_repeated_record_same_db():
    """Test: recording to the same DB path multiple times."""
    print("\n--- Test: Repeated recording to same path ---")
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(5):
            full_record_cycle(tmp, f'''
                result = {i} + 1
            ''', "same_db")
            # Each cycle uses delete_db_files, so it should work

        # Final check: can still read the last recording
        db_path = os.path.join(tmp, "same_db.pyttd.db")
        if os.path.exists(db_path):
            storage.connect_to_db(db_path)
            runs = list(Runs.select())
            check(len(runs) == 1, f"Single run in DB after 5 cycles: {len(runs)}")
            storage.close_db()
            db.init(None)
        else:
            ok("DB file cleaned up (expected)")


def test_concurrent_ops():
    """Test: rapid operations don't cause state corruption."""
    print("\n--- Test: Rapid state transitions ---")
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "rapid.py")
        with open(script_path, 'w') as f:
            f.write(textwrap.dedent('''
                def work(n):
                    total = 0
                    for i in range(n):
                        total += i
                    return total
                results = [work(i) for i in range(30)]
            '''))

        db_path = os.path.join(tmp, "rapid.pyttd.db")
        storage.delete_db_files(db_path)

        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        runner = Runner()
        recorder.start(db_path, script_path=script_path)
        try:
            runner.run_script(script_path, tmp)
        except BaseException:
            pass
        stats = recorder.stop()
        run_id = recorder.run_id

        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Rapid alternating forward/backward
        errors = 0
        for i in range(100):
            try:
                if i % 3 == 0:
                    session.step_into()
                elif i % 3 == 1:
                    session.step_back()
                else:
                    session.step_over()
            except Exception as e:
                errors += 1
                if errors == 1:
                    print(f"  Error on op {i}: {e}")

        check(errors == 0, f"Rapid nav: {errors} errors in 100 operations")

        # Verify stack is consistent
        stack = session.get_stack_at(session.current_frame_seq)
        check(isinstance(stack, list) and len(stack) >= 1,
              f"Stack valid after rapid nav: {len(stack)} frames")

        recorder.cleanup()
        db.init(None)


if __name__ == "__main__":
    print("=" * 60)
    print("RESOURCE LEAK TESTS")
    print("=" * 60)

    test_fd_leak()
    test_process_leak()
    test_db_connection_leak()
    test_repeated_record_same_db()
    test_concurrent_ops()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)
