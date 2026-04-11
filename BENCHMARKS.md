# pyttd Benchmarks

Results captured on 2026-04-05 with Python 3.13.7
on darwin (arm64).

## Performance Targets

| Metric | Target | Status |
|--------|--------|--------|
| Recording overhead (I/O-bound) | < 2x slowdown | PASS (1.4x) |
| Recording overhead (Compute-bound) | < 10x slowdown | FAIL (16.5x) |
| Recording overhead (Tight loop) | < 15x slowdown | PASS (7.9x) |
| Recording overhead (Deep recursion) | < 10x slowdown | PASS (2.9x) |
| Recording overhead (Many locals) | < 15x slowdown | PASS (3.2x) |
| Recording overhead (Multi-thread) | < 20x slowdown | PASS (3.9x) |
| Peak RSS | < 200 MB | PASS (58 MB) |
| Warm navigation | < 10ms/step | (run pytest benchmarks) |
| DB size per frame | < 500 bytes | (run pytest benchmarks) |
| Timeline summary | < 16ms | (run pytest benchmarks) |

## Recording Overhead (subprocess)

These numbers include ~200ms subprocess startup that inflates ratios for fast workloads.
See the in-process benchmarks below for startup-free measurements.

| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |
|----------|----------|----------|----------|----------|---------|
| I/O-bound | 0.166s | 0.224s | 1.4x | 27.4 MB | 468 KB |
| Compute-bound | 0.020s | 0.334s | 16.5x | 58.2 MB | 30456 KB |
| Tight loop | 0.017s | 0.135s | 7.9x | 58.2 MB | 11456 KB |
| Deep recursion | 0.017s | 0.049s | 2.9x | 58.2 MB | 1228 KB |
| Many locals | 0.017s | 0.055s | 3.2x | 58.2 MB | 1648 KB |
| Multi-thread | 0.018s | 0.071s | 3.9x | 58.2 MB | 2416 KB |

Note: Subprocess startup overhead (~200ms for importing pyttd + compiling C extension)
inflates ratios for fast workloads. The compute-bound workload baseline is only ~20ms.

## In-Process Recording Throughput

Measured via `bench_record` fixture — no subprocess startup. Reports the C extension's
actual recording cost.

| Workload | Events | us/event | events/s | bytes/event |
|----------|--------|----------|----------|-------------|
| Many short calls (10K) | 50,004 | 0.9 | 1.08M | 798 |
| Tight loop (50K iter) | 100,004 | 0.1 | 13.2M | 473 |
| Deep recursion (200x50) | 40,304 | 0.2 | 4.3M | 647 |
| Mixed types (7 types) | 24,004 | 0.8 | 1.2M | 801 |
| Large locals (50 vars) | 27,504 | 2.9 | 349K | 995 |
| Expandable vars | 13,510 | 1.1 | 878K | 682 |

True in-process overhead: 5K function calls take 0.3ms unrecorded vs 24ms recorded (75x).
The tight loop's 0.1 us/event includes dropped frames under ring buffer pressure at 50K iterations.

## Locals Serialization Scaling

### By locals count

| Locals | us/event | bytes/event |
|--------|----------|-------------|
| 1 | 1.0 | 673 |
| 5 | 0.8 | 545 |
| 10 | 0.8 | 563 |
| 20 | 1.2 | 787 |
| 50 | 2.9 | 990 |

### By variable type

All types use fast-path repr (int/float/bool bypass `PyObject_Repr()`).

| Type | us/event | bytes/event |
|------|----------|-------------|
| int | 0.9 | 709 |
| float | 1.0 | 709 |
| str | 1.0 | 709 |
| bool | 0.9 | 709 |
| mixed | 0.9 | 709 |

### By container size (expandable serialization)

| Dict size | us/event | bytes/event |
|-----------|----------|-------------|
| 1 | 1.3 | - |
| 10 | 1.4 | - |
| 50 | 2.9 | 1,340 |
| 100 | 3.5 | 1,340 |

## Recorder Internals

### Per-event cost

Measured via linear regression over 1K/10K/100K loop recordings:

| Metric | Value |
|--------|-------|
| Per-event marginal cost (slope) | 0.05 us |
| Adaptive sampling rate (50K loop) | 0.9% of LINE events |
| Return-only RETURN snapshots | 100% (multi-line functions) |

### Type repr throughput

| Type | us/event |
|------|----------|
| int | 0.8 |
| float | 0.8 |
| str | 0.8 |
| dict | 1.0 |

## Running Benchmarks

```bash
# Install dependencies
.venv/bin/pip install -e ".[dev]"

# In-process recording throughput
.venv/bin/pytest benchmarks/bench_recording_inprocess.py -v -s

# Locals serialization scaling
.venv/bin/pytest benchmarks/bench_locals_scaling.py -v -s

# Recorder microbenchmarks (per-event cost, adaptive sampling, return-only)
.venv/bin/pytest benchmarks/bench_recorder_micro.py -v -s

# Component benchmarks (warm nav, timeline, DB size, stack, variables, flush)
.venv/bin/pytest benchmarks/ --benchmark-only -v

# All benchmarks including non-benchmark tests
.venv/bin/pytest benchmarks/ -v -s

# Subprocess recording overhead + RSS measurement
.venv/bin/python3 benchmarks/bench_overhead.py -n 5

# Scaled subprocess benchmarks (longer baselines, honest ratios)
.venv/bin/python3 benchmarks/bench_overhead_scaled.py -n 3

# Update this file with fresh overhead results
.venv/bin/python3 benchmarks/bench_overhead.py -n 5 --output BENCHMARKS.md
```
