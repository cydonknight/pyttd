# C Extension Guide

This documents the `pyttd_native` C extension for contributors working on the recording engine.

## Overview

The C extension (`ext/`) implements Python frame recording using two CPython hooks:

1. **PEP 523 eval hook** — intercepts frame evaluation (one `call` event per function entry)
2. **C-level trace function** — fires per-line for `line`, `return`, `exception` events

Both are installed by `start_recording()` and removed by `stop_recording()`.

## Source Files

| File | Purpose | Lines |
|------|---------|-------|
| `pyttd_native.c` | Module init, method table | ~50 |
| `recorder.c` | Eval hook, trace function, flush thread, locals serialization | ~1400 |
| `recorder.h` | Recorder interface, exposed globals | ~120 |
| `ringbuf.c` | Lock-free SPSC ring buffer, string pools | ~400 |
| `ringbuf.h` | Ring buffer interface | ~80 |
| `checkpoint.c` | Fork-based checkpointing, child init, pipe IPC | ~470 |
| `checkpoint.h` | Checkpoint interface | ~30 |
| `checkpoint_store.c` | Checkpoint array, eviction, lifecycle | ~250 |
| `checkpoint_store.h` | Store interface | ~40 |
| `replay.c` | Checkpoint restore (RESUME command) | ~150 |
| `replay.h` | Replay interface | ~10 |
| `iohook.c` | I/O hooks for deterministic replay | ~400 |
| `iohook.h` | I/O hook interface | ~20 |
| `frame_event.h` | FrameEvent struct definition | ~20 |
| `platform.h` | Platform detection macros | ~60 |

## PEP 523 Eval Hook

`pyttd_eval_hook()` in `recorder.c` replaces CPython's default frame evaluator:

```
Frame entry
  │
  ├── Check g_recording (atomic, relaxed)
  ├── Check g_stop_requested (main thread only → KeyboardInterrupt)
  ├── Check g_inside_repr (skip if inside repr)
  ├── should_ignore(filename, funcname)?
  │     ├── YES: save trace → remove trace → eval → restore trace
  │     └── NO: continue below
  │
  ├── Increment g_call_depth (TLS)
  ├── Record "call" event → ringbuf_push
  ├── Trigger checkpoint if interval elapsed (guard: g_in_checkpoint)
  ├── Save current trace, install pyttd_trace_func (skip if already installed)
  ├── Call original eval function (via saved pointer)
  ├── If eval returned NULL + PyErr_Occurred → record "exception_unwind" (BEFORE depth--)
  ├── Decrement g_call_depth
  └── Restore previous trace function
```

### Key Invariant: Depth Ownership

The eval hook **alone** manages `g_call_depth`. It increments before calling the original eval and decrements after. The trace function never touches `call_depth`. This provides a single unconditional decrement point regardless of how the frame exits.

### Key Invariant: exception_unwind Timing

`exception_unwind` is recorded **before** decrementing `g_call_depth`, so it has the same depth as other events in that frame (`call`, `line`, `exception`).

## Trace Function

`pyttd_trace_func()` fires for per-line events within non-ignored frames:

| Event | Action |
|-------|--------|
| `PyTrace_CALL` | **Skipped** — eval hook already recorded the call |
| `PyTrace_LINE` | Serialize locals, record event, trigger checkpoint, signal flush if buffer 75% full |
| `PyTrace_RETURN` | Skip if `arg == NULL` (exception propagation). Otherwise record with `__return__` extra |
| `PyTrace_EXCEPTION` | Record with `__exception__` extra |

### Stop Request Handling

The trace function checks `g_stop_requested` using `atomic_exchange_explicit` (not separate load + store). This ensures the flag is cleared atomically, so `KeyboardInterrupt` fires exactly once — preventing re-raises in except/finally handlers.

## Frame Filtering

`should_ignore()` checks (in order):

