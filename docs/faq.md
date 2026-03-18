# FAQ

## General

### What Python versions are supported?

Python 3.12 and later. pyttd uses `PyUnstable_InterpreterFrame_*` accessors that were added in CPython 3.12.

### Does pyttd work with async/await code?

pyttd records all frame events including async frames. However, async-specific navigation (e.g., "step to next await") is not implemented. Standard step/continue works through async code.

### Can I use pyttd in production?

pyttd is a **development tool**. It captures `repr()` of all local variables (which may include secrets), adds significant overhead (2-12x slowdown), and creates large trace databases. Do not use in production.

### Does pyttd work with C extensions?

pyttd records Python-level frame events only. Calls into C extensions appear as a single `call`/`return` pair — the C code itself is not traced. Local variables are captured before the call and after the return.

### Does pyttd work with GUI frameworks (tkinter, PyQt)?

pyttd uses a post-mortem replay model: the script runs to completion before you navigate. GUI applications that run an event loop will record normally, but the recording only ends when the application exits (or you press Ctrl+C).

### How much overhead does recording add?

Measured on Python 3.13 (Apple Silicon):

- **I/O-bound workloads:** ~2.5x slowdown
- **Compute-bound workloads:** ~10-12x slowdown
- **Peak RSS:** ~48 MB for the ring buffer and string pools

See [BENCHMARKS.md](../BENCHMARKS.md) for detailed numbers.

## Recording

### Can I record only specific functions?

Yes, use the `@ttdbg` decorator:

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    ...
```

This records only the decorated function's execution. The CLI (`pyttd record`) records all user code.

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

pyttd calls `repr()` on every local variable at each `line` event and stores the result as a JSON string. This means:

- Custom `__repr__` methods are invoked during recording
- Values are flat strings, not expandable object trees
- Large objects produce large `repr()` output (truncated at 256 chars per value)
- Reentrant `__repr__` calls (where `__repr__` itself triggers recording) are guarded against

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

Known limitation: the `line_no` on `exception_unwind` is from the function entry, not the exception site. Cross-reference with the preceding `exception` event for the correct line.

## Platform

### Why doesn't cold navigation work on Windows?

Cold navigation uses `fork()` to create process snapshots. Windows doesn't support `fork()`. All navigation on Windows is warm-only (SQLite reads), which works but means `goto_frame` for distant frames is slower.

### Why are checkpoints skipped during multi-thread recording on macOS?

`fork()` is unsafe when multiple threads are active (the forked child inherits thread state that can't be safely resumed). pyttd detects when multiple threads are recording and skips checkpoint creation. Cold navigation falls back to warm-only for those regions.

## See Also

- [Troubleshooting](troubleshooting.md) — fixing common problems
- [Architecture](architecture.md) — system internals
- [Getting Started](getting-started.md) — first recording walkthrough
