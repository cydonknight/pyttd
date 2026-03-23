"""Component benchmarks: warm navigation, timeline, DB size, stack, variables.

Run: .venv/bin/pytest benchmarks/bench_components.py --benchmark-only -v
"""
import os
import random

import pytest

from pyttd.models.db import db
from pyttd.models.timeline import get_timeline_summary
from pyttd.replay import ReplayController
from pyttd.session import Session


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

WORKLOAD_CALLS = """\
def process(items):
    total = 0
    for item in items:
        total += transform(item)
    return total

def transform(x):
    return x * 2 + 1

for batch in range(20):
    data = list(range(10))
    process(data)
"""

WORKLOAD_DEEP = """\
def level_a(n):
    for i in range(n):
        level_b(i)

def level_b(x):
    level_c(x)

def level_c(x):
    level_d(x)

def level_d(x):
    return x * 2

for _ in range(50):
    level_a(4)
"""

WORKLOAD_MIXED = """\
def compute(x):
    if x % 7 == 0:
        raise ValueError(f"bad {x}")
    return x * 3

results = []
for i in range(100):
    try:
        results.append(compute(i))
    except ValueError:
        pass
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_session(db_path, run_id):
    """Create and enter a replay Session."""
    session = Session()
    first_line = db.fetchone(
        "SELECT sequence_no FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no LIMIT 1",
        (str(run_id),))
    session.enter_replay(run_id, first_line.sequence_no)
    return session


def _get_line_seqs(run_id):
    """Return list of all line-event sequence numbers."""
    rows = db.fetchall(
        "SELECT sequence_no FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no",
        (str(run_id),))
    return [r.sequence_no for r in rows]


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

class TestBenchWarmNav:
    """Warm navigation latency (SQLite reads)."""

    def test_bench_warm_step_into(self, bench_record, benchmark):
        """step_into: target < 10ms per step."""
        db_path, run_id, _ = bench_record(WORKLOAD_CALLS)
        session = _setup_session(db_path, run_id)

        def do_steps():
            for _ in range(50):
                session.step_into()

        benchmark.pedantic(do_steps, rounds=10, warmup_rounds=1)

    def test_bench_warm_step_back(self, bench_record, benchmark):
        """step_back: target < 10ms per step."""
        db_path, run_id, _ = bench_record(WORKLOAD_CALLS)
        session = _setup_session(db_path, run_id)
        # Move forward first so we have room to step back
        line_seqs = _get_line_seqs(run_id)
        midpoint = line_seqs[len(line_seqs) // 2]
        session.goto_frame(midpoint)

        def do_steps():
            for _ in range(50):
                session.step_back()

        benchmark.pedantic(do_steps, rounds=10, warmup_rounds=1)

    def test_bench_warm_goto_frame(self, bench_record, benchmark):
        """warm_goto_frame: target < 10ms per goto."""
        db_path, run_id, _ = bench_record(WORKLOAD_CALLS)
        replay = ReplayController()
        line_seqs = _get_line_seqs(run_id)
        rng = random.Random(42)
        targets = [rng.choice(line_seqs) for _ in range(100)]
        idx = [0]

        def do_goto():
            replay.warm_goto_frame(run_id, targets[idx[0] % len(targets)])
            idx[0] += 1

        benchmark.pedantic(do_goto, rounds=100, warmup_rounds=2)


class TestBenchTimeline:
    """Timeline summary query performance."""

    def test_bench_timeline_summary(self, bench_record, benchmark):
        """get_timeline_summary with 500 buckets: target < 16ms."""
        db_path, run_id, _ = bench_record(WORKLOAD_MIXED)
        line_seqs = _get_line_seqs(run_id)
        start_seq = line_seqs[0]
        end_seq = line_seqs[-1]

        def do_query():
            return get_timeline_summary(run_id, start_seq, end_seq,
                                        bucket_count=500)

        benchmark.pedantic(do_query, rounds=20, warmup_rounds=2)


class TestBenchDBSize:
    """Database size per frame."""

    def test_bench_db_size_per_frame(self, bench_record):
        """DB size: target < 5000 bytes/frame (includes SQLite fixed overhead)."""
        db_path, run_id, stats = bench_record(WORKLOAD_MIXED)
        db_size = os.path.getsize(db_path)
        frame_count = stats.get('frame_count', 1)
        bytes_per_frame = db_size / frame_count
        # SQLite page size (4KB default) and WAL overhead inflate this for small
        # recordings. 5000 bytes/frame accounts for fixed overhead on CI.
        assert bytes_per_frame < 5000, (
            f"DB size {bytes_per_frame:.0f} bytes/frame exceeds 5000 target")
        print(f"\n  DB size: {db_size:,} bytes, "
              f"{frame_count} frames, "
              f"{bytes_per_frame:.1f} bytes/frame")


class TestBenchMultiThread:
    """Multi-thread recording throughput."""

    WORKLOAD_MT = """\
import threading

def worker(tid, n):
    total = 0
    for i in range(n):
        total += i * tid
    return total

threads = []
for t in range(4):
    th = threading.Thread(target=worker, args=(t, 500))
    threads.append(th)
    th.start()
for th in threads:
    th.join()
"""

    def test_bench_multithread_recording(self, bench_record, benchmark):
        """Multi-thread recording throughput (informational)."""
        db_path, run_id, _ = bench_record(self.WORKLOAD_MT)
        session = _setup_session(db_path, run_id)

        def do_steps():
            for _ in range(20):
                session.step_into()

        benchmark.pedantic(do_steps, rounds=5, warmup_rounds=1)


class TestBenchReverse:
    """Reverse navigation latency."""

    def test_bench_reverse_continue(self, bench_record, benchmark):
        """reverse_continue with breakpoints: target < 50ms per hit."""
        db_path, run_id, _ = bench_record(WORKLOAD_MIXED)
        session = _setup_session(db_path, run_id)
        # Move to end
        line_seqs = _get_line_seqs(run_id)
        session.goto_frame(line_seqs[-1])
        # Set breakpoint on a line that appears multiple times
        sample = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1 OFFSET 10",
            (str(run_id),))
        if sample:
            session.set_breakpoints([{'file': sample.filename, 'line': sample.line_no}])

        def do_reverse():
            session.reverse_continue()

        benchmark.pedantic(do_reverse, rounds=10, warmup_rounds=1)


class TestBenchStack:
    """Stack reconstruction and variable access."""

    def test_bench_stack_build_deep(self, bench_record, benchmark):
        """_build_stack_at at max depth (informational)."""
        db_path, run_id, _ = bench_record(WORKLOAD_DEEP)
        session = _setup_session(db_path, run_id)
        # Find a frame at max depth
        deepest = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY call_depth DESC LIMIT 1",
            (str(run_id),))
        target_seq = deepest.sequence_no

        def do_build():
            session._stack_cache.clear()
            return session._build_stack_at(target_seq)

        benchmark.pedantic(do_build, rounds=50, warmup_rounds=2)

    def test_bench_get_variables(self, bench_record, benchmark):
        """get_variables_at (JSON parse, informational)."""
        db_path, run_id, _ = bench_record(WORKLOAD_CALLS)
        session = _setup_session(db_path, run_id)
        # Find a frame with locals
        frame = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            "   AND locals_snapshot IS NOT NULL"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        target_seq = frame.sequence_no

        def do_get_vars():
            return session.get_variables_at(target_seq)

        benchmark.pedantic(do_get_vars, rounds=100, warmup_rounds=2)