1. `strncmp(filename, "<frozen ", 8)` — catches all CPython frozen modules
2. pyttd package directory (computed at runtime from `tracing/constants.py`)
3. Stdlib directories (`lib/python`, `site-packages`)
4. Specific filenames/function names

Ignored frames get special trace handling: save current trace → remove trace → eval → restore trace. This prevents sub-frames from inheriting the trace function.

## Ring Buffer

Per-thread SPSC (Single Producer, Single Consumer) ring buffer with C11 atomics:

- **Producer** — recording thread pushes events via `ringbuf_push_to()`
- **Consumer** — flush thread pops batches via `ringbuf_pop_batch_from()`
- **Capacity** — power-of-2 (default 65536), waste-one-slot for full detection
- **Drop-on-full** — if the buffer is full, the event is dropped and `dropped_frames` incremented

### String Pools

Double-buffered string pools prevent use-after-free:

- All strings in `FrameEvent` (`filename`, `function_name`, `locals_json`) are **copied** into the active producer pool
- Raw Python string pointers become stale before the flush thread reads them
- Pool swap happens **inside the GIL section** of `flush_batch()` to serialize with the producer

Main thread: 2x 8MB pools. Secondary threads: 2x 2MB pools.

### Multi-Thread Design

Each thread gets its own ring buffer, allocated lazily on first frame entry via `ringbuf_get_or_create()`. A global linked list connects all per-thread buffers. The flush thread iterates this list to drain all buffers.

When a thread exits, its pthread destructor marks the buffer as `orphaned`. The flush thread drains remaining events, then marks the buffer slot for reuse.

## Checkpointing

### Fork Process

`checkpoint_do_fork()` in `checkpoint.c`:

1. Create command/result pipes
2. Pause flush thread (condvar protocol)
3. Re-acquire GIL (flush thread is paused, no contention)
4. `PyOS_BeforeFork()` — required for Python 3.13+ PyMutex-based GIL
5. `fork()`
6. **Parent:** `PyOS_AfterFork_Parent()`, resume flush thread, add to store, call Python callback
7. **Child:** `PyOS_AfterFork_Child()`, `checkpoint_child_init()`, enter command loop

### Child Initialization

`checkpoint_child_init()` performs extensive cleanup:

- Reinitialize thread identity
- Disable recording (`g_recording = 0`)
- Reset I/O hook state
- Ignore signals (SIGINT, SIGTERM, SIGPIPE)
- Clear inherited trace function
- Reinitialize pthread objects
- Destroy ring buffer (not needed in child)
- Close inherited checkpoint pipe FDs
- Clear atexit handlers

### Checkpoint Store

Static array `g_store[32]`. Eviction uses **smallest-gap thinning**: sort by `sequence_no`, find the pair with the smallest gap, evict the earlier one. Never evicts the most recent checkpoint. This provides O(log N) coverage.

### Fast-Forward Mode

Dedicated `pyttd_eval_hook_fast_forward()` and `pyttd_trace_func_fast_forward()` that count `g_sequence_counter` without serialization or ring buffer writes. When the target sequence is reached, the child serializes state and sends the result via pipe.

## I/O Hooks

Module attribute replacement via `PyObject_SetAttrString()`:

**Hooked functions:** `time.time`, `time.monotonic`, `time.perf_counter`, `random.random`, `random.randint`, `os.urandom`

**Recording mode:** hook calls original, serializes return value (IEEE 754 doubles for floats, length-prefixed for ints/bytes), calls flush callback.

**Replay mode:** `iohook_enter_replay_mode()` pre-loads IOEvents from DB. Hooks return pre-loaded values from a cursor.

### Adding New I/O Hooks

1. Add a new hook function in `iohook.c` following the pattern of existing hooks
2. Save the original function pointer in a `static PyObject *`
3. In recording mode: call original, serialize return value, call `io_flush_callback`
4. In replay mode: deserialize from cursor
5. Register in `install_io_hooks_internal()` and `remove_io_hooks_internal()`
6. Add test cases in `tests/test_iohook.py`

