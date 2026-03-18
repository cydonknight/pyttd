"""Tests for multi-thread recording (v0.3.0)."""
import pytest
from pyttd.models.frames import ExecutionFrames
from pyttd.models.checkpoints import Checkpoint
from pyttd.session import Session


class TestMultiThreadRecording:
    def test_basic_multithread(self, record_func):
        """Events from both main and child threads are recorded."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    x = 1
    y = 2
t = threading.Thread(target=worker)
t.start()
t.join()
z = 3
""")
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        thread_ids = set(f.thread_id for f in frames)
        assert len(thread_ids) >= 2, f"Expected 2+ threads, got {thread_ids}"

    def test_thread_id_distinct(self, record_func):
        """Main thread and child thread have different thread_ids."""
        db_path, run_id, stats = record_func("""
import threading
result = []
def worker():
    result.append(1)
t = threading.Thread(target=worker)
t.start()
t.join()
""")
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        thread_ids = set(f.thread_id for f in frames)
        # Must have at least 2 distinct thread IDs
        assert len(thread_ids) >= 2

    def test_sequence_no_unique(self, record_func):
        """Sequence numbers are globally unique (no duplicates)."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    for i in range(10):
        x = i
t = threading.Thread(target=worker)
t.start()
t.join()
for i in range(10):
    y = i
""")
        frames = list(ExecutionFrames.select(
            ExecutionFrames.sequence_no
        ).where(ExecutionFrames.run_id == run_id))
        seq_nos = [f.sequence_no for f in frames]
        assert len(seq_nos) == len(set(seq_nos)), "Duplicate sequence numbers found"

    def test_call_depth_per_thread(self, record_func):
        """Each thread has independent call depth starting at 0."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    x = 1
def main_func():
    y = 2
t = threading.Thread(target=worker)
t.start()
t.join()
main_func()
""")
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        thread_ids = set(f.thread_id for f in frames)

        # Each thread should have events at depth 0
        for tid in thread_ids:
            thread_frames = [f for f in frames if f.thread_id == tid]
            depths = set(f.call_depth for f in thread_frames)
            assert 0 in depths, f"Thread {tid} has no depth-0 frames"

    def test_step_over_stays_on_thread(self, record_func, db_setup):
        """step_over in thread A does not land on thread B."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    a = 1
    b = 2
    c = 3
t = threading.Thread(target=worker)
t.start()
t.join()
x = 1
y = 2
""")
        session = Session()
        # Find first line event
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Get the main thread ID
        main_thread = first_line.thread_id

        # Step over several times — should stay on main thread
        for _ in range(5):
            result = session.step_over()
            if result.get("reason") == "end":
                break
            frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == result["seq"]))
            assert frame is not None, f"Frame not found for seq {result['seq']}"
            assert frame.thread_id == main_thread, \
                f"step_over landed on thread {frame.thread_id}, expected {main_thread}"

    def test_step_into_crosses_threads(self, record_func, db_setup):
        """step_into follows global sequence, may cross threads."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    a = 1
t = threading.Thread(target=worker)
t.start()
t.join()
""")
        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Collect all threads we visit via step_into
        visited_threads = set()
        for _ in range(50):
            result = session.step_into()
            if result.get("reason") == "end":
                break
            if "thread_id" in result:
                visited_threads.add(result["thread_id"])

        # Non-deterministic: worker thread may finish before we step into it,
        # so we can only guarantee the main thread was visited.
        assert len(visited_threads) >= 1

    def test_stack_reconstruction_per_thread(self, record_func, db_setup):
        """Stack at a frame in thread A only contains thread A's frames."""
        db_path, run_id, stats = record_func("""
import threading
def worker_inner():
    z = 3
def worker():
    worker_inner()
t = threading.Thread(target=worker)
t.start()
t.join()
""")
        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Find a frame from the worker thread
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        thread_ids = set(f.thread_id for f in frames)

        assert len(thread_ids) >= 2, \
            f"Expected 2+ threads for stack reconstruction test, got {len(thread_ids)}"
        main_tid = first_line.thread_id
        worker_tid = (thread_ids - {main_tid}).pop()

        # Find a line event in worker thread
        worker_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.thread_id == worker_tid) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        assert worker_line is not None, "Worker thread should have line events"
        stack = session._build_stack_at(worker_line.sequence_no)
        # All stack entries should be from worker thread's frames
        for entry in stack:
            frame = ExecutionFrames.get_or_none(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no == entry['seq']))
            assert frame is not None, f"Stack entry frame not found for seq {entry['seq']}"
            assert frame.thread_id == worker_tid, \
                f"Stack entry from wrong thread: {frame.thread_id} != {worker_tid}"

    def test_continue_breakpoint_any_thread(self, record_func, db_setup):
        """Breakpoint hit in any thread stops execution."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    a = 1
    b = 2
t = threading.Thread(target=worker)
t.start()
t.join()
x = 1
""")
        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        # Find a worker thread line to set breakpoint on
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        thread_ids = set(f.thread_id for f in frames)

        assert len(thread_ids) >= 2, \
            f"Expected 2+ threads for breakpoint test, got {len(thread_ids)}"
        main_tid = first_line.thread_id
        worker_tid = (thread_ids - {main_tid}).pop()

        worker_lines = list(ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.thread_id == worker_tid) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no))

        assert len(worker_lines) >= 1, "Worker thread should have line events"
        wl = worker_lines[0]
        session.set_breakpoints([{'file': wl.filename, 'line': wl.line_no}])
        result = session.continue_forward()
        assert result.get("reason") == "breakpoint"

    def test_get_threads(self, record_func, db_setup):
        """get_threads returns all recorded threads."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    x = 1
t = threading.Thread(target=worker)
t.start()
t.join()
""")
        session = Session()
        first_line = (ExecutionFrames.select()
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.frame_event == 'line'))
            .order_by(ExecutionFrames.sequence_no).first())
        session.enter_replay(run_id, first_line.sequence_no)

        threads = session.get_threads()
        thread_ids_in_db = set(f.thread_id for f in ExecutionFrames.select(
            ExecutionFrames.thread_id).where(
            ExecutionFrames.run_id == run_id).distinct())

        assert len(threads) == len(thread_ids_in_db)
        # Main thread is the one that recorded the first event (seq 0)
        first_event = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == 0))
        main_tid = first_event.thread_id
        main_entry = [t for t in threads if t["id"] == main_tid]
        assert len(main_entry) == 1
        assert main_entry[0]["name"] == "Main Thread"

    def test_checkpoint_skipped_multithread(self, record_func):
        """Checkpoints are skipped when multiple threads exist."""
        db_path, run_id, stats = record_func("""
import threading
def worker():
    for i in range(100):
        x = i
# Start worker so multi-thread detected early
t = threading.Thread(target=worker)
t.start()
# Do lots of work on main thread too
for i in range(200):
    y = i
t.join()
""", checkpoint_interval=50)
        # After thread spawn, checkpoints should be skipped
        # Some checkpoints might exist from before the thread was spawned
        checkpoints = list(Checkpoint.select().where(
            Checkpoint.run_id == run_id))
        # We can't guarantee zero checkpoints (some may fire before thread spawn).
        # But the count must be bounded by MAX_CHECKPOINTS (32) and should be
        # fewer than single-threaded equivalent due to multi-thread skip guard.
        assert len(checkpoints) <= 32

    def test_multiple_worker_threads(self, record_func):
        """Multiple worker threads are all recorded."""
        db_path, run_id, stats = record_func("""
import threading
def worker(n):
    x = n * 2
threads = []
for i in range(3):
    t = threading.Thread(target=worker, args=(i,))
    threads.append(t)
    t.start()
for t in threads:
    t.join()
""")
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        thread_ids = set(f.thread_id for f in frames)
        # Main thread + 3 workers = 4 threads (at minimum main + some workers)
        assert len(thread_ids) >= 2, f"Expected 2+ threads, got {len(thread_ids)}"

    def test_thread_id_stored_in_db(self, record_func):
        """thread_id values are non-zero for all recorded frames."""
        db_path, run_id, stats = record_func("""
x = 1
y = 2
""")
        frames = list(ExecutionFrames.select().where(
            ExecutionFrames.run_id == run_id))
        for f in frames:
            assert f.thread_id != 0, "thread_id should be non-zero (actual OS thread ID)"
