# pyttd Benchmarks

Results captured on 2026-03-22 with Python 3.13.7
on darwin (arm64).

## Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Recording overhead (I/O-bound) | < 2x slowdown | FAIL (3.6x) |
| Recording overhead (Compute-bound) | < 10x slowdown | FAIL (13.3x) |
| Recording overhead (Tight loop) | < 15x slowdown | FAIL (532.1x) |
| Recording overhead (Deep recursion) | < 10x slowdown | PASS (5.1x) |
| Recording overhead (Many locals) | < 15x slowdown | FAIL (16.0x) |
| Recording overhead (Multi-thread) | < 20x slowdown | PASS (8.8x) |
| Peak RSS | < 200 MB | PASS (141 MB) |
| Warm navigation | < 10ms/step | (run pytest benchmarks) |
| DB size per frame | < 500 bytes | (run pytest benchmarks) |
| Timeline summary | < 16ms | (run pytest benchmarks) |

## Recording Overhead

| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |
|----------|----------|----------|----------|----------|---------|
| I/O-bound | 0.159s | 0.578s | 3.6x | 50.0 MB | 5180 KB |
| Compute-bound | 0.019s | 0.250s | 13.3x | 50.5 MB | 5228 KB |
| Tight loop | 0.019s | 10.167s | 532.1x | 141.1 MB | 129116 KB |
| Deep recursion | 0.019s | 0.097s | 5.1x | 141.1 MB | 1352 KB |
| Many locals | 0.019s | 0.308s | 16.0x | 141.1 MB | 4276 KB |
| Multi-thread | 0.020s | 0.175s | 8.8x | 141.1 MB | 2980 KB |

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
