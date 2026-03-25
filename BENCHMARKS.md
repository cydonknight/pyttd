# pyttd Benchmarks

Results captured on 2026-03-23 with Python 3.13.7
on darwin (arm64).

## Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Recording overhead (I/O-bound) | < 2x slowdown | PASS (1.3x) |
| Recording overhead (Compute-bound) | < 10x slowdown | PASS (4.9x) |
| Recording overhead (Tight loop) | < 15x slowdown | PASS (3.5x) |
| Recording overhead (Deep recursion) | < 10x slowdown | PASS (2.8x) |
| Recording overhead (Many locals) | < 15x slowdown | PASS (3.1x) |
| Recording overhead (Multi-thread) | < 20x slowdown | PASS (3.8x) |
| Peak RSS | < 200 MB | PASS (46 MB) |
| Warm navigation | < 10ms/step | (run pytest benchmarks) |
| DB size per frame | < 500 bytes | (run pytest benchmarks) |
| Timeline summary | < 16ms | (run pytest benchmarks) |

## Recording Overhead

| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |
|----------|----------|----------|----------|----------|---------|
| I/O-bound | 0.169s | 0.225s | 1.3x | 26.6 MB | 492 KB |
| Compute-bound | 0.019s | 0.095s | 4.9x | 33.4 MB | 4880 KB |
| Tight loop | 0.018s | 0.061s | 3.5x | 33.4 MB | 2488 KB |
| Deep recursion | 0.018s | 0.049s | 2.8x | 33.4 MB | 1236 KB |
| Many locals | 0.018s | 0.055s | 3.1x | 33.4 MB | 1684 KB |
| Multi-thread | 0.018s | 0.067s | 3.8x | 46.2 MB | 2436 KB |

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
