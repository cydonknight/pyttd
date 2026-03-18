#!/usr/bin/env python3
"""Standalone overhead benchmark: recording slowdown + peak RSS.

Runs workloads with and without pyttd recording, computes slowdown ratio.

Usage:
    .venv/bin/python benchmarks/bench_overhead.py [-n 5] [--output BENCHMARKS.md]
"""
import argparse
import os
import resource
import subprocess
import sys
import tempfile
import textwrap
import time


WORKLOAD_IO = """\
import time
import tempfile
import os

for i in range(100):
    time.sleep(0.001)
    fd, path = tempfile.mkstemp()
    os.write(fd, b"x" * 100)
    os.close(fd)
    os.unlink(path)
"""

WORKLOAD_COMPUTE = """\
def inner(x):
    return x * x + 1

def middle(n):
    total = 0
    for i in range(n):
        total += inner(i)
    return total

def outer():
    result = 0
    for _ in range(200):
        result += middle(50)
    return result

outer()
"""


def _python():
    return sys.executable


def _run_timed(cmd, cwd=None):
    """Run command, return (wall_seconds, max_rss_bytes)."""
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    elapsed = time.monotonic() - start
    # resource.getrusage(RUSAGE_CHILDREN) gives max RSS of all waited-for children
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    # macOS: ru_maxrss is in bytes; Linux: in KB
    rss_bytes = ru.ru_maxrss
    if sys.platform == 'linux':
        rss_bytes *= 1024
    return elapsed, rss_bytes, result.returncode


def bench_workload(name, script_content, iterations):
    """Run a workload with and without recording, return results dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = os.path.join(tmpdir, "workload.py")
        with open(script_path, 'w') as f:
            f.write(textwrap.dedent(script_content))

        # Baseline runs
        baseline_times = []
        for _ in range(iterations):
            elapsed, rss, rc = _run_timed([_python(), script_path])
            if rc != 0:
                print(f"  WARNING: baseline exited with {rc}")
            baseline_times.append(elapsed)

        # Recorded runs
        recorded_times = []
        recorded_rss = 0
        db_size = 0
        for i in range(iterations):
            # Remove DB from previous iteration
            db_path = os.path.join(tmpdir, "workload.pyttd.db")
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(db_path + suffix)
                except FileNotFoundError:
                    pass

            elapsed, rss, rc = _run_timed(
                [_python(), "-m", "pyttd", "record",
                 "--checkpoint-interval", "0", script_path])
            if rc != 0:
                print(f"  WARNING: recorded run exited with {rc}")
            recorded_times.append(elapsed)
            recorded_rss = max(recorded_rss, rss)

            if os.path.exists(db_path):
                db_size = os.path.getsize(db_path)

        baseline_mean = sum(baseline_times) / len(baseline_times)
        recorded_mean = sum(recorded_times) / len(recorded_times)
        slowdown = recorded_mean / baseline_mean if baseline_mean > 0 else float('inf')

        return {
            'name': name,
            'baseline_mean': baseline_mean,
            'recorded_mean': recorded_mean,
            'slowdown': slowdown,
            'peak_rss_mb': recorded_rss / (1024 * 1024),
            'db_size_kb': db_size / 1024,
            'iterations': iterations,
        }


def format_results(results):
    """Format results as a markdown table."""
    lines = []
    lines.append("| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |")
    lines.append("|----------|----------|----------|----------|----------|---------|")
    for r in results:
        lines.append(
            f"| {r['name']} "
            f"| {r['baseline_mean']:.3f}s "
            f"| {r['recorded_mean']:.3f}s "
            f"| {r['slowdown']:.1f}x "
            f"| {r['peak_rss_mb']:.1f} MB "
            f"| {r['db_size_kb']:.0f} KB |"
        )
    return '\n'.join(lines)


def write_benchmarks_md(results, output_path):
    """Write full BENCHMARKS.md with results."""
    io_status = 'PASS' if results[0]['slowdown'] < 2 else 'FAIL'
    cpu_status = 'PASS' if results[1]['slowdown'] < 10 else 'FAIL'
    peak_rss = max(r['peak_rss_mb'] for r in results)
    rss_status = 'PASS' if peak_rss < 200 else 'FAIL'
    overhead_table = format_results(results)

    content = f"""# pyttd Benchmarks

Results captured on {time.strftime('%Y-%m-%d')} with Python {sys.version.split()[0]}
on {sys.platform} ({os.uname().machine}).

## Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Recording overhead (I/O-bound) | < 2x slowdown | {io_status} ({results[0]['slowdown']:.1f}x) |
| Recording overhead (compute-bound) | < 10x slowdown | {cpu_status} ({results[1]['slowdown']:.1f}x) |
| Peak RSS | < 200 MB | {rss_status} ({peak_rss:.0f} MB) |
| Warm navigation | < 10ms/step | (run pytest benchmarks) |
| DB size per frame | < 500 bytes | (run pytest benchmarks) |
| Timeline summary | < 16ms | (run pytest benchmarks) |

## Recording Overhead

{overhead_table}

Note: Subprocess startup overhead (~200ms for importing pyttd + compiling C extension)
inflates ratios for fast workloads. The compute-bound workload baseline is only ~20ms.

## Running Benchmarks

```bash
# Install dependencies
.venv/bin/pip install -e ".[dev]"

# Component benchmarks (warm nav, timeline, DB size, stack, variables, flush)
.venv/bin/pytest benchmarks/ --benchmark-only -v

# All benchmarks including non-benchmark tests (DB size, flush throughput)
.venv/bin/pytest benchmarks/ -v -s

# Recording overhead + RSS measurement
.venv/bin/python3 benchmarks/bench_overhead.py -n 5

# Update this file with fresh results
.venv/bin/python3 benchmarks/bench_overhead.py -n 5 --output BENCHMARKS.md
```
"""
    with open(output_path, 'w') as f:
        f.write(content)
    print(f"Wrote {output_path}")


def main():
    parser = argparse.ArgumentParser(description='pyttd overhead benchmarks')
    parser.add_argument('-n', '--iterations', type=int, default=5,
                        help='number of iterations per workload (default: 5)')
    parser.add_argument('--output', type=str, default=None,
                        help='write results to BENCHMARKS.md at this path')
    args = parser.parse_args()

    print(f"Running overhead benchmarks ({args.iterations} iterations each)...\n")

    results = []
    for name, script in [('I/O-bound', WORKLOAD_IO),
                          ('Compute-bound', WORKLOAD_COMPUTE)]:
        print(f"  {name}...")
        r = bench_workload(name, script, args.iterations)
        print(f"    baseline={r['baseline_mean']:.3f}s  "
              f"recorded={r['recorded_mean']:.3f}s  "
              f"slowdown={r['slowdown']:.1f}x  "
              f"RSS={r['peak_rss_mb']:.1f}MB  "
              f"DB={r['db_size_kb']:.0f}KB")
        results.append(r)

    print(f"\n{format_results(results)}")

    if args.output:
        write_benchmarks_md(results, args.output)


if __name__ == '__main__':
    main()
