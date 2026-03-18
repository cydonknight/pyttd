"""Flush throughput benchmark.

Records a script generating many frames and measures recording overhead.
Run: .venv/bin/pytest benchmarks/bench_flush.py -v
"""
import os


WORKLOAD_FLUSH = """\
def tick(x):
    return x + 1

total = 0
for i in range(5000):
    total = tick(total)
"""


def test_bench_flush_throughput(bench_record):
    """Recording throughput (informational — includes per-frame C overhead)."""
    db_path, run_id, stats = bench_record(WORKLOAD_FLUSH)
    frame_count = stats.get('frame_count', 0)
    elapsed = stats.get('elapsed_time', 0)
    dropped = stats.get('dropped_frames', 0)
    flush_count = stats.get('flush_count', 0)
    db_size = os.path.getsize(db_path)

    assert frame_count > 10000, f"Expected >10K frames, got {frame_count}"
    assert dropped == 0, f"Dropped {dropped} frames"

    us_per_frame = (elapsed / frame_count) * 1_000_000 if frame_count else 0
    print(f"\n  Recording: {frame_count:,} frames in {elapsed:.3f}s")
    print(f"  Throughput: {us_per_frame:.1f} μs/frame "
          f"({1_000_000/us_per_frame:.0f} frames/s)" if us_per_frame else "")
    print(f"  Flushes: {flush_count}, dropped: {dropped}")
    print(f"  DB size: {db_size:,} bytes "
          f"({db_size/frame_count:.0f} bytes/frame)")

    # Sanity: recording should complete in reasonable time
    assert elapsed < 30, f"Recording took {elapsed:.1f}s (expected <30s)"
