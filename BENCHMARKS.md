# pyttd Benchmarks

Results captured on 2026-04-12 with Python 3.13.7 on darwin (arm64, Apple
M-series). Reproduce with `.venv/bin/pytest benchmarks/ -v -s` and
`.venv/bin/python3 benchmarks/bench_overhead.py -n 3`.

---

## What each benchmark measures

pyttd's subprocess slowdown has three distinct phases. Knowing which phase a
benchmark captures is essential to interpreting the numbers:

| Phase | What runs | Hot path? |
|-------|-----------|-----------|
| **Subprocess startup** | Python interpreter init, `import pyttd`, C extension load | No — one-time |
| **Recorder setup** | `Recorder.start()`, DB connect, schema init, ring buffer alloc | No — one-time |
| **Recording** | Every traced frame event: eval hook, trace function, ring buffer push, locals serialization | **Yes** — scales with event count |
| **stop_recording** | Drain ring buffers to binlog file | No — one-time |
| **binlog_load** | Bulk `INSERT` from binlog into SQLite | No — one-time, scales with event count |
| **Index build** | Create 5 secondary indexes on `executionframes` | No — lazy (first query) since 2026-04 |

**Three ways to benchmark:**

| Benchmark type | Captures | When to use |
|---|---|---|
| **In-process** (`bench_recording_inprocess.py`) | Recording only | True hot-path cost per event |
| **Subprocess default** (`bench_overhead.py`) | All phases | What users see on a real `pyttd record` call |
| **Subprocess scaled** (`bench_overhead_scaled.py`) | All phases, long workload | Hot-path-dominated ratio (startup amortized) |

