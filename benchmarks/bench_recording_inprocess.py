"""In-process recording throughput benchmarks.

Measures C extension recording overhead without subprocess startup noise.
Each test records a workload via the bench_record fixture and reports
per-event metrics.

Run: .venv/bin/pytest benchmarks/bench_recording_inprocess.py -v -s
"""
import os


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report(stats, db_path, label=""):
    fc = stats.get('frame_count', 0)
    elapsed = stats.get('elapsed_time', 0.001)
    dropped = stats.get('dropped_frames', 0)
    us = elapsed / fc * 1_000_000 if fc else 0
    db_size = os.path.getsize(db_path)
    bpf = db_size / fc if fc else 0
    evts_sec = fc / elapsed if elapsed else 0
    print(f"\n  {label}: {fc:,} events in {elapsed:.3f}s")
    print(f"    {us:.1f} us/event | {evts_sec:,.0f} events/s | {bpf:.0f} bytes/event | dropped: {dropped}")
    return fc, elapsed, us, bpf


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

WORKLOAD_MANY_CALLS = """\
def f(x):
    return x + 1

for i in range(10000):
    f(i)
"""

WORKLOAD_TIGHT_LOOP = """\
total = 0
for i in range(50000):
    total += i * i + 1
"""

WORKLOAD_DEEP_RECURSION = """\
def recurse(n):
    if n <= 0:
        return 0
    return recurse(n - 1) + 1

for _ in range(50):
    recurse(200)
"""

WORKLOAD_MIXED_TYPES = """\
def work():
    a = 42
    b = 3.14
    c = "hello"
    d = True
    e = None
    f = [1, 2, 3]
    g = {"x": 1}
    return a + b

for _ in range(2000):
    work()
"""

WORKLOAD_EXPANDABLE = """\
class Obj:
    def __init__(self, x):
        self.x = x
        self.y = [1, 2, 3]

def work(n):
    d = {"a": 1, "b": [1, 2], "c": {"nested": True}}
    lst = [Obj(i) for i in range(5)]
    return len(d) + len(lst)

for i in range(500):
    work(i)
"""


def _make_large_locals_workload(n=50):
    assigns = "\n    ".join(f"v{i} = {i}" for i in range(n))
    total = " + ".join(f"v{i}" for i in range(n))
    return f"def work():\n    {assigns}\n    return {total}\n\nfor _ in range(500):\n    work()\n"


WORKLOAD_LARGE_LOCALS = _make_large_locals_workload(50)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordingInprocess:

    def test_bench_many_short_calls(self, bench_record):
        """10K short function calls: CALL+LINE+RETURN cycle, return-only opt."""
        db_path, _, stats = bench_record(WORKLOAD_MANY_CALLS)
        fc, _, us, _ = _report(stats, db_path, "Many short calls")
        assert fc > 30000
        assert stats.get('dropped_frames', 0) == 0
        assert us < 50, f"Per-event cost {us:.1f} us exceeds 50 us target"

    def test_bench_tight_loop(self, bench_record):
        """50K loop iterations: pure LINE event overhead + adaptive sampling."""
        db_path, _, stats = bench_record(WORKLOAD_TIGHT_LOOP)
        fc, _, us, _ = _report(stats, db_path, "Tight loop 50K")
        assert fc > 10000  # may drop some frames under high event rate

    def test_bench_deep_recursion(self, bench_record):
        """200-depth recursion x50: eval hook + filter cache pressure."""
        db_path, _, stats = bench_record(WORKLOAD_DEEP_RECURSION)
        fc, _, us, _ = _report(stats, db_path, "Deep recursion")
        assert fc > 10000
        assert stats.get('dropped_frames', 0) == 0

    def test_bench_mixed_types(self, bench_record):
        """int/float/str/bool/None/list/dict locals: fast_repr coverage."""
        db_path, _, stats = bench_record(WORKLOAD_MIXED_TYPES)
        fc, _, us, _ = _report(stats, db_path, "Mixed types")
        assert fc > 5000
        assert stats.get('dropped_frames', 0) == 0

    def test_bench_large_locals(self, bench_record):
        """50 local variables: serialization buffer scaling."""
        db_path, _, stats = bench_record(WORKLOAD_LARGE_LOCALS)
        fc, _, us, bpf = _report(stats, db_path, "Large locals (50 vars)")
        assert fc > 1000
        assert stats.get('dropped_frames', 0) == 0

    def test_bench_expandable_vars(self, bench_record):
        """Nested containers + user objects: expandable serialization."""
        db_path, _, stats = bench_record(WORKLOAD_EXPANDABLE)
        fc, _, us, bpf = _report(stats, db_path, "Expandable vars")
        assert fc > 1000
        assert stats.get('dropped_frames', 0) == 0
