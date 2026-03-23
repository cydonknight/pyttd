# pyttd Benchmarks

Results captured on 2026-03-22 with Python 3.13.7
on darwin (arm64).

## Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Recording overhead (I/O-bound) | < 2x slowdown | PASS (1.6x) |
| Recording overhead (Compute-bound) | < 10x slowdown | PASS (4.7x) |
| Recording overhead (Tight loop) | < 15x slowdown | FAIL (56.2x) |
| Recording overhead (Deep recursion) | < 10x slowdown | PASS (3.0x) |
| Recording overhead (Many locals) | < 15x slowdown | PASS (11.7x) |
| Recording overhead (Multi-thread) | < 20x slowdown | PASS (4.1x) |
| Peak RSS | < 200 MB | PASS (50 MB) |
| Warm navigation | < 10ms/step | (run pytest benchmarks) |
| DB size per frame | < 500 bytes | (run pytest benchmarks) |
| Timeline summary | < 16ms | (run pytest benchmarks) |

## Recording Overhead

| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |
|----------|----------|----------|----------|----------|---------|
| I/O-bound | 0.170s | 0.271s | 1.6x | 27.7 MB | 1092 KB |
| Compute-bound | 0.020s | 0.093s | 4.7x | 32.4 MB | 4904 KB |
| Tight loop | 0.018s | 0.983s | 56.2x | 50.4 MB | 28316 KB |
| Deep recursion | 0.016s | 0.049s | 3.0x | 50.4 MB | 1256 KB |
| Many locals | 0.017s | 0.196s | 11.7x | 50.4 MB | 4180 KB |
| Multi-thread | 0.018s | 0.073s | 4.1x | 50.4 MB | 2572 KB |

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