## Locals Serialization

`recorder_serialize_locals()` builds a JSON dict from frame locals:

- Python 3.12: `PyDict_Next()` fast path (frame locals are a real dict)
- Python 3.13+: `PyMapping_Items()` (works on both dict and `FrameLocalsProxy` from PEP 667)
- Each value: `repr()` call with `g_inside_repr` reentrancy guard, truncated at 256 chars
- JSON escaping via `recorder_json_escape_string()` (quotes, backslashes, control characters)
- Buffer overflow: truncation at `last_complete_pos` ensures valid JSON

The global `g_locals_buf` (64 KB) is safe because the GIL guarantees only one thread writes at a time.

## Key Globals

| Global | Type | Scope | Description |
|--------|------|-------|-------------|
| `g_recording` | `_Atomic int` | All threads | Recording active flag |
| `g_stop_requested` | `_Atomic int` | All threads | Interrupt flag |
| `g_sequence_counter` | `_Atomic uint64_t` | All threads | Global event counter |
| `g_frame_count` | `_Atomic uint64_t` | All threads | Total events recorded |
| `g_call_depth` | `PYTTD_THREAD_LOCAL int` | Per-thread | Nesting depth |
| `g_inside_repr` | `PYTTD_THREAD_LOCAL int` | Per-thread | Repr reentrancy guard |
| `g_main_thread_id` | `unsigned long` | Main thread | For stop-request gating |
| `g_in_checkpoint` | `int` | Main thread | Checkpoint reentrancy guard |
| `g_fast_forward` | `int` | Child only | Fast-forward mode flag |
| `g_fast_forward_target` | `uint64_t` | Child only | Target sequence for FF |

## Debugging

### ASAN Build

```bash
CFLAGS="-fsanitize=address -fno-omit-frame-pointer" \
LDFLAGS="-fsanitize=address" \
pip install -e .
```

### Common Issues

- **Segfault in locals serialization** — usually a NULL return from `PyUnicode_AsUTF8` or `PyObject_Repr`. Check NULL guards
- **"unlocking mutex that is not locked"** — missing `PyOS_BeforeFork()`/`PyOS_AfterFork_Child()` calls around `fork()`
- **Stale string pointers** — forgot to copy string into pool; pointer valid at push time but stale at flush time
- **Recursive recording** — `g_inside_repr` or `g_in_checkpoint` guard missing
- **PyEval_SetTrace overflow** — calling `PyEval_SetTrace` on every frame entry overflows Python 3.13's internal counter. Check `saved_trace != pyttd_trace_func` before re-installing

### Version-Gated APIs

Use `#if PY_VERSION_HEX` for version-specific code:

```c
#if PY_VERSION_HEX >= 0x030F0000  // 3.15+
    PyUnstable_InterpreterState_GetEvalFrameFunc(...)
#else  // 3.12-3.14
    _PyInterpreterState_GetEvalFrameFunc(...)
#endif
```

Do NOT use `#include <internal/pycore_frame.h>`. Use public `PyUnstable_InterpreterFrame_*` accessors.

## Portability Notes

- **Windows:** no `fork()`, no checkpoints. `#ifdef PYTTD_HAS_FORK` guards all fork-related code
- **macOS:** fork works but only if single-threaded at fork time
- **Thread-local storage:** `_Thread_local` (C11) on Unix, `__declspec(thread)` on Windows, via `PYTTD_THREAD_LOCAL` macro
- **Atomics:** C11 `<stdatomic.h>` (GCC, Clang, MSVC 2022+)
- **Byte order:** `pyttd_htobe64`/`pyttd_be64toh` in `platform.h` with fallbacks

## See Also

- [Architecture](../architecture.md) — system overview and data flow
- [Building](building.md) — compilation instructions
- [Protocol Reference](protocol.md) — JSON-RPC and pipe protocols
