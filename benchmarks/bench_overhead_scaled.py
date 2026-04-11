#!/usr/bin/env python3
"""Scaled overhead benchmark: workloads with baseline >= 500ms.

Eliminates the startup-inflation problem in bench_overhead.py where ~200ms
subprocess startup dominates 20ms workloads, making slowdown ratios unreliable.

Reports both slowdown ratios AND absolute recording work (ms).

Usage:
    .venv/bin/python3 benchmarks/bench_overhead_scaled.py [-n 3] [--quick]
"""
import argparse
import os
import resource
import subprocess
import sys
import tempfile
import textwrap
import time


# ---------------------------------------------------------------------------
# Scaled workloads (target ~500ms-2s baseline)
# ---------------------------------------------------------------------------

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

for _ in range({scale}):
    outer()
"""

WORKLOAD_TIGHT_LOOP = """\
total = 0
for i in range({scale}):
    total += i * i + 1
"""

WORKLOAD_DEEP_RECURSION = """\
import sys
sys.setrecursionlimit(2000)

def recurse(n):
    if n <= 0:
        return 0
    return recurse(n - 1) + 1

for _ in range({scale}):
    recurse(500)
"""

WORKLOAD_MANY_LOCALS = """\
def many_vars(n):
    a, b, c, d, e = 1, 2, 3, 4, 5
    f, g, h, i, j = 6, 7, 8, 9, 10
    k, l, m, o, p = 11, 12, 13, 14, 15
    q, r, s, t, u = 16, 17, 18, 19, 20
    total = a + b + c + d + e + f + g + h + i + j
    total += k + l + m + o + p + q + r + s + t + u
    return total + n

for x in range({scale}):
    many_vars(x)
"""

WORKLOAD_MULTITHREAD = """\
import threading

def worker(tid, iterations):
    total = 0
    for i in range(iterations):
        total += i * tid
    return total

threads = []
for t in range(4):
    th = threading.Thread(target=worker, args=(t, {scale}))
    threads.append(th)
    th.start()
for th in threads:
    th.join()
"""

# Scales: (name, template, full_scale, quick_scale)
# Event-heavy workloads (compute, recursion) can't reach long baselines
# without generating millions of events. Focus on the Work (ms) column
# (= recorded - baseline) which isolates recording overhead from startup.
# Tight loop scales best because it's single-frame with minimal events/CPU.
WORKLOADS = [
    ('Compute-bound',  WORKLOAD_COMPUTE,       100,    30),
    ('Tight loop',     WORKLOAD_TIGHT_LOOP,    25000000, 5000000),
    ('Deep recursion', WORKLOAD_DEEP_RECURSION, 1500,  400),
    ('Many locals',    WORKLOAD_MANY_LOCALS,    20000, 5000),
    ('Multi-thread',   WORKLOAD_MULTITHREAD,    100000, 25000),
]

# Performance targets (slowdown multiplier) — these should be more honest
# than the original bench_overhead.py targets because startup is diluted.
TARGETS = {
    'Compute-bound': 20,
    'Tight loop': 10,
    'Deep recursion': 5,
    'Many locals': 10,
    'Multi-thread': 10,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _python():
    return sys.executable


def _run_timed(cmd, cwd=None):
    """Run command, return (wall_seconds, max_rss_bytes, returncode)."""
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    elapsed = time.monotonic() - start
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
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
        for _ in range(iterations):
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

        baseline_mean = sum(baseline_times) / len(baseline_times)
        recorded_mean = sum(recorded_times) / len(recorded_times)
        slowdown = recorded_mean / baseline_mean if baseline_mean > 0 else float('inf')
        work_ms = (recorded_mean - baseline_mean) * 1000

        return {
            'name': name,
            'baseline_mean': baseline_mean,
            'recorded_mean': recorded_mean,
            'slowdown': slowdown,
            'work_ms': work_ms,
            'peak_rss_mb': recorded_rss / (1024 * 1024),
            'iterations': iterations,
        }


def format_results(results):
    """Format results as a markdown table."""
    lines = []
    lines.append("| Workload | Baseline | Recorded | Slowdown | Work (ms) | Peak RSS |")
    lines.append("|----------|----------|----------|----------|-----------|----------|")
    for r in results:
        lines.append(
            f"| {r['name']} "
            f"| {r['baseline_mean']:.3f}s "
            f"| {r['recorded_mean']:.3f}s "
            f"| {r['slowdown']:.1f}x "
            f"| {r['work_ms']:.0f} ms "
            f"| {r['peak_rss_mb']:.1f} MB |"
        )
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='Scaled overhead benchmarks (baseline >= 500ms)')
    parser.add_argument('-n', '--iterations', type=int, default=3,
                        help='iterations per workload (default: 3)')
    parser.add_argument('--quick', action='store_true',
                        help='use smaller scale for faster CI runs')
    parser.add_argument('--json', type=str, default=None,
                        help='write results as JSON to this path')
    args = parser.parse_args()

    scale_idx = 3 if args.quick else 2  # quick_scale or full_scale
    print(f"Running scaled overhead benchmarks "
          f"({'quick' if args.quick else 'full'} scale, "
          f"{args.iterations} iterations each)...\n")

    results = []
    for name, template, full_scale, quick_scale in WORKLOADS:
        scale = quick_scale if args.quick else full_scale
        script = template.format(scale=scale)
        print(f"  {name} (scale={scale})...")
        r = bench_workload(name, script, args.iterations)
        target = TARGETS.get(name, 20)
        status = 'PASS' if r['slowdown'] < target else 'FAIL'
        r['target'] = target
        r['status'] = status
        print(f"    baseline={r['baseline_mean']:.3f}s  "
              f"recorded={r['recorded_mean']:.3f}s  "
              f"slowdown={r['slowdown']:.1f}x  "
              f"work={r['work_ms']:.0f}ms  "
              f"target=<{target}x [{status}]  "
              f"RSS={r['peak_rss_mb']:.1f}MB")
        results.append(r)

    print(f"\n{format_results(results)}")

    if args.json:
        import json
        with open(args.json, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == '__main__':
    main()
