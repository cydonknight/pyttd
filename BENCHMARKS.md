# pyttd Benchmarks

Results captured on 2026-03-22 with Python 3.13.7
on darwin (arm64).

## Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Recording overhead (I/O-bound) | < 2x slowdown | PASS (1.6x) |
| Recording overhead (Compute-bound) | < 10x slowdown | FAIL (12.0x) |
| Recording overhead (Tight loop) | < 15x slowdown | FAIL (91.1x) |
| Recording overhead (Deep recursion) | < 10x slowdown | PASS (5.0x) |
| Recording overhead (Many locals) | < 15x slowdown | FAIL (16.2x) |
| Recording overhead (Multi-thread) | < 20x slowdown | PASS (6.7x) |
| Peak RSS | < 200 MB | PASS (103 MB) |
| Warm navigation | < 10ms/step | (run pytest benchmarks) |
| DB size per frame | < 500 bytes | (run pytest benchmarks) |
| Timeline summary | < 16ms | (run pytest benchmarks) |

## Recording Overhead

| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |
|----------|----------|----------|----------|----------|---------|
| I/O-bound | 0.173s | 0.279s | 1.6x | 37.3 MB | 1096 KB |
| Compute-bound | 0.021s | 0.247s | 12.0x | 49.9 MB | 4908 KB |
| Tight loop | 0.019s | 1.758s | 91.1x | 103.4 MB | 28320 KB |
| Deep recursion | 0.020s | 0.101s | 5.0x | 103.4 MB | 1260 KB |
| Many locals | 0.019s | 0.310s | 16.2x | 103.4 MB | 4184 KB |
| Multi-thread | 0.027s | 0.183s | 6.7x | 103.4 MB | 2572 KB |

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