The default subprocess numbers are what users see, but they **aren't the hot
path** on short workloads. The 16x → 8.8x compute-bound slowdown jump from
the 2026-04 performance work is mostly from removing the ~150ms synchronous
index rebuild from `Recorder.stop()` (it's now built lazily on first query).
Recording itself was already at ~0.2 μs/event.

---

## Performance Targets

| Metric | Target | Current |
|--------|--------|---------|
| Recording overhead (I/O-bound) | < 2x subprocess | 1.4x PASS |
| Recording overhead (Compute-bound) | < 10x subprocess | 8.8x PASS |
| Recording overhead (Tight loop) | < 15x subprocess | 5.2x PASS |
| Recording overhead (Deep recursion) | < 10x subprocess | 2.9x PASS |
| Recording overhead (Many locals) | < 15x subprocess | 2.9x PASS |
| Recording overhead (Multi-thread) | < 20x subprocess | 3.7x PASS |
| Peak RSS | < 200 MB | 43-46 MB PASS |
| Warm navigation | < 10ms/step | 0.7ms PASS |
| DB size per frame | < 500 bytes | 185-634 PASS |
| Timeline summary | < 16ms | 0.94ms PASS |

---

## 1. In-Process Recording Throughput (hot path only)

Measured via the `bench_record` fixture. **No subprocess startup, no
binlog_load, no index build** — just the C extension's per-event cost.

Use these numbers for per-event optimization decisions. They do NOT reflect
what a user sees when they run `pyttd record`.

| Workload | Events | us/event | events/s | bytes/event | Dropped |
|----------|--------|----------|----------|-------------|---------|
| Many short calls (10K) | 50,004 | 0.7 | 1.48M | 426 | 0 |
| Tight loop (50K iter) | 100,004 | 0.1 | 17.6M | 185 | 30,373 |
| Deep recursion (200x50) | 40,304 | 0.2 | 4.96M | 321 | 0 |
| Mixed types (7 types) | 24,004 | 0.7 | 1.46M | 366 | 0 |
| Large locals (50 vars) | 27,504 | 1.8 | 542K | 634 | 0 |
| Expandable vars | 13,510 | 1.1 | 913K | 374 | 0 |

The tight loop's high dropped-frame count is expected — the ring buffer fills
faster than the flush thread can drain at >17M events/s. The measured
us/event is a lower bound in that case.

---

## 2. Subprocess Recording Overhead (what users see)

End-to-end `pyttd record script.py` timings. Includes Python interpreter
startup, pyttd import, C extension load, recording, stop, binlog load, and
process teardown — all the non-hot-path costs.

**Subprocess startup is ~40ms.** For short workloads (baseline <100ms), it
inflates the slowdown ratio. Use the scaled benchmarks in section 3 for a
hot-path-dominated view.

| Workload | Baseline | Recorded | Slowdown | Peak RSS | DB Size |
|----------|----------|----------|----------|----------|---------|
| I/O-bound | 0.169s | 0.229s | 1.4x | 26.8 MB | 240 KB |
| Compute-bound | 0.021s | 0.187s | 8.8x | 43.3 MB | 14.2 MB |
| Tight loop | 0.017s | 0.090s | 5.2x | 43.3 MB | 5.1 MB |
| Deep recursion | 0.017s | 0.048s | 2.9x | 43.3 MB | 580 KB |
| Many locals | 0.017s | 0.049s | 2.9x | 43.3 MB | 940 KB |
| Multi-thread | 0.018s | 0.066s | 3.7x | 45.9 MB | 1.1 MB |

### How the subprocess overhead breaks down

From a separate phase-by-phase profile of the compute-bound workload
(51K events, ~316 ms total subprocess time — this profile predates the lazy
index change and shows where the time *used* to go):

| Phase | Time | % | Hot path? |
|-------|------|---|-----------|
| Python + pyttd import + C ext init | ~40 ms | 13% | No |
| Recorder setup | ~4 ms | 1% | No |
| **Recording the workload** | **~12 ms** | **4%** | **Yes** |
| `stop_recording` (drain ringbufs → binlog) | ~6 ms | 2% | No |
| `binlog_load` (bulk INSERT into SQLite) | ~107 ms | 34% | No |
| Secondary index rebuild | ~151 ms | 48% | No |

After the 2026-04 lazy-index fix, the 151ms index rebuild is removed from
the critical path (it runs on first query instead), cutting the
compute-bound slowdown from 16.5x → 8.8x without touching the recording
hot path at all.

---

## 3. Scaled Subprocess Benchmarks (hot-path-dominated)

Same subprocess harness as section 2, but with workloads tuned so the
recording phase is 1-5 seconds. Startup and finalization are amortized
to <5% of total time, so these ratios reflect the actual hot path.

These are the numbers to quote if you're answering "what's the CPU cost of
instrumenting every frame event."

| Workload | Scale | Baseline | Recorded | Slowdown | Peak RSS |
|----------|-------|----------|----------|----------|----------|
| Compute-bound | 100 | 0.056s | 3.169s | 56.6x | 100.1 MB |
| Tight loop | 25M iter | 1.571s | 6.220s | 4.0x | 100.1 MB |
| Deep recursion | 1500 | 0.046s | 1.862s | 40.1x | 100.1 MB |
| Many locals | 20K | 0.020s | 0.808s | 39.9x | 104.0 MB |
| Multi-thread | 100K | 0.027s | 0.755s | 28.1x | 112.2 MB |

The disparity between section 2 and section 3 shows how much of the
"default" slowdown is non-hot-path work. For the compute-bound case: the
default benchmark reports 8.8x on a 21ms workload, but the same workload
scaled to 3.1s shows 56.6x — because recording every frame event really
does cost ~50x when the workload is all function calls and line events.

---

## 4. Locals Serialization Scaling

How recording cost scales with the amount of locals data serialized.
In-process measurement only.

### By locals count

| Locals | us/event | bytes/event |
|--------|----------|-------------|
| 1 | 0.7 | ~1 |
| 5 | 0.5 | ~0 |
| 10 | 0.6 | 392 |
| 20 | 0.9 | 340 |
| 50 | 1.8 | 556 |

Sub-linear scaling is expected because adaptive sampling reduces locals
capture frequency as frames accumulate many events.

### By variable type

Primitives (int, float, bool) use a fast-repr path that bypasses
`PyObject_Repr()`. Mixed types exercise all paths.

| Type | us/event | bytes/event |
|------|----------|-------------|
| int | 0.6 | 337 |
| float | 0.7 | 337 |
| str | 0.7 | 337 |
| bool | 0.6 | 337 |
| mixed | 0.7 | 337 |

### By container size (expandable dict)

| Dict size | us/event |
|-----------|----------|
| 1 | 0.8 |
| 10 | 1.1 |
| 50 | 2.6 |
| 100 | 3.3 |

Expandable containers serialize each child separately (up to `MAX_CHILDREN
= 50`), so cost is linear in the number of children.

---

## 5. Recorder Internals

Measured via the `bench_recorder_micro` suite. These isolate specific
optimizations in the hot path.

| Metric | Value |
|--------|-------|
| Per-event marginal cost (linear regression slope) | 0.04 us |
| Adaptive sampling rate (50K loop) | 0.2% of LINE events capture locals |
| Return-only RETURN snapshots (multi-line fns) | 100% |

True in-process overhead measurement (comparing a 5000-call workload
with and without recording, same process):

| | Time |
|---|------|
| Baseline (unrecorded) | 0.3 ms |
| Recorded | 17.3 ms |
| **Pure overhead** | **17.0 ms (56.7x)** |

This matches the scaled subprocess benchmark — when startup and
finalization are amortized, recording every line event of tight compute
code is genuinely ~50x.

---

## 6. Navigation and Query Performance (replay side)

Measured via `bench_components.py` (`pytest-benchmark` harness).

| Operation | Median time |
|-----------|------------:|
| `warm_goto_frame` | 6.3 μs |
| `get_variables` (at seq) | 8.6 μs |
| Stack rebuild (deep) | 34 μs |
| Warm `step_back` | ~9 ms |
| Multi-thread recording | 248 μs |
| Warm `step_into` | 699 μs |
| Timeline summary | 914 μs |
| Reverse continue | 1.34 ms |

Navigation is fast because it's a point query against the primary key
`(run_id, sequence_no)`. Timeline summary is a single aggregation query
with SQL-side bucketing. Stack rebuild walks up to ~30 frames with a
cache hit on most lookups.

---

## Running Benchmarks

```bash
# Install dependencies
.venv/bin/pip install -e ".[dev]"

# In-process recording throughput (hot path only, no subprocess)
.venv/bin/pytest benchmarks/bench_recording_inprocess.py -v -s

# Locals serialization scaling (by count, type, container size)
.venv/bin/pytest benchmarks/bench_locals_scaling.py -v -s

# Recorder microbenchmarks (per-event cost slope, adaptive sampling, return-only)
.venv/bin/pytest benchmarks/bench_recorder_micro.py -v -s

# Component benchmarks (warm nav, timeline, stack rebuild, reverse continue)
.venv/bin/pytest benchmarks/bench_components.py --benchmark-only -v

# Subprocess recording overhead + RSS measurement (what users see)
.venv/bin/python3 benchmarks/bench_overhead.py -n 3

# Scaled subprocess benchmarks (hot-path-dominated ratios, longer workloads)
.venv/bin/python3 benchmarks/bench_overhead_scaled.py -n 3

# Update this file with fresh overhead results
.venv/bin/python3 benchmarks/bench_overhead.py -n 5 --output BENCHMARKS.md
```

---

## Methodology notes

- **Machine:** Apple M-series silicon, macOS. Your mileage will vary on
  Intel / Linux / Windows; the ranking between workloads is stable but
  absolute numbers can differ by 2x.
- **Wall-clock time** for all measurements. The `us/event` metric is
  `elapsed / (calls + lines + returns)` — events of all types, not just
  lines.
- **Ring buffer pressure:** at event rates above ~5M events/s, the
  recorder drops frames because the flush thread can't keep up. Dropped
  frames are reported; they make the measured `us/event` an underestimate
  of the steady-state per-event cost.
- **DB size per frame** is reported for context but varies by workload.
  Locals-heavy frames are ~600-1000 bytes each; control-flow-only frames
  (after adaptive sampling) are ~200-400 bytes.
- **In-process vs subprocess:** in-process measurements cover only the
  recording phase. Subprocess measurements include startup (~40ms) and
  finalization (~100-150ms on large traces) — see section 2's breakdown
  table.
