# C Extension Guide

This documents the `pyttd_native` C extension for contributors working on the recording engine.

## Overview

The C extension (`ext/`) implements Python frame recording using two CPython hooks:

1. **PEP 523 eval hook** — intercepts frame evaluation (one `call` event per function entry)
2. **C-level trace function** — fires per-line for `line`, `return`, `exception` events

Both are installed by `start_recording()` and removed by `stop_recording()`.

## Source Files

| File | Purpose |
|------|---------|
| `pyttd_native.c` | Module init, `PyttdMethods` table |
| `recorder.c` | PEP 523 eval hook, C trace function, locals serialization, secrets redaction |
| `recorder.h` | Recorder interface, exposed globals |
| `ringbuf.c` | Lock-free SPSC ring buffer, per-thread buffer registry, string pools |
| `ringbuf.h` | Ring buffer interface |
| `binlog.c` | Binary log writer (recording) and bulk SQLite loader (stop) |
| `binlog.h` | Binlog interface |
| `sqliteflush.c` | Optional per-batch SQLite INSERT path (live mode) |
| `sqliteflush.h` | Flush interface |
| `checkpoint.c` | Fork-based checkpointing, child init, pipe IPC |
| `checkpoint.h` | Checkpoint interface |
| `checkpoint_store.c` | Checkpoint array, smallest-gap eviction, RSS tracking |
| `checkpoint_store.h` | Store interface |
| `replay.c` | Checkpoint restore (RESUME command), resume_live protocol |
| `replay.h` | Replay interface |
| `iohook.c` | I/O hooks for deterministic replay |
| `iohook.h` | I/O hook interface |
| `frame_event.h` | FrameEvent struct definition |
| `platform.h` | Platform detection macros (fork support, TLS, atomics) |

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

**Hooked functions:**
- `time.time`, `time.monotonic`, `time.perf_counter`, `time.sleep`
- `random.random`, `random.randint`, `random.uniform`, `random.gauss`, `random.choice`, `random.sample`, `random.shuffle`
- `os.urandom`
- `uuid.uuid1`, `uuid.uuid4`
- `datetime.datetime.now`, `datetime.datetime.utcnow`

**Recording mode:** hook calls original, serializes return value (IEEE 754 doubles for floats, length-prefixed for ints/bytes/strings), calls flush callback.

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

- **Python 3.12 and earlier:** `PyDict_Next()` fast path (frame locals are a real dict)
- **Python 3.13+:** `PyMapping_Items()` — works on both dict and `FrameLocalsProxy` (PEP 667). Do NOT use `PyDict_Next()` on 3.13+; it crashes on `FrameLocalsProxy`.
- **Primitives** (int, float, bool, None, short str): fast-path repr via `fast_repr()` — avoids `PyObject_Repr()` entirely.
- **Containers and objects** (dict, list, tuple, set, `@dataclass`, NamedTuple, anything with `__dict__` / `__slots__`): serialized via `serialize_expandable_value()` as a structured envelope (`__type__`, `__repr__`, `__len__`, `__children__`) so query/replay can present expandable trees.
- **Custom `__repr__`:** called once; result truncated at 256 chars (`MAX_REPR_LENGTH`). `g_inside_repr` is a TLS reentrancy guard.
- **Secret redaction:** `should_redact(name)` uses a word-boundary pattern scan; matched locals get `<redacted>`. Dict values and NamedTuple fields with secret-matching keys are also redacted. A sticky per-frame `g_frame_had_redaction` flag conservatively taints `__return__` to prevent container leaks.
- **JSON escaping** via `recorder_json_escape_string()` (quotes, backslashes, control characters).
- **Buffer overflow:** truncation at `last_complete_pos` ensures valid JSON.

The global `g_locals_buf` (64 KB) is safe because the GIL guarantees only one thread writes at a time.

### Adaptive sampling

To keep per-event cost bounded in long-running frames, locals capture is throttled after the first 16 LINE events per frame:

```
g_line_sample_counter <= 16                              → capture every line (warmup)
g_line_sample_counter <= 256  and interval 8             → every 8th event
g_line_sample_counter <= 1024 and interval 32            → every 32nd event
g_line_sample_counter <= 4096 and interval 64            → every 64th event
otherwise                         interval 128           → every 128th event
```

Newly-seen source lines always capture (tracked via `g_seen_lines` hash table) so branch coverage is preserved.

### Checkpoint multi-thread safety

`checkpoint_do_fork()` is gated on `ringbuf_thread_count() <= 1`. POSIX fork is unsafe with multiple threads — the child only inherits the calling thread, and any mutex held elsewhere is unrecoverable. Skipped checkpoints increment `g_checkpoints_skipped_threads`, surfaced to the user via the recording summary.

`arm()` attach mode defaults to `checkpoint_interval=0` (no checkpoints). `arm(checkpoints=True)` opts in, with a warmup period after `synthesize_existing_stack()` so the first checkpoint fires only once the interpreter has a real live frame to fork into.

## Key Globals

| Global | Type | Scope | Description |
|--------|------|-------|-------------|
| `g_recording` | `_Atomic int` | All threads | Recording active flag |
| `g_stop_requested` | `_Atomic int` | All threads | Interrupt flag |
| `g_sequence_counter` | `_Atomic uint64_t` | All threads | Global event counter |
| `g_frame_count` | `_Atomic uint64_t` | All threads | Total events recorded |
| `g_checkpoints_skipped_threads` | `_Atomic uint64_t` | All threads | Count of checkpoints skipped due to multi-thread guard (surfaced in stats) |
| `g_call_depth` | `PYTTD_THREAD_LOCAL int` | Per-thread | Nesting depth |
| `g_inside_repr` | `PYTTD_THREAD_LOCAL int` | Per-thread | Repr reentrancy guard |
| `g_line_sample_counter` | `PYTTD_THREAD_LOCAL int` | Per-thread | Adaptive sampling counter |
| `g_seen_lines` | `PYTTD_THREAD_LOCAL int[...]` | Per-thread | First-visit tracking for sampling |
| `g_last_exception_line` | `PYTTD_THREAD_LOCAL int` | Per-thread | Raise-site line for `exception_unwind` |
| `g_frame_had_redaction` | `PYTTD_THREAD_LOCAL int` | Per-thread | Sticky: any secret-matching local in this frame |
| `g_main_thread_id` | `unsigned long` | Main thread | For stop-request gating |
| `g_in_checkpoint` | `int` | Main thread | Checkpoint reentrancy guard |
| `g_fast_forward` | `int` | Child only | Fast-forward mode flag |
| `g_fast_forward_target` | `uint64_t` | Child only | Target sequence for FF |
| `g_attach_real_frames_start` | `_Atomic uint64_t` | All threads | In `arm()` mode, first seq past the synthesized stack (for safe checkpointing) |

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
