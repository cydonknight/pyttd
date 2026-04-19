# FAQ

## General

### What Python versions are supported?

Python 3.12 and later. pyttd uses `PyUnstable_InterpreterFrame_*` accessors that were added in CPython 3.12.

### Does pyttd work with async/await code?

pyttd records all frame events including async frames. However, async-specific navigation (e.g., "step to next await") is not implemented. Standard step/continue works through async code.

### Can I use pyttd in production?

pyttd is a **development tool**. It captures `repr()` of all local variables (with secrets redaction), adds significant overhead on compute-heavy code (see [Performance](../README.md#performance)), and creates large trace databases. Do not use in production for always-on tracing. For targeted capture — e.g., attaching briefly to reproduce a suspected bug — `arm()` / `disarm()` with `--include`-scoped recording can be acceptable.

### Does pyttd work with C extensions?

pyttd records Python-level frame events only. Calls into C extensions appear as a single `call`/`return` pair — the C code itself is not traced. Local variables are captured before the call and after the return.

### Does pyttd work with GUI frameworks (tkinter, PyQt)?

pyttd uses a post-mortem replay model: the script runs to completion before you navigate. GUI applications that run an event loop will record normally, but the recording only ends when the application exits (or you press Ctrl+C).

### How much overhead does recording add?

Measured on Python 3.13 (Apple Silicon), hot-path-dominated (recorded workload 1-5 seconds so startup amortizes):

- **I/O-bound:** ~1.4x slowdown
- **Tight loops with adaptive sampling:** ~4x slowdown
- **Compute-bound (worst case, every frame is a line event):** ~40-57x slowdown
- **Peak RSS:** ~45 MB for unscoped recordings, scales with DB size

The 57x compute-bound ceiling is the honest cost of recording every line event. **Use `--include` or `--include-file` scoping for compute-heavy code** — realistic scoping drops overhead to 2-5x. See [BENCHMARKS.md](../BENCHMARKS.md) for the full breakdown (in-process vs default subprocess vs scaled subprocess).

## Recording

### Can I record only specific functions?

Three ways:

1. **CLI scoping** — `pyttd record --include my_func` or `--include-file 'path/*.py'`. Most flexible, works with any script.
2. **`@ttdbg` decorator** — annotate a function, its execution is recorded.
3. **`arm()` / `disarm()`** — start/stop recording from anywhere inside a running process; captures inline code via stack synthesis.

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    ...
```

Without any scoping, the CLI records all user code (stdlib and site-packages are filtered by default).

### Why doesn't pyttd record stdlib/third-party code?

pyttd filters out standard library, site-packages, and frozen modules to keep recordings focused on user code. This dramatically reduces database size and noise. The filtering happens in `should_ignore()` in the C extension.

### What's the `PYTTD_RECORDING` environment variable?

During recording, pyttd sets `PYTTD_RECORDING=1`. Your script can check this:

```python
import os
if os.environ.get('PYTTD_RECORDING'):
    # Skip expensive operations during recording
    pass
```

### How are variables captured?

pyttd captures locals at `line` events with a mix of fast-path formatting and structured serialization:

- Primitives (int, float, bool, None) use a fast-repr path that bypasses `PyObject_Repr()`
- Containers (dicts, lists, tuples, sets) and objects with `__dict__` / `__slots__` (including `@dataclass(slots=True)` and `NamedTuple`) are captured as expandable trees — you can drill into them via `--expand` in CLI queries, `expand VARNAME` in the REPL, or the Variables panel in VSCode
- Custom `__repr__` methods are invoked once; reentrant `__repr__` calls (where `__repr__` itself triggers recording) are guarded against
- Large values are truncated at 256 characters per field (container children get their own 256-char budgets, so you can still drill in)
- Adaptive sampling reduces capture frequency in long-running frames. Use `--var-history` for gap-free tracking of specific variables

## Navigation

### What's the difference between warm and cold navigation?

- **Warm** — reads from the SQLite database. Sub-millisecond. Used for step/continue/step_back/reverse_continue
- **Cold** — restores a `fork()` checkpoint and fast-forwards the child process. 50-300ms. Used for large jumps via `goto_frame`

You don't choose between them — pyttd picks automatically based on the operation.

### Why is step_back always fast?

`step_back` is always warm: it reads the previous `line` event from SQLite. No checkpoint restore needed.

### Can I evaluate expressions during replay?

Yes. The Variables panel shows locals at the current position. Hover shows variable values. The Debug Console supports expression evaluation.

However, evaluation operates on recorded `repr()` snapshots — it can look up variable values but cannot execute arbitrary Python against live state.

### How does multi-thread navigation work?

- `step_over` and `step_out` stay on the **current thread**
- `step_into` and `step_back` follow **global sequence order** (may cross threads)
- The Threads panel shows all recorded threads
- Stack reconstruction filters by the target thread's ID

### What are `exception_unwind` events?

An `exception_unwind` event is recorded when a frame exits via exception propagation (the exception was not caught in that frame). It's distinct from `exception` events, which fire when an exception is first raised.

`exception_unwind` events now carry the raise-site line number (stashed by the trace function's `PyTrace_EXCEPTION` handler in TLS and read at unwind time), so both event types report correct lines.

## Platform

### Why doesn't cold navigation work on Windows?

Cold navigation uses `fork()` to create process snapshots. Windows doesn't support `fork()`. All navigation on Windows is warm-only (SQLite reads), which works but means `goto_frame` for distant frames is slower.

### Why are checkpoints skipped during multi-thread recording on macOS?

`fork()` is unsafe when multiple threads are active — POSIX rules only preserve async-signal-safe behavior in the child, so any mutex or C-extension lock held by another thread at fork time would be unrecoverable. pyttd's checkpoint trigger guards on `ringbuf_thread_count() <= 1` and skips the fork otherwise. The CLI surfaces a `checkpoints_skipped_threads` count in the recording summary. Cold navigation falls back to warm-only for those regions.

**Mitigations:** scope recording with `--include-file` so that only main-thread user code is instrumented (background-thread events don't count toward the guard), or use `arm(checkpoints=True)` during a known-quiesced region. See [troubleshooting.md#checkpoints](troubleshooting.md#checkpoints) for more.

## See Also

- [Troubleshooting](troubleshooting.md) — fixing common problems
- [Architecture](architecture.md) — system internals
- [Getting Started](getting-started.md) — first recording walkthrough
