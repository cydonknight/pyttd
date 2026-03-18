# pyttd Build Plan: C Extension + VSCode Time-Travel Debugger

## Context

pyttd is a Python time-travel debugger. **Phases 0 and 1 are complete.** The C extension recorder is fully functional: PEP 523 frame eval hook captures call/line/return/exception/exception_unwind events via a lock-free ring buffer, flushed to SQLite via Peewee. CLI `record` and `query` commands work. 28 tests pass. The old `sys.settrace` code has been replaced.

### Prerequisites

- **Python >= 3.12** installed (required for `PyUnstable_InterpreterFrame_*` C API accessors) — using 3.13.7
- **C compiler** (gcc or clang on Unix, MSVC on Windows)
- **Node.js >= 18** and npm (for VSCode extension development — not currently installed on dev machine)
- **VSCode** (for testing the extension; CLI works standalone)

### Virtual Environment

The project uses `.venv/` (created in Phase 0). The old `venv/` directory is gitignored.

```bash
.venv/bin/pip install -e ".[dev]"    # Compiles C extension + installs peewee, pytest, rich
.venv/bin/python -m pyttd record script.py
.venv/bin/pytest tests/
.venv/bin/pip install -e .           # Recompile after C changes
```

**Convention:** Never use bare `pip` or `python` — always `.venv/bin/pip` and `.venv/bin/python`.

## Architecture

```
VSCode Extension Host (single Node.js process)
  +-----------------------------------------------------+
  |  Extension (TypeScript)                              |
  |  - Registers providers, manages lifecycle            |
  |  +-----------------------------------------------+  |
  |  | Debug Adapter (inline, LoggingDebugSession)    |  |
  |  | - Translates DAP messages <-> JSON-RPC         |  |
  |  +--------------------+--------------------------+   |
  |                       | JSON-RPC over TCP (localhost) |
  |  +-----------------------------------------------+  |
  |  | WebviewViewProvider, CodeLensProvider,          |  |
  |  | InlineValuesProvider, TreeDataProvider          |  |
  |  +-----------------------------------------------+  |
  +-----------------------------------------------------+
                          | TCP socket
  +-----------------------------------------------------+
  |  Python TTD Backend (child process)                  |
  |  +------------------+  +---------------------------+ |
  |  | pyttd_native     |  | pyttd (Python)            | |
  |  | (C extension)    |  | - server.py (JSON-RPC)    | |
  |  | - recorder.c     |  | - session.py (navigation) | |
  |  | - checkpoint.c   |  | - runner.py (script exec) | |
  |  | - replay.c       |  | - models/ (Peewee ORM)    | |
  |  | - iohook.c       |  | - query.py (trace data)   | |
  |  +------------------+  +---------------------------+ |
  +-----------------------------------------------------+
```

The debug adapter runs **inline** in the extension host process via `DebugAdapterInlineImplementation`. The `@vscode/debugadapter` package's `LoggingDebugSession` class implements the `vscode.DebugAdapter` interface, so it works directly as an inline adapter (this is the pattern used by vscode-mock-debug). This eliminates a separate Node process but means adapter bugs affect the extension host — acceptable for development, and the adapter logic is thin enough to be low-risk.

**Session lifecycle:**
1. User presses F5 with a `pyttd` launch configuration
2. Extension's `DebugAdapterDescriptorFactory` creates a `DebugAdapterInlineImplementation(new PyttdDebugSession())`
3. Debug Adapter spawns `python -m pyttd serve --script <path> --cwd <dir>` as a child process
4. The Python server binds a TCP socket on localhost (port 0 = OS-assigned), writes a single line `PYTTD_PORT:<port>\n` to stdout, then redirects stdout/stderr via `os.dup2()` to capture pipes. The adapter reads this line, parses the port, and connects via TCP for all subsequent JSON-RPC communication. The user script's stdout/stderr are captured and forwarded as JSON-RPC `output` events over TCP — they never touch the child process's actual stdout/stderr after the handshake.
5. Adapter sends `launch` RPC; server stores config but does NOT start recording yet
6. Adapter sends `InitializedEvent` to VSCode; VSCode sends breakpoint configuration
7. Adapter receives `configurationDone` -> sends `configuration_done` RPC -> server begins recording (executes user script with C frame eval hook active)
8. During recording, the server sends `progress` events over TCP (frame count, elapsed time). User can click Stop (`pauseRequest` -> adapter sends `interrupt` RPC -> server stops recording early).
9. When the script completes (or is interrupted), server sends `stopped` notification — user enters **replay mode**
10. In replay mode, all navigation (step forward/back, continue/reverse-continue, goto frame, scrub timeline) operates on the recorded trace. Forward stepping walks forward through recorded frames; backward stepping walks backward. Neither re-executes user code (except during cold checkpoint restore for state reconstruction).
11. `disconnect` DAP request -> adapter sends `disconnect` RPC -> server kills all checkpoint children, closes socket, exits

**Replay mode vs. live debugging:** This is a **post-mortem / record-replay debugger**, not a live debugger. The script runs to completion (or interruption) before interactive debugging begins. Breakpoints set before recording mark positions for forward-continue and reverse-continue to stop at during replay. Live breakpoints that pause execution mid-recording are a future enhancement.

### Server Concurrency Model

The Python backend uses **two threads** during recording:

1. **RPC thread** (main thread): Runs a `select()`-based event loop on the TCP socket. Handles incoming JSON-RPC requests (`interrupt`, `set_breakpoints`, etc.) and sends outgoing notifications (`progress`, `output`). During recording, most RPC requests are deferred until replay mode — only `interrupt` and `set_breakpoints` are processed immediately.

2. **Recording thread**: Spawned when `configuration_done` RPC is received. Calls `runner.run_script()` which executes the user script with the C frame eval hook active. When the script completes (or is interrupted), this thread calls `recorder.stop()`, then posts a `recording_complete` message to the main thread's event queue via the wakeup pipe.

**Interrupt mechanism:** The `interrupt` RPC handler calls `pyttd_native.request_stop()` which sets an atomic `g_stop_requested` flag in `recorder.c`. The frame eval hook checks this flag at the top of each invocation. When set, the hook raises `KeyboardInterrupt` via `PyErr_SetNone(PyExc_KeyboardInterrupt)` and returns `NULL`, which unwinds the script back to the recording thread's `runner.run_script()` call. The recording thread catches the exception, calls `recorder.stop()`, and signals recording complete. This avoids the Python `threading.Event` approach which would require the C eval hook to periodically re-acquire the GIL to check a Python object.

The C flush thread (a third thread, managed by the C extension) wakes periodically to batch-insert frames from the ring buffer into SQLite. It acquires the GIL only during the Python-side `batch_insert` call.

After recording completes, the recording thread exits and the RPC thread handles all replay navigation in a single-threaded loop.

**Signal handling:** The server registers `signal.signal(signal.SIGINT, handler)` and `signal.signal(signal.SIGTERM, handler)` in the main thread. The handler calls `pyttd_native.request_stop()` (if recording is active) to interrupt the user script via the C atomic flag, then sets `self._shutdown = True` and writes to the wakeup pipe to unblock the selector. The event loop exits on the next iteration, triggering graceful cleanup: wait for recording thread to finish, kill checkpoint children, close socket, exit. The recording thread catches the resulting `KeyboardInterrupt`, calls `recorder.stop()`, and signals completion via the wakeup pipe — same path as the `interrupt` RPC handler.

### Checkpoint IPC Model

When the user rewinds to an arbitrary frame that requires state reconstruction (cold navigation), the server restores a checkpoint. The **pipe-based relay** pattern is used:

```
Parent (server.py, owns TCP socket to Debug Adapter)
  |
  | write (RESUME, target_seq) to command pipe
  v
Child (checkpoint, frozen at frame N)
  | wakes, fast-forwards to target_seq
  | serializes frame state as JSON
  | writes to result pipe (chunked if > 64KB to avoid pipe buffer limits)
  | re-freezes: blocks on cmd_pipe waiting for next command
  v
Parent reads result pipe, relays to Debug Adapter via JSON-RPC
```

The parent process always remains the sole owner of the Debug Adapter TCP connection. Checkpoint children are workers that produce state on demand. After producing a result, the child re-freezes (blocks on the command pipe) so it can serve subsequent requests within its checkpoint window. The child only exits when the parent sends a `DIE` command (when the user jumps outside this child's window, or the session ends).

For sequential rewinds (user steps back multiple times within one checkpoint window), the parent sends incremental `(STEP, -1)` commands to the already-warm child instead of restoring a different checkpoint.

**Pre-fork synchronization:** Before forking, the recorder must pause the ring buffer flush thread. The mechanism:
1. Parent sets an atomic `pause_requested` flag
2. Flush thread checks this flag at the top of each iteration; when set, it signals a `pause_ack` condition variable and blocks on a `resume` condition variable
3. Parent waits on `pause_ack`, then calls `fork()`
4. After fork, parent signals `resume` to wake the flush thread
5. The child process does NOT have a flush thread (threads don't survive `fork()`) — the child only needs the frozen process state

**Fast-forward execution flow:** When a checkpoint child receives `(RESUME, target_seq)`, control returns from the `read(cmd_pipe)` blocking call back into the checkpoint function, which sets the frame eval hook to fast-forward mode and returns. The C call stack unwinds back into the Python interpreter, which resumes executing the user's script from the checkpointed frame. In fast-forward mode, the frame eval hook increments a counter but does NOT serialize locals or write to the ring buffer — pure counting. At `target_seq`, the hook serializes full frame state, writes to the result pipe, then blocks again on `read(cmd_pipe)`. The user's code IS re-executed between the checkpoint frame and the target frame — which is why I/O hooks (Phase 4) are needed for deterministic state at the target.

### Checkpoint Eviction Strategy

With a default interval of 1000 frames and a max of 32 live checkpoints, a naive FIFO eviction means only the last 32,000 frames have checkpoint coverage. For longer recordings, earlier frames become unreachable via cold navigation.

**Exponential thinning strategy:** Instead of evicting the oldest checkpoint, retain checkpoints at exponentially-spaced intervals from the current position:

```
Keep: latest, latest-1000, latest-2000, latest-4000, latest-8000, latest-16000, ...
```

This provides O(log N) checkpoints covering the full recording, with higher density near the current position (where the user is most likely navigating). When a checkpoint is no longer needed by the spacing policy, it's evicted.

**Fallback for frames without checkpoint coverage:** If no checkpoint covers the target frame (e.g., on Windows, or if all checkpoints have been evicted), the system falls back to **warm-only navigation** — it reads the recorded frame data from SQLite. The user sees `repr()` snapshots of variables but cannot get live object state. The adapter reports this via a status message: "Limited: displaying recorded snapshots (no checkpoint available for this frame)."

### Platform Constraints

| Platform | Status | Notes |
|---|---|---|
| **Linux** | Full support | `fork()` reliable for single-threaded processes. Primary development target. |
| **macOS** | Partial support | `fork()` works if single-threaded at fork time. Python 3.12+ emits `DeprecationWarning` for fork with active threads. pyttd warns and skips checkpoint if threads detected. |
| **Windows** | Record + browse only | No `fork()`. Recording and warm navigation (SQLite-backed frame browsing) work. Cold navigation unavailable. Future: `CreateProcess` + serialized state, out of scope for v1. |

The C extension compiles on all three platforms. `checkpoint.c` and `replay.c` are conditionally compiled: `#ifdef PYTTD_HAS_FORK` (defined in `platform.h` for non-Windows). Stubs return `PYTTD_ERR_NO_FORK` on Windows.

### Trace Database

**Location:** By default, `<script_name>.pyttd.db` in the same directory as the user script. Configurable via `traceDb` in launch.json.

**Lifecycle:** Each recording creates or overwrites the DB for that script. Previous recordings for the same script are lost unless the user specifies a different `traceDb` path. The DB is not deleted after the session — the user can re-open it for offline analysis via the CLI (`pyttd query`).

**Size management:** For long recordings, the DB can grow large (500 bytes/frame * 1M frames = ~500MB). The CLI and extension should display DB size after recording. A future enhancement: configurable max frame count with circular buffer semantics (drop oldest frames).

**Required indexes** (added to `ExecutionFrames` model):
- `(run_id, sequence_no)` — primary navigation index, used by step forward/back
- `(run_id, filename, line_no)` — breakpoint matching, used by continue/reverse-continue
- `(run_id, function_name)` — CodeLens stats queries

---

## What Stays vs. Changes (Post Phase 0+1)

All Phase 0 and Phase 1 changes are complete. The table below reflects the final state:

| File | Status | Phase |
|---|---|---|
| `models/base.py` | **DONE** — deferred `SqliteDatabase(None)` | 0 |
| `models/storage.py` | **DONE** — module functions, dynamic DB path, batch_insert | 0 |
| `models/constants.py` | **DONE** — `DB_NAME_SUFFIX`, synchronous=1, busy_timeout | 0 |
| `models/frames.py` | **DONE** — AutoField PK, new fields, 5 composite indexes | 0 |
| `models/runs.py` | **DONE** — callable defaults, script_path, total_frames | 0 |
| `tracing/enums.py` | **DONE** — added EXCEPTION_UNWIND, removed OPCODE | 0 |
| `tracing/trace_func.py` | **DELETED** | 1 |
| `tracing/trace.py` | **DELETED** | 1 |
| `main.py` | **DONE** — @ttdbg uses C extension Recorder | 1 |
| `models/frames.old.py` | **DELETED** | 0 |
| `pyttd/runs.py` (empty) | **DELETED** | 0 |
| `samplecode/__init__.py` | **DELETED** | 0 |
| `requirements.txt` | **DELETED** | 0 |
| `__init__.py` (repo root) | **DELETED** | 0 |
| `TTdebug.db` | **DELETED** | 0 |
| `TODO` | **DELETED** | 0 |

## Project Structure (Final)

```
pyttd/                           # Repository root
  ext/                           # C extension source
    pyttd_native.c               # Module init (PyInit_pyttd_native), Python bindings
    recorder.c/h                 # PEP 523 frame eval hook (3.12+)
    ringbuf.c/h                  # Lock-free SPSC ring buffer
    frame_event.h                # FrameEvent struct
    checkpoint.c/h               # Fork-based checkpoint manager (Unix only)
    checkpoint_store.c/h         # Checkpoint index/lifecycle/eviction
    replay.c/h                   # Checkpoint resume + fast-forward
    iohook.c/h                   # Intercept time/random/file for deterministic replay
    platform.h                   # Platform detection macros (PYTTD_HAS_FORK, etc.)
  pyttd/                         # Python package
    __init__.py                  # Exports __version__
    __main__.py                  # Enables `python -m pyttd`
    main.py                      # @ttdbg decorator, public API
    cli.py                       # CLI: record, query, replay, serve subcommands
    server.py                    # JSON-RPC server (TCP socket)
    protocol.py                  # JSON-RPC framing (Content-Length over TCP)
    session.py                   # Session state, frame navigation, query delegation
    query.py                     # Query API for trace data
    recorder.py                  # Python wrapper around C recorder
    replay.py                    # Python replay controller
    runner.py                    # User script execution (runpy-based)
    config.py                    # Configuration management
    errors.py                    # Custom exceptions
    models/                      # Peewee ORM (existing, evolved)
      __init__.py
      base.py, frames.py, runs.py, storage.py, constants.py
      checkpoints.py             # NEW: checkpoint index model (Phase 2)
      io_events.py               # NEW: I/O event model (Phase 4)
      timeline.py                # NEW: timeline summary queries (Phase 5)
    tracing/                     # Existing, kept for config
      __init__.py
      enums.py, constants.py
    performance/                 # Existing (dead code removed in Phase 0+1 review)
      __init__.py
  vscode-pyttd/                  # VSCode extension (TypeScript)
    package.json                 # Extension manifest
    tsconfig.json
    .vscodeignore
    .vscode/launch.json          # Dev launch config for Extension Development Host
    src/
      extension.ts               # Activation: register all providers + factories
      debugAdapter/
        pyttdDebugSession.ts     # LoggingDebugSession subclass (core DAP impl, inline)
        backendConnection.ts     # Manages Python child process, TCP JSON-RPC
        types.ts                 # TypeScript interfaces
      views/
        timelineScrubberProvider.ts  # WebviewViewProvider (Phase 5)
        timelineScrubber.html/js/css # Timeline webview (Phase 5)
      providers/
        codeLensProvider.ts      # Execution stats per function (Phase 6)
        inlineValuesProvider.ts  # Variable values inline during debug (Phase 6)
        callHistoryProvider.ts   # TreeDataProvider for call tree (Phase 6)
        decorationProvider.ts    # TextEditorDecorationType for inline values (Phase 6)
  tests/                         # Python tests
    __init__.py
    conftest.py                  # Shared fixtures (tmp_path DB, sample scripts)
    test_models.py
    test_native_stub.py
    test_recorder.py             # Phase 1
    test_ringbuf.py              # Phase 1
    test_checkpoint.py           # Phase 2
    test_replay.py               # Phase 2
    test_server.py               # Phase 3
    test_session.py              # Phase 3
    test_iohook.py               # Phase 4
  samplecode/                    # Test scripts (no __init__.py — not a package)
  setup.py                       # C extension build (ext_modules)
  pyproject.toml                 # Project metadata, dependencies, build system
  .gitignore
```

**C extension module name:** `pyttd_native` — a top-level module (`import pyttd_native`). This is intentional: building a C extension as a sub-module (`pyttd._native`) requires the source file layout to match the package structure, complicating the build. A top-level name is simpler for setuptools `ext_modules`. The `pyttd_` prefix avoids namespace pollution. The init function is `PyInit_pyttd_native()`.

**Dependency note:** The existing code uses `rich` for print-based tracing, which is fully replaced by the C extension. `rich` can be removed from runtime dependencies after Phase 1. Keep it as a dev dependency if useful for CLI output formatting.

---

## Phase 0: Foundation Cleanup & Scaffolding — COMPLETE

**Goal:** Fix bugs, initialize git, set up monorepo, scaffold C extension stubs + VSCode extension skeleton. Pressing F5 in Extension Development Host shows "pyttd" in the debug type dropdown.

> **Status:** All steps completed. Git initialized, all bugs fixed, build system created, C stubs compile, VSCode skeleton created, 14 tests passing (7 model + 7 native stub).

### Initialize Repository

```bash
git init
```

Create `.gitignore` before first commit.

### Create Virtual Environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip setuptools wheel
```

After `pyproject.toml` and `setup.py` are created (below), install the project in editable mode with dev dependencies:
```bash
.venv/bin/pip install -e ".[dev]"
```

This compiles the C extension stubs and installs `peewee`, `pytest`, and `rich`.

### Delete

- `pyttd/models/frames.old.py` — superseded by Peewee version
- `pyttd/runs.py` — empty file at package root, confusing alongside `pyttd/models/runs.py`
- `samplecode/__init__.py` — samplecode is not a package, just a directory of test scripts
- `requirements.txt` — replaced by `pyproject.toml`
- `__init__.py` (repo root) — this is not a package
- `TTdebug.db` — leftover test artifact
- `TODO` — superseded by this build plan
- All `__pycache__/` directories (recursively) — stale bytecode from the old `venv/`; `.gitignore` prevents future ones but existing ones must be removed before the initial commit. Use: `find . -path ./venv -prune -o -type d -name __pycache__ -print -exec rm -rf {} +`

**Note:** After Phase 0's `storage.py` rewrite, `pyttd/main.py` will have broken imports (`from pyttd.models.storage import db_conn` — the `db_conn` singleton no longer exists). This is expected and intentional: `main.py` is evolved in Phase 1 to use the new `Recorder` API. Do NOT import or test `main.py` in Phase 0. Similarly, `samplecode/basic_sample_function.py` imports `from pyttd.main import ttdbg` — this will also break. Clean up the sample file in this phase (see Modify section below).

### Create

New files created in Phase 0 (details below). Note: `pyttd/__init__.py` already exists (empty) and is listed here with its new content — overwrite it.
- `.gitignore`
- `pyproject.toml` + `setup.py` (build system)
- `ext/` directory — all C stub files (`pyttd_native.c`, `recorder.c/h`, `ringbuf.c/h`, `checkpoint.c/h`, `checkpoint_store.c/h`, `replay.c/h`, `iohook.c/h`, `platform.h`, `frame_event.h`)
- `pyttd/__main__.py` — enables `python -m pyttd`
- `pyttd/cli.py` — CLI stub (full implementation in Phase 1)
- `pyttd/config.py` — configuration dataclass
- `pyttd/errors.py` — custom exceptions
- `pyttd/performance/__init__.py` — makes `performance/` a proper package (currently missing)
- `tests/__init__.py`, `tests/conftest.py`, `tests/test_models.py`, `tests/test_native_stub.py`
- `vscode-pyttd/` directory — full extension scaffold (package.json, tsconfig.json, src/*.ts)

**`.gitignore`:**
```
__pycache__/
*.pyc
*.egg-info/
dist/
build/
venv/
.venv/
node_modules/
out/
*.vsix
*.o
*.so
*.pyd
*.dylib
*.pyttd.db
*.pyttd.db-wal
*.pyttd.db-shm
TTdebug.db
```

**Build system — `pyproject.toml` + `setup.py`:**

`pyproject.toml` handles project metadata and dependencies. `setup.py` handles `ext_modules` because setuptools' pyproject.toml-only C extension support is limited. Both files are required.

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "pyttd"
version = "0.1.0"
description = "Python Time-Travel Debugger"
requires-python = ">=3.12"
license = "MIT"
dependencies = [
    "peewee>=3.17",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "rich",
]

[project.scripts]
pyttd = "pyttd.cli:main"

[tool.setuptools.packages.find]
include = ["pyttd*"]
```

```python
# setup.py
import sys
from setuptools import setup, Extension

if sys.version_info < (3, 12):
    raise SystemExit("pyttd requires Python 3.12 or later")

pyttd_native = Extension(
    "pyttd_native",
    sources=[
        "ext/pyttd_native.c",
        "ext/recorder.c",
        "ext/ringbuf.c",
        "ext/checkpoint.c",
        "ext/checkpoint_store.c",
        "ext/replay.c",
        "ext/iohook.c",
    ],
    include_dirs=["ext"],
)

setup(ext_modules=[pyttd_native])
```

**C extension stubs — `ext/` directory:**

All C files compile and export Python-callable methods that return `None` or raise `NotImplementedError`. This validates the build pipeline.

`ext/platform.h`:
```c
#ifndef PYTTD_PLATFORM_H
#define PYTTD_PLATFORM_H

#ifdef _WIN32
  /* Windows: no fork support */
#else
  #define PYTTD_HAS_FORK 1
#endif

#define PYTTD_ERR_NO_FORK -1

#endif /* PYTTD_PLATFORM_H */
```

`ext/frame_event.h` — FrameEvent struct:
```c
#ifndef PYTTD_FRAME_EVENT_H
#define PYTTD_FRAME_EVENT_H

#include <stdint.h>

typedef struct {
    uint64_t sequence_no;
    int line_no;
    int call_depth;
    const char *filename;
    const char *function_name;
    const char *event_type;     /* "call", "line", "return", "exception", "exception_unwind" */
    const char *locals_json;    /* serialized repr() of locals, or NULL */
    double timestamp;           /* monotonic clock, seconds since recording start */
} FrameEvent;

#endif /* PYTTD_FRAME_EVENT_H */
```

**Timestamp clock source:** Use a monotonic clock relative to recording start. On Linux/macOS: `clock_gettime(CLOCK_MONOTONIC)`. On Windows: `QueryPerformanceCounter`. Store `start_time` in `start_recording()`, stamp each event as `current_time - start_time` (seconds, double precision). This provides sub-microsecond resolution for timeline display and performance analysis. Do NOT use `gettimeofday` / `time()` — wall clock is affected by NTP adjustments.

`ext/pyttd_native.c` — Module init with stubs for all Python-facing functions:
```c
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "platform.h"
#include "frame_event.h"

/* Include headers — each declares the functions defined in its respective .c file.
 * These must NOT be static since they are defined in separate translation units. */
#include "recorder.h"
#include "ringbuf.h"
#include "checkpoint.h"
#include "checkpoint_store.h"
#include "replay.h"
#include "iohook.h"

static PyMethodDef PyttdMethods[] = {
    {"start_recording", (PyCFunction)pyttd_start_recording, METH_VARARGS | METH_KEYWORDS,
     "Start recording with frame eval hook. Args: flush_callback, buffer_size, flush_interval_ms"},
    {"stop_recording", (PyCFunction)pyttd_stop_recording, METH_NOARGS,
     "Stop recording and flush ring buffer"},
    {"get_recording_stats", (PyCFunction)pyttd_get_recording_stats, METH_NOARGS,
     "Return dict with frame_count, elapsed_time, etc."},
    {"set_ignore_patterns", (PyCFunction)pyttd_set_ignore_patterns, METH_VARARGS,
     "Set filename/function patterns to ignore during recording"},
    {"request_stop", (PyCFunction)pyttd_request_stop, METH_NOARGS,
     "Set atomic stop flag checked by frame eval hook (for interrupt)"},
    {"create_checkpoint", (PyCFunction)pyttd_create_checkpoint, METH_NOARGS,
     "Fork a checkpoint (Unix only)"},
    {"restore_checkpoint", (PyCFunction)pyttd_restore_checkpoint, METH_VARARGS,
     "Find nearest checkpoint <= target_seq, resume child, fast-forward, return state dict"},
    {"kill_all_checkpoints", (PyCFunction)pyttd_kill_all_checkpoints, METH_NOARGS,
     "Send DIE to all checkpoint children"},
    {"install_io_hooks", (PyCFunction)pyttd_install_io_hooks, METH_NOARGS,
     "Replace non-deterministic functions with recording hooks"},
    {"remove_io_hooks", (PyCFunction)pyttd_remove_io_hooks, METH_NOARGS,
     "Restore original functions"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef pyttd_module = {
    PyModuleDef_HEAD_INIT,
    "pyttd_native",
    "Python Time-Travel Debugger native extension",
    -1,
    PyttdMethods
};

PyMODINIT_FUNC PyInit_pyttd_native(void) {
    #if PY_VERSION_HEX < 0x030C0000
    PyErr_SetString(PyExc_ImportError, "pyttd requires Python 3.12 or later");
    return NULL;
    #endif
    return PyModule_Create(&pyttd_module);
}
```

Each stub .c file (`recorder.c`, `ringbuf.c`, `checkpoint.c`, `checkpoint_store.c`, `replay.c`, `iohook.c`) contains the stub implementations. **Functions must NOT use `static` since they are referenced from `pyttd_native.c` (a different translation unit).** For example:

`ext/recorder.h`:
```c
#ifndef PYTTD_RECORDER_H
#define PYTTD_RECORDER_H
#include <Python.h>

PyObject *pyttd_start_recording(PyObject *self, PyObject *args, PyObject *kwargs);
PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_set_ignore_patterns(PyObject *self, PyObject *args);
PyObject *pyttd_request_stop(PyObject *self, PyObject *Py_UNUSED(args));

#endif
```

`ext/recorder.c`:
```c
#include <Python.h>
#include "platform.h"
#include "frame_event.h"
#include "recorder.h"

/* Phase 1: PEP 523 frame eval hook + C-level trace function + ring buffer integration */

PyObject *pyttd_start_recording(PyObject *self, PyObject *args, PyObject *kwargs) {
    PyErr_SetString(PyExc_NotImplementedError, "start_recording not yet implemented");
    return NULL;
}

PyObject *pyttd_stop_recording(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "stop_recording not yet implemented");
    return NULL;
}

PyObject *pyttd_get_recording_stats(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "get_recording_stats not yet implemented");
    return NULL;
}

PyObject *pyttd_set_ignore_patterns(PyObject *self, PyObject *args) {
    PyErr_SetString(PyExc_NotImplementedError, "set_ignore_patterns not yet implemented");
    return NULL;
}

PyObject *pyttd_request_stop(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "request_stop not yet implemented");
    return NULL;
}
```

The remaining stub `.h`/`.c` files follow the same pattern. Each `.h` declares the functions; each `.c` defines them (without `static` for functions referenced across translation units). All Python-facing stubs raise `NotImplementedError`.

`ext/checkpoint.h`:
```c
#ifndef PYTTD_CHECKPOINT_H
#define PYTTD_CHECKPOINT_H
#include <Python.h>

PyObject *pyttd_create_checkpoint(PyObject *self, PyObject *Py_UNUSED(args));

#endif
```

`ext/checkpoint.c`:
```c
#include <Python.h>
#include "platform.h"
#include "checkpoint.h"

/* Phase 2: Fork-based checkpoint manager */

PyObject *pyttd_create_checkpoint(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "create_checkpoint not yet implemented");
    return NULL;
}
```

`ext/checkpoint_store.h` — Mostly internal (checkpoint array, eviction), but `pyttd_kill_all_checkpoints` is Python-facing (referenced in `PyttdMethods`):
```c
#ifndef PYTTD_CHECKPOINT_STORE_H
#define PYTTD_CHECKPOINT_STORE_H
#include <Python.h>
#include <stdint.h>

/* Internal functions (called by checkpoint.c and replay.c) */
void checkpoint_store_init(void);
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no);
int checkpoint_store_find_nearest(uint64_t target_seq);

/* Python-facing (referenced in PyttdMethods) */
PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args));

#endif
```

`ext/checkpoint_store.c`:
```c
#include <Python.h>
#include "platform.h"
#include "checkpoint_store.h"

/* Phase 2: Checkpoint index, lifecycle, eviction */

void checkpoint_store_init(void) { /* stub */ }
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no) { return 0; }
int checkpoint_store_find_nearest(uint64_t target_seq) { return -1; }

PyObject *pyttd_kill_all_checkpoints(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "kill_all_checkpoints not yet implemented");
    return NULL;
}
```

`ext/replay.h`:
```c
#ifndef PYTTD_REPLAY_H
#define PYTTD_REPLAY_H
#include <Python.h>

PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args);

#endif
```

`ext/replay.c`:
```c
#include <Python.h>
#include "platform.h"
#include "replay.h"

/* Phase 2: Checkpoint resume + fast-forward */

PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args) {
    PyErr_SetString(PyExc_NotImplementedError, "restore_checkpoint not yet implemented");
    return NULL;
}
```

`ext/iohook.h`:
```c
#ifndef PYTTD_IOHOOK_H
#define PYTTD_IOHOOK_H
#include <Python.h>

PyObject *pyttd_install_io_hooks(PyObject *self, PyObject *Py_UNUSED(args));
PyObject *pyttd_remove_io_hooks(PyObject *self, PyObject *Py_UNUSED(args));

#endif
```

`ext/iohook.c`:
```c
#include <Python.h>
#include "iohook.h"

/* Phase 4: Intercept time/random/file for deterministic replay */

PyObject *pyttd_install_io_hooks(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "install_io_hooks not yet implemented");
    return NULL;
}

PyObject *pyttd_remove_io_hooks(PyObject *self, PyObject *Py_UNUSED(args)) {
    PyErr_SetString(PyExc_NotImplementedError, "remove_io_hooks not yet implemented");
    return NULL;
}
```

`ext/ringbuf.h` — Purely internal (no Python-facing functions, not in `PyttdMethods`). Called only by `recorder.c`:
```c
#ifndef PYTTD_RINGBUF_H
#define PYTTD_RINGBUF_H
#include <stdint.h>
#include "frame_event.h"

int ringbuf_init(uint32_t capacity);
int ringbuf_push(const FrameEvent *event);
int ringbuf_pop_batch(FrameEvent *out, uint32_t max_count, uint32_t *actual_count);
void ringbuf_destroy(void);

#endif
```

`ext/ringbuf.c`:
```c
#include "ringbuf.h"

/* Phase 1: Lock-free SPSC ring buffer with string pool */

int ringbuf_init(uint32_t capacity) { return 0; }
int ringbuf_push(const FrameEvent *event) { return 0; }
int ringbuf_pop_batch(FrameEvent *out, uint32_t max_count, uint32_t *actual_count) { if (actual_count) *actual_count = 0; return 0; }
void ringbuf_destroy(void) { }
```

**Python package files:**

`pyttd/__init__.py`:
```python
__version__ = "0.1.0"
```

`pyttd/__main__.py`:
```python
from pyttd.cli import main
main()
```

`pyttd/performance/__init__.py` — **NEW** (currently missing, required for `from pyttd.performance import clock` to work):
```python
# empty — makes performance/ a proper package
```

`pyttd/cli.py` — **Stub** (full implementation in Phase 1, but needed now for `__main__.py` import):
```python
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(
        prog='pyttd',
        description='Python Time-Travel Debugger'
    )
    subparsers = parser.add_subparsers(dest='command')

    # Stubs — full implementations added in Phase 1 (record, query) and Phase 3 (serve)
    subparsers.add_parser('record', help='Record script execution')
    subparsers.add_parser('query', help='Query trace data')
    subparsers.add_parser('replay', help='Replay a recorded session')
    subparsers.add_parser('serve', help='Start JSON-RPC server (used by VSCode)')

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    print(f"pyttd {args.command}: not yet implemented (Phase 1+)")
```

`pyttd/config.py` — Configuration dataclass:
```python
from dataclasses import dataclass, field

@dataclass
class PyttdConfig:
    checkpoint_interval: int = 1000
    max_checkpoints: int = 32
    ring_buffer_size: int = 65536
    flush_interval_ms: int = 10
    max_repr_length: int = 256
    ignore_patterns: list[str] = field(default_factory=list)
    db_path: str | None = None  # None = auto (<script>.pyttd.db)
```

`pyttd/errors.py` — Custom exceptions:
```python
class PyttdError(Exception): ...
class RecordingError(PyttdError): ...
class ReplayError(PyttdError): ...
class CheckpointError(PyttdError): ...
class ServerError(PyttdError): ...
class NoForkError(PyttdError): ...
```

**Tests:**

`tests/__init__.py` — empty.

`tests/conftest.py` — shared fixtures:
```python
import pytest
from pyttd.models import storage
from pyttd.models.base import db
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs

@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary DB path for test isolation."""
    return str(tmp_path / "test.pyttd.db")

@pytest.fixture
def db_setup(db_path):
    """Connect to a temp DB, create tables, and close after test."""
    storage.connect_to_db(db_path)
    storage.initialize_schema([Runs, ExecutionFrames])
    yield db_path
    storage.close_db()
    # Reset the deferred database so next test can re-init
    db.init(None)
```

`tests/test_models.py` (all tests use the `db_setup` fixture for DB isolation):
- Test `Runs` creation with auto-generated `run_id` (unique UUIDs across multiple creates, not shared)
- Test `Runs` timestamp defaults (different values on separate calls — use `time.sleep(0.01)` between)
- Test `ExecutionFrames` creation with all required fields
- Test `ExecutionFrames` foreign key to `Runs`
- Test `batch_insert` inserts correct number of rows
- Test DB is created at the specified path with WAL mode enabled

`tests/test_native_stub.py`:
- Test `import pyttd_native` succeeds
- Test `dir(pyttd_native)` contains expected method names: `start_recording`, `stop_recording`, `get_recording_stats`, `set_ignore_patterns`, `request_stop`, `create_checkpoint`, `restore_checkpoint`, `kill_all_checkpoints`, `install_io_hooks`, `remove_io_hooks`
- Test each stub method raises `NotImplementedError` when called

**VSCode extension scaffolding — `vscode-pyttd/`:**

`package.json`:
```jsonc
{
  "name": "pyttd",
  "displayName": "pyttd - Python Time-Travel Debugger",
  "description": "Time-travel debugger for Python with record, replay, and visual timeline",
  "version": "0.1.0",
  "publisher": "pyttd",
  "engines": { "vscode": "^1.85.0" },
  "categories": ["Debuggers"],
  "activationEvents": ["onDebugResolve:pyttd"],
  "main": "./out/extension.js",
  "contributes": {
    "debuggers": [{
      "type": "pyttd",
      "label": "Python Time-Travel Debug",
      "languages": ["python"],
      "configurationAttributes": {
        "launch": {
          "required": [],
          "properties": {
            "program": {
              "type": "string",
              "description": "Path to Python script to debug"
            },
            "module": {
              "type": "string",
              "description": "Python module to debug (dotted name)"
            },
            "pythonPath": {
              "type": "string",
              "description": "Path to Python interpreter"
            },
            "cwd": {
              "type": "string",
              "description": "Working directory"
            },
            "args": {
              "type": "array",
              "items": { "type": "string" },
              "description": "Command line arguments"
            },
            "traceDb": {
              "type": "string",
              "description": "Path to trace database file"
            },
            "checkpointInterval": {
              "type": "number",
              "description": "Frames between checkpoints (default: 1000)"
            }
          }
        }
      },
      "configurationSnippets": [{
        "label": "pyttd: Launch",
        "description": "Time-travel debug a Python script",
        "body": {
          "type": "pyttd",
          "request": "launch",
          "name": "Time-Travel Debug",
          "program": "^\"${file}\""
        }
      }],
      "exceptionBreakpointFilters": [
        {
          "filter": "uncaught",
          "label": "Uncaught Exceptions",
          "default": true
        },
        {
          "filter": "raised",
          "label": "All Exceptions",
          "default": false
        }
      ]
    }]
  },
  "scripts": {
    "compile": "tsc -p ./",
    "watch": "tsc -watch -p ./"
  },
  "dependencies": {
    "@vscode/debugadapter": "^1.65.0",
    "@vscode/debugprotocol": "^1.65.0"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "@types/vscode": "^1.85.0",
    "typescript": "^5.3.0"
  }
}
```

`tsconfig.json`:
```json
{
  "compilerOptions": {
    "module": "commonjs",
    "target": "ES2022",
    "lib": ["ES2022"],
    "outDir": "out",
    "rootDir": "src",
    "sourceMap": true,
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "exclude": ["node_modules", "out"]
}
```

`.vscodeignore`:
```
node_modules/
src/
tsconfig.json
.vscode/
```

`vscode-pyttd/.vscode/tasks.json`:
```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "type": "npm",
      "script": "compile",
      "group": { "kind": "build", "isDefault": true },
      "label": "npm: compile",
      "problemMatcher": "$tsc"
    }
  ]
}
```

`vscode-pyttd/.vscode/launch.json`:
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Extension Development Host",
      "type": "extensionHost",
      "request": "launch",
      "args": ["--extensionDevelopmentPath=${workspaceFolder}"],
      "outFiles": ["${workspaceFolder}/out/**/*.js"],
      "preLaunchTask": "npm: compile"
    }
  ]
}
```

`src/extension.ts`:
```typescript
import * as vscode from 'vscode';
import { PyttdDebugSession } from './debugAdapter/pyttdDebugSession';

export function activate(context: vscode.ExtensionContext) {
    context.subscriptions.push(
        vscode.debug.registerDebugAdapterDescriptorFactory('pyttd', {
            createDebugAdapterDescriptor(_session: vscode.DebugSession): vscode.ProviderResult<vscode.DebugAdapterDescriptor> {
                return new vscode.DebugAdapterInlineImplementation(new PyttdDebugSession());
            }
        })
    );
    // Phase 5: register TimelineScrubberProvider
    // Phase 6: register CodeLensProvider, InlineValuesProvider, CallHistoryProvider
}

export function deactivate() {}
```

`src/debugAdapter/pyttdDebugSession.ts`:
```typescript
import {
    LoggingDebugSession,
    InitializedEvent,
    StoppedEvent,
    Thread,
    StackFrame,
    Scope,
    Source,
    Variable,
    TerminatedEvent,
} from '@vscode/debugadapter';
import { DebugProtocol } from '@vscode/debugprotocol';
import { BackendConnection } from './backendConnection';
import { PyttdLaunchConfig } from './types';

export class PyttdDebugSession extends LoggingDebugSession {
    private backend: BackendConnection = new BackendConnection();

    public constructor() {
        super();
        this.setDebuggerLinesStartAt1(true);
        this.setDebuggerColumnsStartAt1(true);
    }

    protected initializeRequest(response: DebugProtocol.InitializeResponse, args: DebugProtocol.InitializeRequestArguments): void {
        response.body = response.body || {};
        response.body.supportsConfigurationDoneRequest = true;
        response.body.supportsEvaluateForHovers = true;
        // Phase 3: supportsProgressReporting (when progress events are implemented)
        // Phase 4: supportsStepBack, supportsGotoTargetsRequest, supportsRestartFrame
        // Do NOT advertise supportsExceptionInfoRequest unless exceptionInfoRequest is implemented
        this.sendResponse(response);
    }

    protected launchRequest(response: DebugProtocol.LaunchResponse, args: DebugProtocol.LaunchRequestArguments): void {
        // Phase 3: spawn backend, connect TCP, send InitializedEvent
        this.sendResponse(response);
    }

    protected setBreakPointsRequest(response: DebugProtocol.SetBreakpointsResponse, args: DebugProtocol.SetBreakpointsArguments): void {
        // Phase 3: forward breakpoints to backend
        response.body = { breakpoints: [] };
        this.sendResponse(response);
    }

    protected configurationDoneRequest(response: DebugProtocol.ConfigurationDoneResponse, args: DebugProtocol.ConfigurationDoneArguments): void {
        // Phase 3: send configuration_done RPC to start recording
        this.sendResponse(response);
    }

    protected threadsRequest(response: DebugProtocol.ThreadsResponse): void {
        response.body = { threads: [new Thread(1, "Main Thread")] };
        this.sendResponse(response);
    }

    protected stackTraceRequest(response: DebugProtocol.StackTraceResponse, args: DebugProtocol.StackTraceArguments): void {
        // Phase 3: query backend for stack trace
        response.body = { stackFrames: [], totalFrames: 0 };
        this.sendResponse(response);
    }

    protected scopesRequest(response: DebugProtocol.ScopesResponse, args: DebugProtocol.ScopesArguments): void {
        // Phase 3: return Locals scope
        response.body = { scopes: [] };
        this.sendResponse(response);
    }

    protected variablesRequest(response: DebugProtocol.VariablesResponse, args: DebugProtocol.VariablesArguments): void {
        // Phase 3: query backend for variables
        response.body = { variables: [] };
        this.sendResponse(response);
    }

    protected evaluateRequest(response: DebugProtocol.EvaluateResponse, args: DebugProtocol.EvaluateArguments): void {
        // Phase 3: lookup variable in recorded state
        response.body = { result: '', variablesReference: 0 };
        this.sendResponse(response);
    }

    protected continueRequest(response: DebugProtocol.ContinueResponse, args: DebugProtocol.ContinueArguments): void {
        // Phase 3: forward continue to backend
        this.sendResponse(response);
    }

    protected nextRequest(response: DebugProtocol.NextResponse, args: DebugProtocol.NextArguments): void {
        // Phase 3: step over
        this.sendResponse(response);
    }

    protected stepInRequest(response: DebugProtocol.StepInResponse, args: DebugProtocol.StepInArguments): void {
        // Phase 3: step into
        this.sendResponse(response);
    }

    protected stepOutRequest(response: DebugProtocol.StepOutResponse, args: DebugProtocol.StepOutArguments): void {
        // Phase 3: step out
        this.sendResponse(response);
    }

    protected pauseRequest(response: DebugProtocol.PauseResponse, args: DebugProtocol.PauseArguments): void {
        // Phase 3: interrupt recording
        this.sendResponse(response);
    }

    protected disconnectRequest(response: DebugProtocol.DisconnectResponse, args: DebugProtocol.DisconnectArguments): void {
        // Phase 3: shutdown backend, cleanup
        this.backend.close();
        this.sendResponse(response);
    }

    // Phase 4: stepBackRequest, reverseContinueRequest, gotoTargetsRequest, gotoRequest, restartFrameRequest
}
```

`src/debugAdapter/backendConnection.ts`:
```typescript
import * as net from 'net';
import * as child_process from 'child_process';

export class BackendConnection {
    private process: child_process.ChildProcess | null = null;
    private socket: net.Socket | null = null;

    async spawn(pythonPath: string, args: string[]): Promise<number> {
        // Phase 3: spawn child process, read PYTTD_PORT:<port> from stdout
        throw new Error('Not yet implemented');
    }

    async connect(port: number): Promise<void> {
        // Phase 3: TCP connect, wrap in JSON-RPC connection
        throw new Error('Not yet implemented');
    }

    async sendRequest(method: string, params: any): Promise<any> {
        // Phase 3: send JSON-RPC request, await response
        throw new Error('Not yet implemented');
    }

    onNotification(callback: (method: string, params: any) => void): void {
        // Phase 3: register notification handler
    }

    close(): void {
        if (this.socket) { this.socket.destroy(); this.socket = null; }
        if (this.process) { this.process.kill(); this.process = null; }
    }
}
```

`src/debugAdapter/types.ts`:
```typescript
export interface JsonRpcRequest {
    jsonrpc: '2.0';
    id: number;
    method: string;
    params?: any;
}

export interface JsonRpcResponse {
    jsonrpc: '2.0';
    id: number;
    result?: any;
    error?: { code: number; message: string };
}

export interface JsonRpcNotification {
    jsonrpc: '2.0';
    method: string;
    params?: any;
}

export interface PyttdLaunchConfig {
    type: 'pyttd';
    request: 'launch';
    program?: string;
    module?: string;
    pythonPath?: string;
    cwd?: string;
    args?: string[];
    traceDb?: string;
    checkpointInterval?: number;
    rpcTimeout?: number;
}
```

### Modify

**`pyttd/models/base.py`** — Use Peewee's deferred database pattern:
```python
from peewee import Model, SqliteDatabase

# Deferred database — initialized later via db.init(path)
db = SqliteDatabase(None)

class _BaseModel(Model):
    class Meta:
        database = db
```

This replaces the current `database = db_conn.db` which binds at import time and breaks dynamic DB paths. The `db.init(path, pragmas=PRAGMAS)` call happens in `storage.py` when the DB path is known.

**`pyttd/models/storage.py`** — Dynamic DB path with deferred initialization:
```python
import logging
from typing import List, Type

from peewee import Model

from pyttd.models.base import db
from pyttd.models.constants import PRAGMAS

logger = logging.getLogger(__name__)

def connect_to_db(db_path: str):
    """Initialize the deferred database with the given path."""
    if not db.is_closed():
        db.close()
    db.init(db_path, pragmas=PRAGMAS)
    db.connect(reuse_if_open=True)
    logger.info("Connected to database: %s", db_path)

def initialize_schema(models: List[Type[Model]]):
    """Create tables for the given models (safe=True for idempotency)."""
    db.create_tables(models, safe=True)

def delete_db_files(db_path: str):
    """Delete a SQLite database and its WAL/SHM companion files."""
    import os
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)

def batch_insert(model_class: Type[Model], rows: list[dict], batch_size: int = 500):
    """Batch-insert rows into the given model's table."""
    with db.atomic():
        for i in range(0, len(rows), batch_size):
            model_class.insert_many(rows[i:i + batch_size]).execute()

def close_db():
    """Close the database connection."""
    if not db.is_closed():
        db.close()
```

Key changes from current code:
- Removed singleton `DBConnector` class — replaced with module-level functions operating on the deferred `db`
- Removed `rich` import (replaced by `logging`)
- Removed `PooledSqliteExtDatabase` (unnecessary for single-writer pattern)
- Added `batch_insert()` for the flush thread
- `db_path` is passed in at runtime, not hardcoded

**`pyttd/models/frames.py`:**
```python
from peewee import (AutoField, CharField, IntegerField, BigIntegerField, TextField,
                    ForeignKeyField, FloatField)
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs

class ExecutionFrames(_BaseModel):
    frame_id = AutoField()  # auto-increment integer PK (NOT UUIDField — see note below)
    run_id = ForeignKeyField(Runs, backref='frames', field='run_id')
    sequence_no = BigIntegerField()
    timestamp = FloatField()       # monotonic seconds since recording start (from C extension)
    line_no = IntegerField()
    filename = CharField()
    function_name = CharField()
    frame_event = CharField()      # 'call', 'line', 'return', 'exception', 'exception_unwind'
    call_depth = IntegerField()
    locals_snapshot = TextField(null=True)  # JSON string, NOT JSONField (avoids double-encoding)
    # Note: locals_snapshot is NULL for 'call' and 'exception_unwind' events (only
    # 'line', 'return', and 'exception' events capture locals via the trace function).
    # Navigation methods (step_over, step_into, step_back) always land on 'line' events,
    # so the Variables panel always has locals available during normal navigation.

    class Meta:
        indexes = (
            (('run_id', 'sequence_no'), True),     # unique, primary nav
            (('run_id', 'filename', 'line_no'), False),  # breakpoint matching
            (('run_id', 'function_name'), False),  # CodeLens stats
            (('run_id', 'frame_event', 'sequence_no'), False),  # reverse navigation on event type
            (('run_id', 'call_depth', 'sequence_no'), False),   # step_over, step_out queries
        )
```

Key changes from current code:
- **`frame_id` uses `AutoField` (auto-increment integer), NOT `UUIDField`** — Peewee's `insert_many()` does NOT apply Python-level `default=` callables (it generates raw SQL `INSERT` statements). With `UUIDField(default=uuid4)`, `insert_many` would produce rows with `NULL` `frame_id`, violating the primary key constraint. `AutoField` solves this because SQLite's `INTEGER PRIMARY KEY` auto-assigns rowid. Additionally, sequential integer keys give ~2-3x faster writes than random UUIDs due to B-tree page locality.
- `timestamp` is a plain `FloatField` (no default) — always provided by the C extension as monotonic seconds since recording start. No wall-clock default because all rows are inserted via `batch_insert` with C-provided timestamps
- Added `sequence_no`, `function_name`, `call_depth`
- Removed `globals_snapshot` (too large, not useful for repr-based debugging)
- Removed `line_byte_code` (not needed with C extension approach)
- Removed `line_code` (populated lazily via `linecache` during replay, not stored per-frame — saves ~50 bytes/frame)
- Removed `frame_args` (redundant with `locals_snapshot` which captures all locals including arguments)
- `locals_snapshot` uses `TextField` (NOT `JSONField`) — the C flush thread stores a raw JSON string, and `JSONField` would double-encode it. Callers read it with `json.loads(frame.locals_snapshot)`.
- Added composite indexes for query performance, including `(run_id, frame_event, sequence_no)` for efficient reverse navigation queries and `(run_id, call_depth, sequence_no)` for `step_over`/`step_out` depth-filtered queries
- Note: `checkpoint_id` field is NOT added here — it's added in Phase 2 when checkpoints exist

**`pyttd/models/runs.py`:**
```python
from uuid import uuid4
from datetime import datetime
from peewee import UUIDField, FloatField, CharField, IntegerField
from pyttd.models.base import _BaseModel

class Runs(_BaseModel):
    run_id = UUIDField(unique=True, primary_key=True, default=uuid4)
    timestamp_start = FloatField(default=lambda: datetime.now().timestamp())
    timestamp_end = FloatField(null=True)
    script_path = CharField(null=True)
    total_frames = IntegerField(default=0)
```

Key changes: callable defaults, added `script_path` and `total_frames` fields.

**`pyttd/models/constants.py`:**
```python
DB_NAME_SUFFIX = ".pyttd.db"     # <script_name>.pyttd.db
PRAGMAS = {
    'journal_mode': 'wal',
    'cache_size': -1024 * 64,
    'foreign_keys': 1,
    'synchronous': 1,            # Changed from 0: WAL + synchronous=NORMAL is safe
    'busy_timeout': 5000,        # 5s timeout for concurrent access (flush thread + queries)
}
```

Remove `DEBUG_DB_NAME` (hardcoded name), `EXECUTION_FRAME_TABLE`, `FRAME_PRIMARY_KEY` (Peewee handles table names).

**`pyttd/models/__init__.py`:**
- Keep existing exports (`_BaseModel`, `ExecutionFrames`, `Runs`), they remain valid after the field changes

**`samplecode/basic_sample_function.py`** — Remove broken imports and decorator that depend on the old `pyttd.main` API:
- Remove `from pyttd.main import ttdbg` import (line 58) and the `@ttdbg` decorator (line 60) — the script will be recorded via `pyttd record` CLI, not via the decorator
- Remove `from rich import print as rprint` and replace ALL `rprint(...)` calls with `print(...)` throughout the file (including inside `traceback_frames`, `n_frequent_words`, etc.) — `rich` is a dev dependency, not guaranteed available when recording via CLI
- Remove unused imports (`settrace`, `pprint`, `dis`, `linecache`, `StrEnum`, `Counter`) and the commented-out trace function block (lines 1-56). **Keep** `from sys import _getframe, modules` — both are used by `traceback_frames()` (which uses `_getframe(1)` and `modules[__name__]`). Also keep `from types import FrameType` (used by `traceback_frames` type hint) and `from typing import List` (used by `to_binary_tree` and `n_frequent_words`).
- In `traceback_frames()`: replace `modules[__name__]` with `__name__` (simplifies output, but keep the `sys.modules` import removal — `__name__` is a builtin, not from `modules`). After this change, `from sys import modules` can also be removed (only `_getframe` is still needed from `sys`).
- Remove `n_frequent_words_cheat` function and `from collections import Counter` (uses removed import)
- Uncomment `testfuncA(1)` (line 72) so the function actually executes when recorded
- Result: a clean script with test functions (`testfuncA`, `binarysearch`, `superdupertesting` chain, tree construction, `n_frequent_words`) that runs standalone for Phase 1 verification

### Verify

1. `.venv/bin/pip install -e ".[dev]"` compiles C stubs without errors
2. `.venv/bin/python -c "import pyttd_native; print(dir(pyttd_native))"` shows all stub method names
3. `.venv/bin/python -c "import pyttd; print(pyttd.__version__)"` prints `0.1.0`
4. `.venv/bin/python -m pyttd` prints help message (cli.py stub works)
5. `.venv/bin/pytest tests/test_models.py` — model creation, UUID uniqueness, timestamp callable defaults, batch insert
6. `.venv/bin/pytest tests/test_native_stub.py` — import succeeds, methods raise NotImplementedError
7. `cd vscode-pyttd && npm install && npm run compile` succeeds
8. F5 in Extension Dev Host shows "pyttd" in debug type dropdown
9. `.venv/bin/python samplecode/basic_sample_function.py` runs without import errors (cleaned-up sample file)

---

## Phase 1: C Extension Recorder + Ring Buffer — COMPLETE

**Goal:** Replace Python settrace with C-level frame eval hook. Ring buffer captures frames, background flush thread writes to SQLite via Peewee.

> **Status:** All steps completed. `recorder.c` (~500 lines) implements PEP 523 eval hook + C-level trace function. `ringbuf.c` implements lock-free SPSC ring buffer with double-buffered string pools. Python wrappers (`recorder.py`, `runner.py`, `query.py`) and full CLI (`record`, `query`) implemented. 26 tests passing. Recording verified on sample scripts (2000+ frames, 0 dropped, 0 pool overflows).
>
> **Implementation notes:**
> - Used `PyDict_Next()` fast path on 3.12 and `PyMapping_Items()` on 3.13+ (version-gated with `#if PY_VERSION_HEX < 0x030D0000`)
> - Flush thread closes its DB connection via `PyImport_ImportModule("pyttd.models.base")` → `db.close()` before exit
> - `stop_recording()` releases GIL with `Py_BEGIN_ALLOW_THREADS` while joining flush thread (flush thread needs GIL for final flush)
> - pyttd's own `runner.py`/`cli.py` frames are recorded alongside user code (not yet in ignore filter — cosmetic issue, addressed in Phase 3 server mode)

### Key C components

**`recorder.c` — PEP 523 Frame Eval Hook:**

Uses `_PyInterpreterState_SetEvalFrameFunc()` to install a custom frame evaluator. Version-specific API details:

```c
#include <Python.h>

/* _PyInterpreterFrame is forward-declared in cpython/pystate.h (included via Python.h).
 * Do NOT include <internal/pycore_frame.h> — it's an internal header that may not be
 * available in all installations. Use the public PyUnstable_InterpreterFrame_* accessors
 * instead of direct struct field access.
 */

/* Get the interpreter state */
PyInterpreterState *interp = PyInterpreterState_Get();

/*
 * On 3.12-3.14: _PyInterpreterState_SetEvalFrameFunc(interp, our_eval)
 * On 3.15+: PyUnstable_InterpreterState_SetEvalFrameFunc(interp, our_eval)
 *
 * The frame eval function signature:
 *   PyObject *our_eval(PyThreadState *tstate, _PyInterpreterFrame *frame, int throwflag)
 *
 * Inside the eval hook, use these accessors to read frame data:
 *   PyUnstable_InterpreterFrame_GetCode(frame)  -> PyCodeObject*
 *   PyUnstable_InterpreterFrame_GetLine(frame)   -> int (line number, -1 if unknown)
 *   PyUnstable_InterpreterFrame_GetLasti(frame)  -> int (last bytecode index)
 */

#if PY_VERSION_HEX >= 0x030F0000  /* 3.15+ (PyUnstable_ promoted in 3.15, NOT 3.14) */
  #define PYTTD_SET_EVAL_FUNC PyUnstable_InterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC PyUnstable_InterpreterState_GetEvalFrameFunc
#else  /* 3.12-3.14 */
  #define PYTTD_SET_EVAL_FUNC _PyInterpreterState_SetEvalFrameFunc
  #define PYTTD_GET_EVAL_FUNC _PyInterpreterState_GetEvalFrameFunc
#endif
```

**Two-mechanism approach for per-line recording:** PEP 523's frame eval hook fires **once per frame entry** (when a code object starts execution), NOT per-line. To capture `call`, `line`, `return`, and `exception` events at line granularity, pyttd uses a combined approach:

1. **PEP 523 eval hook** — intercepts every new frame. Used for filtering (ignore patterns, thread check), recording the `call` event, and incrementing `call_depth`.
2. **C-level trace function** — installed on each frame by the eval hook via `PyEval_SetTrace()`. The trace function signature is `int trace_func(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg)` where `what` is `PyTrace_CALL`, `PyTrace_LINE`, `PyTrace_RETURN`, or `PyTrace_EXCEPTION`. This captures per-line events within each frame.

**`call_depth` tracking:** The C extension owns a static `int call_depth` counter in `recorder.c`. The **eval hook** owns both increment and decrement: it increments `call_depth` before calling the original eval function (step 5) and decrements it after the original eval returns (step 9), regardless of whether the frame returned normally or via exception. The trace function does NOT modify `call_depth`. Each `FrameEvent` is stamped with the current `call_depth`. This counter is reset to **-1** in `start_recording()`, so the first non-ignored frame increments it to 0, making the top-level user frame's `call_depth == 0`. This ensures `step_out` at depth 0 correctly detects the outermost frame, and the `uncaught` exception breakpoint filter (`call_depth == 0`) correctly matches exceptions propagating out of the top-level frame.

**Why the eval hook owns `call_depth`, not the trace function:** While CPython 3.12+ does fire `PyTrace_RETURN` with `arg=NULL` for frames that exit via unhandled exception propagation (via the PY_UNWIND → legacy trace adapter translation), this path has had bugs (cpython#110892) and is not guaranteed to be reliable across all Python versions. By decrementing in the eval hook (which unconditionally regains control after the original eval returns, whether normally or via exception), `call_depth` stays correct regardless of edge-case trace adapter behavior. The eval hook provides a single, deterministic point of increment/decrement.

The frame eval hook flow:
1. Extracts `PyCodeObject*` from the internal frame via `PyUnstable_InterpreterFrame_GetCode(frame)`
2. Checks `g_stop_requested` atomic flag — if set, raises `KeyboardInterrupt` via `PyErr_SetNone(PyExc_KeyboardInterrupt)` and returns `NULL`
3. Checks ignore filter (see Ignore Filter section below) — if ignored, saves the current C-level trace function via thread state, temporarily removes it (`PyEval_SetTrace(NULL, NULL)`), calls the saved original eval function, then restores the previous trace function. This prevents ignored frames from producing `line`/`return`/`exception` events via an inherited parent trace. Returns the eval result directly.
4. Checks `PyThread_get_thread_ident()` — Phase 1 only records the main thread; other threads pass through to the saved original eval function
5. Increments `call_depth`, then writes a `call` FrameEvent into the ring buffer (filename, function_name, line_no from code object, current call_depth)
6. Saves the current C-level trace function (`tstate->c_tracefunc` and `tstate->c_traceobj`), then installs `pyttd_trace_func` as the C-level trace function via `PyEval_SetTrace(pyttd_trace_func, trace_state)` (this makes CPython call our trace function for subsequent line/return/exception events within this frame)
7. Calls the **saved original eval function** (NOT hardcoded `_PyEval_EvalFrameDefault`) via the function pointer saved in `start_recording()`. This ensures correct chaining if another PEP 523 hook is already installed.
8. If the original eval returned `NULL` with `PyErr_Occurred()` (exception propagation), writes an `exception_unwind` FrameEvent to the ring buffer **before decrementing `call_depth`** — so `exception_unwind` is stamped with the same `call_depth` as the `call`/`line`/`exception` events in this frame. This is essential for `step_out` queries that match on `call_depth`.
9. Decrements `call_depth` (always — regardless of normal return or exception propagation)
10. Restores the previous trace function (saved in step 6)

**Why step 3 must remove the trace function for ignored frames:** `PyEval_SetTrace` is thread-wide, not per-frame. If a parent frame installed our trace function, it remains active for all descendant frames — including ignored ones. Without explicitly removing it, ignored frames would still produce `line`/`return`/`exception` events. By saving/removing/restoring the trace around the eval call for ignored frames, we guarantee zero recording overhead for them.

The C-level trace function (`pyttd_trace_func`) is called by CPython's eval loop for each event:
- `PyTrace_CALL`: **Skipped** — return 0 immediately. The eval hook already recorded the `call` event (step 5). Without this skip, every frame entry would be double-recorded.
- `PyTrace_LINE`: Extracts line number via `PyFrame_GetLineNumber(frame)`, serializes locals via `PyFrame_GetLocals()` + `PyObject_Repr()` (capped at 256 bytes per value), writes a `line` FrameEvent to the ring buffer
- `PyTrace_RETURN`: If `arg == NULL`, **skip** (return 0 immediately) — this is an exception-propagation exit, handled exclusively by the eval hook as `exception_unwind` (step 8). Recording both would produce duplicate events. If `arg != NULL`, serializes the frame's locals via `PyFrame_GetLocals()` (same as `PyTrace_LINE`) AND appends a `"__return__"` key with `repr(arg)` (the return value). The combined JSON is stored in `locals_snapshot`. This allows the Variables panel to display both the final local variable state and the return value when navigating to a return event. Does NOT decrement `call_depth` — the eval hook owns that (step 9). Note: on CPython 3.12+, `PyTrace_RETURN` IS fired for both normal returns and exception-propagating exits (via the PY_UNWIND → legacy trace adapter), with `arg=NULL` distinguishing the latter.
- `PyTrace_EXCEPTION`: Writes an `exception` FrameEvent with exception info. The `arg` is `(type, value, traceback)` — serialize `repr(value)` into `locals_json` alongside any captured locals. This records the exception at the point it was raised. Note: this is distinct from the `exception_unwind` event recorded by the eval hook (step 8), which records that the frame *exited* via exception.
- Returns 0 to continue tracing, or -1 to stop tracing this frame (used for error recovery)

**Memory management in locals serialization:** `PyFrame_GetLocals()` returns a **new reference** (a new `dict` on 3.12, a `FrameLocalsProxy` on 3.13+ per PEP 667). The caller MUST `Py_DECREF` it after iteration to avoid memory leaks. Similarly, each `PyObject_Repr()` call returns a new reference.

**Critical:** On Python 3.13+, `PyFrame_GetLocals()` returns a `FrameLocalsProxy`, NOT a `dict`. `PyDict_Next()` does NOT work on `FrameLocalsProxy` — it is not a dict subclass, and calling `PyDict_Next()` on it causes undefined behavior. Use `PyMapping_Items()` which works on both `dict` (3.12) and `FrameLocalsProxy` (3.13+):
```c
PyObject *locals = PyFrame_GetLocals(frame);
if (locals) {
    /* PyMapping_Items works on both dict and FrameLocalsProxy */
    PyObject *items = PyMapping_Items(locals);
    if (items) {
        Py_ssize_t n = PyList_GET_SIZE(items);
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *pair = PyList_GET_ITEM(items, i);
            PyObject *key = PyTuple_GET_ITEM(pair, 0);
            PyObject *val = PyTuple_GET_ITEM(pair, 1);
            /* serialize key name + PyObject_Repr(val) */
        }
        Py_DECREF(items);
    }
    Py_DECREF(locals);
}
```
On 3.13+, `PyFrameLocalsProxy_Check(obj)` can be used to detect the proxy type if version-specific fast paths are desired. **Performance optimization (recommended):** On Python 3.12 (where `PyFrame_GetLocals()` returns a `dict`), use `PyDict_Next()` which is O(n) with zero allocations. On 3.13+ (returns `FrameLocalsProxy`), use `PyMapping_Items()` which allocates a new `list` of `tuple` objects per call. For a function with 20 locals, that is 21 heap allocations per `PyTrace_LINE` event. Gate with `#if PY_VERSION_HEX < 0x030D0000` for the `PyDict_Next` fast path. `PyFrame_GetLineNumber()` and `PyFrame_GetCode()` work on `PyFrameObject*` (the public frame type used by the trace function), NOT on `_PyInterpreterFrame*` (the internal type used by the eval hook).

**Important:** The hook must save and restore the original eval function pointer to chain correctly. On `start_recording()`, save the current eval function pointer via `PYTTD_GET_EVAL_FUNC(interp)` (see version-gated macros above — `_PyInterpreterState_GetEvalFrameFunc` on 3.12-3.14, `PyUnstable_InterpreterState_GetEvalFrameFunc` on 3.15+). On `stop_recording()`, restore it via `PYTTD_SET_EVAL_FUNC`. Similarly, save and restore the previous trace function around `PyEval_SetTrace` calls — access `tstate->c_tracefunc` and `tstate->c_traceobj` directly. **Portability note:** these fields are in `Include/cpython/pystate.h` and are not part of the stable/limited API. CPython is working toward making `PyThreadState` opaque (cpython#84128), so direct field access may break in future versions. There is no public API to read the current C-level trace function (`sys.gettrace()` returns the Python-level trace object, not the C `c_tracefunc` pointer). Version-gate the struct access with `#if PY_VERSION_HEX` guards and test against each supported minor version. The saved eval function pointer is the one called in step 7 — never hardcode `_PyEval_EvalFrameDefault`.

**Version detection robustness:** The `PYTTD_SET_EVAL_FUNC` / `PYTTD_GET_EVAL_FUNC` macros gate on `PY_VERSION_HEX >= 0x030F0000`. If `PyUnstable_InterpreterState_SetEvalFrameFunc` is backported to 3.14.x or the `_Py` names are removed earlier than expected, add a compile-time `#ifdef` fallback:
```c
#ifdef PyUnstable_InterpreterState_SetEvalFrameFunc
  #define PYTTD_SET_EVAL_FUNC PyUnstable_InterpreterState_SetEvalFrameFunc
#else
  #define PYTTD_SET_EVAL_FUNC _PyInterpreterState_SetEvalFrameFunc
#endif
```

Runtime version check: `PyInit_pyttd_native()` should verify `PY_VERSION_HEX >= 0x030C0000` and raise `ImportError("pyttd requires Python 3.12 or later")` if not met.

**Sequence number tracking:** `recorder.c` maintains a `static uint64_t g_sequence_counter` initialized to 0 in `start_recording()`. Every frame event (call, line, return, exception, exception_unwind) increments this counter. The counter value is stamped on each `FrameEvent.sequence_no` before writing to the ring buffer. This provides a globally ordered sequence across all event types. The eval hook increments for `call` and `exception_unwind` events; the trace function increments for `line`, `return`, and `exception` events.

**`ringbuf.c`:** Lock-free SPSC ring buffer with these specifics:
- Atomic head (write position, modified by producer) and tail (read position, modified by consumer)
- Power-of-2 capacity, default 65536 entries — use `capacity - 1` as mask for index wrapping. `ringbuf_init()` must validate that `capacity` is a power of 2 (assert `(capacity & (capacity - 1)) == 0`); if the user passes a non-power-of-2 value via config, round up to the next power of 2
- Array of `FrameEvent` structs
- **String pool size:** Each pool arena defaults to 8MB (`PYTTD_STRING_POOL_SIZE`). This accommodates ~65K entries with an average of ~120 bytes of string data per entry (filename + function_name + locals_json). Configurable via a `#define` in `ringbuf.c`. If the pool is too small, the overflow handling (below) gracefully degrades by recording frames with `locals_json = NULL`
- **Double-buffered string pool**: Two string pool arenas, alternating between producer and consumer. The producer writes into pool A while the consumer reads from pool B (copying strings into Python `str` objects via `PyUnicode_FromString`), then they swap. This prevents use-after-free: the producer never writes into a pool the consumer is reading, and the consumer never reads from a pool the producer is writing into. Each pool is reset only after the swap (when the consumer is done with it). The swap is coordinated by the flush batch boundary — after `ringbuf_pop_batch()`, the consumer finishes all reads from the current pool, then the pools swap atomically (a single pointer swap under the tail update's release barrier)
- Atomics: prefer C11 `<stdatomic.h>` (`atomic_load_explicit` / `atomic_store_explicit` with `memory_order_acquire` / `memory_order_release`) which is portable across GCC, Clang, and MSVC 2022+ (all compilers that support Python 3.12+). Fallback: `__atomic_load_n` / `__atomic_store_n` on GCC/Clang. Define `PYTTD_ATOMIC_LOAD` / `PYTTD_ATOMIC_STORE` macros that select the appropriate implementation

**String lifetime:** `FrameEvent.filename`, `function_name`, and `locals_json` are `const char *` pointers. These point to data that may become invalid before the flush thread reads them (e.g., code objects could be garbage collected between write and flush). **All string data MUST be copied into the string pool** by the producer (eval hook), not stored as raw pointers to Python-owned memory. The string pool is reset after each flush batch. The `event_type` field is an exception — it points to C string literals ("call", "line", etc.) which are always valid.

**Overflow handling:**
- **String pool overflow:** If the string pool fills before the next flush, subsequent frames are recorded with `locals_json = NULL` (metadata is still captured). The flush thread resets the pool after each batch.
- **Ring buffer full:** If the producer (frame eval hook) catches up to the consumer (flush thread), the frame is **dropped** — a `dropped_frames` counter is incremented. The flush thread reports dropped frame count via a Python callback after each batch. This is preferred over blocking the user's script.

**Flush thread lifecycle:**
- **Created** when `start_recording()` is called from Python
- **Runs** as a C `pthread` (POSIX) or `_beginthreadex` (Windows), wakes every 10ms or when buffer reaches 75% capacity (signaled by condition variable from the frame eval hook)
- **GIL interaction:** The flush thread reads `FrameEvent` structs from the ring buffer without the GIL. It then calls `PyGILState_Ensure()`, converts the batch into a Python `list[dict]` (creating Python objects requires the GIL), calls the `flush_callback` with the list, then calls `PyGILState_Release()`. The GIL is held during both dict creation and the Python callback, not during ring buffer reads. On interpreter shutdown, `PyGILState_Ensure` can fail — `stop_recording()` must be called before interpreter finalization. As a safety net, register an `atexit` handler in `PyInit_pyttd_native()` that sets an atomic `g_interpreter_alive = 0` flag; the flush thread checks this flag before calling `PyGILState_Ensure` and exits cleanly if the interpreter is shutting down.
- **Dict key mapping:** Each `FrameEvent` struct is converted to a Python dict with keys matching `ExecutionFrames` model field names: `{"sequence_no": uint64, "timestamp": double, "line_no": int, "filename": str, "function_name": str, "frame_event": str (one of "call", "line", "return", "exception", "exception_unwind"), "call_depth": int, "locals_snapshot": str|None}`. The `run_id` key is NOT set by the C thread — it is stamped by `recorder.py._on_flush()` before `batch_insert`. The `frame_id` is NOT set here — `ExecutionFrames.frame_id` uses `AutoField()` (auto-increment integer), so SQLite auto-assigns it during `INSERT`. This is critical: `insert_many()` generates raw SQL and does NOT apply Python-level `default=` callables, so a `UUIDField(default=uuid4)` would produce `NULL` primary keys. `AutoField` avoids this entirely.
- **Thread-local DB connections:** Peewee uses thread-local connection storage by default (`thread_safe=True`). The flush thread's first DB operation triggers an automatic connection via `autoconnect=True`. This means the flush thread gets its own SQLite connection with its own `PRAGMA` settings — no `check_same_thread` issues. The pragmas (including WAL mode, `busy_timeout`) are applied per-connection by Peewee.
- **Callback reference:** The Python `flush_callback` is stored as a `PyObject*` with `Py_INCREF` to prevent garbage collection. `Py_DECREF` on `stop_recording()`.
- **Error handling:** After calling the flush callback, the flush thread must check `PyErr_Occurred()`. If the callback raised an exception (e.g., disk full, DB locked), log the error via `PyErr_WriteUnraisable(flush_callback)`, clear the exception, and continue — do NOT propagate the exception or stop the flush thread. The dropped batch is lost but recording continues. The `dropped_frames` counter should be incremented by the batch size so stats reflect the loss.
- **Paused** before fork (Phase 2): set atomic flag, wait for acknowledgment via condition variable
- **Resumed** after fork in parent process
- **Stopped** when `stop_recording()` is called: set stop flag, signal condition variable, join thread, flush remaining buffer entries. Before the flush thread exits, it must close its thread-local DB connection. Mechanism: the flush thread acquires the GIL via `PyGILState_Ensure()`, imports `pyttd.models.base` via `PyImport_ImportModule`, gets the `db` attribute via `PyObject_GetAttrString`, and calls `PyObject_CallMethod(db, "close", NULL)`. This closes the thread-local Peewee connection (Peewee creates separate connections per thread via `autoconnect=True`; without explicit close, the connection leaks because C pthreads don't trigger Python thread-local cleanup)

**`get_recording_stats()` return dict:** Returns a Python dict with cumulative recording statistics: `{"frame_count": uint64 (total frames recorded), "dropped_frames": uint64 (frames dropped due to ring buffer overflow or failed flush), "elapsed_time": double (seconds since start_recording), "flush_count": uint64 (number of flush batches completed), "pool_overflows": uint64 (frames where locals_json was set to NULL due to string pool overflow)}`. These counters are maintained as C statics in `recorder.c`, reset in `start_recording()`, and read atomically (safe from any thread since the recording thread is the sole writer during recording, and `get_recording_stats` is called after `stop_recording` which joins the flush thread).

**Locals serialization pipeline:** For each variable captured via `PyFrame_GetLocals()`:
- Primitives (`int`, `float`, `str`, `bool`, `None`): extracted directly via C API type checks for faithful representation
- Complex types: `PyObject_Repr()`, truncated to 256 bytes
- The eval hook builds a JSON string in the string pool: `{"var_name": "repr_string", ...}` (C string manipulation, no Python objects)
- The flush thread reads this JSON string from the ring buffer and creates a Python `str` via `PyUnicode_FromString(event->locals_json)`
- The resulting Python string is stored as `locals_snapshot` in the insertion dict
- `ExecutionFrames.locals_snapshot` is a `TextField` (not `JSONField`) to avoid double-encoding — the raw JSON string is stored as-is
- On read, callers use `json.loads(frame.locals_snapshot)` to parse the JSON back into a Python dict
- **JSON escaping:** The C JSON builder must properly escape special characters in both variable names and repr strings: double quotes (`"` → `\"`), backslashes (`\` → `\\`), control characters (`\n` → `\\n`, `\t` → `\\t`, etc.), and any byte < 0x20 (as `\uXXXX`). Failure to escape produces invalid JSON that `json.loads()` will reject. Repr strings frequently contain these characters (e.g., `"hello\nworld"`, paths with backslashes on Windows). Implement a `json_escape_string(const char *src, char *dst, size_t dst_size)` helper used by both key and value serialization.

**Ignore filter:** Receives ignore patterns from Python at init time via `set_ignore_patterns()`. The existing patterns in `tracing/constants.py` include directory substrings (`"lib/python"`, `"site-packages"`), filenames (`"_weakref.py"`), and function names (`"_shutdown"`). The Python side passes these as a flat list. The C implementation classifies patterns heuristically and uses two complementary strategies:
- **Substring scan** for patterns containing `"/"` (directory patterns like `"lib/python"`, `"site-packages"`) — checks if `co_filename` contains the pattern. Since there are only a few directory patterns (typically 2-3), this is effectively O(1) with a small constant.
- **Exact match hash set** for all other patterns (filenames like `"_weakref.py"` and function names like `"_shutdown"`) — checks both `co_filename`'s basename and `co_name` against the hash set. O(1) lookup.

The eval hook extracts `co_filename` and `co_name` from the `PyCodeObject*` and runs both checks using C string operations — no Python object creation on the hot path.

**Multi-thread note:** Phase 1 uses SPSC (single-threaded recording). The PEP 523 frame eval hook fires for all threads, but Phase 1 only records the main thread (checks `PyThread_get_thread_ident()`). Phase 7 upgrades to MPSC for multi-thread support.

### Python wrapper (`pyttd/recorder.py`)

```python
from datetime import datetime
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.tracing.constants import IGNORE_PATTERNS as INTERNAL_IGNORE

class Recorder:
    def __init__(self, config: PyttdConfig):
        self.config = config
        self._recording = False
        self._run = None

    def start(self, db_path: str, script_path: str | None = None):
        """Initialize DB, create Runs record, set ignore patterns, install frame eval hook."""
        storage.connect_to_db(db_path)
        storage.initialize_schema([Runs, ExecutionFrames])
        self._run = Runs.create(script_path=script_path)
        # Merge built-in ignore patterns (stdlib, site-packages) with user-provided patterns
        all_ignore = list(INTERNAL_IGNORE) + list(self.config.ignore_patterns)
        pyttd_native.set_ignore_patterns(all_ignore)
        pyttd_native.start_recording(
            flush_callback=self._on_flush,
            buffer_size=self.config.ring_buffer_size,
            flush_interval_ms=self.config.flush_interval_ms,
        )
        self._recording = True

    def stop(self) -> dict:
        """Stop recording, flush remaining, update Runs record, return stats.
        Does NOT close the DB — it's needed for replay mode after recording.
        Call cleanup() during session shutdown to close the DB."""
        pyttd_native.stop_recording()
        self._recording = False
        stats = pyttd_native.get_recording_stats()
        if self._run:
            self._run.timestamp_end = datetime.now().timestamp()
            self._run.total_frames = stats.get('frame_count', 0)
            self._run.save()
        return stats

    def cleanup(self):
        """Close DB connection. Called during session shutdown (disconnect),
        NOT after recording stops (DB is needed for replay)."""
        storage.close_db()

    @property
    def run_id(self):
        """Return the current run_id (needed by server for session)."""
        return self._run.run_id if self._run else None

    def _on_flush(self, events: list[dict]):
        """Called by C flush thread (with GIL held) to batch-insert frames.
        The Python wrapper stamps run_id on each event before insertion.
        May raise — the C flush thread checks PyErr_Occurred() after this call
        and logs the exception via PyErr_WriteUnraisable (which clears it),
        then continues recording. The batch is lost but recording is not stopped."""
        for event in events:
            event['run_id'] = self._run.run_id
        try:
            storage.batch_insert(ExecutionFrames, events)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("batch_insert failed")
            raise  # C layer will log via PyErr_WriteUnraisable and continue
```

### User script execution (`pyttd/runner.py`)

```python
import runpy
import sys
import os

class Runner:
    def run_script(self, script_path: str, cwd: str, args: list[str] | None = None):
        """Execute user script via runpy.run_path.
        Adds the script's directory to sys.path[0] to match 'python script.py' behavior
        (runpy.run_path does NOT do this automatically)."""
        old_cwd = os.getcwd()
        old_argv = sys.argv[:]
        old_path0 = sys.path[0] if sys.path else None
        os.chdir(cwd)
        sys.argv = [script_path] + (args or [])
        # Match CPython's behavior: sys.path[0] = directory of the script
        script_dir = os.path.dirname(os.path.abspath(script_path))
        if sys.path and sys.path[0] != script_dir:
            sys.path[0] = script_dir
        elif not sys.path:
            sys.path.insert(0, script_dir)
        try:
            runpy.run_path(script_path, run_name='__main__')
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)
            if old_path0 is not None:
                sys.path[0] = old_path0

    def run_module(self, module_name: str, cwd: str, args: list[str] | None = None):
        """Execute user module via runpy.run_module."""
        old_cwd = os.getcwd()
        old_argv = sys.argv[:]
        os.chdir(cwd)
        sys.argv = [module_name] + (args or [])
        try:
            runpy.run_module(module_name, run_name='__main__', alter_sys=True)
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)
```

**Limitation:** `run_path` does not support relative imports (`from . import foo`). Users debugging package modules should use `run_module` mode (specify module name instead of file path in launch config). Support both `program` (file path, uses `run_path`) and `module` (dotted name, uses `run_module`) in the launch config.

### CLI (`pyttd/cli.py`) — Full implementation

Replace the Phase 0 stub with full `record` and `query` implementations:

```python
import argparse
import os
import sys

def main():
    parser = argparse.ArgumentParser(prog='pyttd', description='Python Time-Travel Debugger')
    subparsers = parser.add_subparsers(dest='command')

    # pyttd record <script> [--module] [--checkpoint-interval N] [--args ...]
    record_parser = subparsers.add_parser('record', help='Record script execution')
    record_parser.add_argument('script', help='Script path or module name (with --module)')
    record_parser.add_argument('--module', action='store_true', help='Treat script as module name')
    record_parser.add_argument('--checkpoint-interval', type=int, default=1000)
    record_parser.add_argument('--args', nargs='*', default=[])

    # pyttd query --last-run [--frames] [--limit N] [--db PATH]
    query_parser = subparsers.add_parser('query', help='Query trace data')
    query_parser.add_argument('--last-run', action='store_true')
    query_parser.add_argument('--frames', action='store_true')
    query_parser.add_argument('--limit', type=int, default=50)
    query_parser.add_argument('--db', type=str, default=None)

    # pyttd replay --last-run --goto-frame N [--db PATH]  (Phase 2)
    replay_parser = subparsers.add_parser('replay', help='Replay a recorded session')
    replay_parser.add_argument('--last-run', action='store_true')
    replay_parser.add_argument('--goto-frame', type=int, default=0)
    replay_parser.add_argument('--db', type=str, default=None)

    # pyttd serve --script <path> --cwd <dir>  (Phase 3)
    serve_parser = subparsers.add_parser('serve', help='Start JSON-RPC debug server')
    serve_parser.add_argument('--script', required=True)
    serve_parser.add_argument('--module', action='store_true')
    serve_parser.add_argument('--cwd', default='.')
    serve_parser.add_argument('--checkpoint-interval', type=int, default=1000)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == 'record':
        _cmd_record(args)
    elif args.command == 'query':
        _cmd_query(args)
    elif args.command == 'replay':
        print("pyttd replay: not yet implemented (Phase 2)")
    elif args.command == 'serve':
        print("pyttd serve: not yet implemented (Phase 3)")

def _cmd_record(args):
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models.storage import delete_db_files

    if args.module:
        # For module mode, derive DB name from module name, place in cwd
        script_name = args.script.replace('.', '_')
        script_dir = os.getcwd()
        script_abs = args.script  # module name, not a path
    else:
        script_abs = os.path.abspath(args.script)
        script_name = os.path.splitext(os.path.basename(script_abs))[0]
        script_dir = os.path.dirname(script_abs) or '.'
    db_path = os.path.join(script_dir, script_name + DB_NAME_SUFFIX)
    cwd = script_dir

    config = PyttdConfig(checkpoint_interval=args.checkpoint_interval)
    recorder = Recorder(config)
    runner = Runner()

    # Delete existing DB + WAL/SHM files so create_tables builds fresh schema
    delete_db_files(db_path)
    recorder.start(db_path, script_path=script_abs)
    script_error = None
    try:
        if args.module:
            runner.run_module(args.script, cwd, args.args)
        else:
            runner.run_script(args.script, cwd, args.args)
    except BaseException as e:
        # Catch all exceptions including SystemExit and KeyboardInterrupt
        # so we can print stats before exiting. The recording is still valid.
        script_error = e
    finally:
        stats = recorder.stop()
        recorder.cleanup()  # CLI mode: close DB after recording (no replay session)
    if script_error:
        print(f"Script exited with {type(script_error).__name__}: {script_error}")
    print(f"Recording complete: {stats}")

def _cmd_query(args):
    from pyttd.query import get_last_run, get_frames, get_line_code
    from pyttd.models.constants import DB_NAME_SUFFIX
    import glob as globmod

    db_path = args.db
    if not db_path:
        # Find the most recently modified .pyttd.db in current directory
        dbs = sorted(globmod.glob(f"*{DB_NAME_SUFFIX}"), key=os.path.getmtime, reverse=True)
        if not dbs:
            print("No .pyttd.db files found in current directory. Use --db to specify path.")
            sys.exit(1)
        db_path = dbs[0]

    run = get_last_run(db_path)
    print(f"Run: {run.run_id} ({run.script_path or 'unknown'}) — {run.total_frames} frames")

    if args.frames:
        frames = get_frames(run.run_id, limit=args.limit)
        for f in frames:
            source = get_line_code(f.filename, f.line_no)
            print(f"  #{f.sequence_no:>6} {f.frame_event:<10} {f.function_name}:{f.line_no}  {source}")
```

### Other changes

- **Delete** `pyttd/tracing/trace_func.py` and `pyttd/tracing/trace.py` — replaced by C extension recorder
- **Update** `pyttd/tracing/enums.py` — add `EXCEPTION_UNWIND = 'exception_unwind'` to `EventENUM`, remove `OPCODE = 'opcode'` (not used by C extension). The enum is a reference for Python-side code; the C extension uses string literals directly.
- **Evolve** `pyttd/main.py`:
  ```python
  import functools
  import os
  from pyttd.config import PyttdConfig
  from pyttd.recorder import Recorder
  from pyttd.models.constants import DB_NAME_SUFFIX
  from pyttd.models.storage import delete_db_files

  def ttdbg(func):
      """Decorator that records function execution with the C extension."""
      @functools.wraps(func)
      def wrapper(*args, **kwargs):
          import inspect
          source_file = inspect.getfile(func)
          script_name = os.path.splitext(os.path.basename(source_file))[0]
          db_path = os.path.join(os.path.dirname(source_file) or '.', script_name + DB_NAME_SUFFIX)
          delete_db_files(db_path)  # Must delete WAL/SHM files too — orphaned WAL corrupts new DB
          config = PyttdConfig()
          recorder = Recorder(config)
          recorder.start(db_path, script_path=source_file)
          try:
              return func(*args, **kwargs)
          finally:
              recorder.stop()
              recorder.cleanup()
      return wrapper
  ```
- **Update** `pyttd/tracing/__init__.py` — remove imports of deleted trace modules (currently empty, so no change needed)
- **Create** `pyttd/query.py` — query functions for reading back trace data from the DB:
  ```python
  from pyttd.models.frames import ExecutionFrames
  from pyttd.models.runs import Runs
  from pyttd.models import storage

  def get_last_run(db_path: str) -> Runs:
      storage.connect_to_db(db_path)
      return Runs.select().order_by(Runs.timestamp_start.desc()).get()

  def get_frames(run_id, limit=50, offset=0) -> list[ExecutionFrames]:
      return list(ExecutionFrames.select()
          .where(ExecutionFrames.run_id == run_id)
          .order_by(ExecutionFrames.sequence_no)
          .offset(offset).limit(limit))

  def get_frame_at_seq(run_id, seq) -> ExecutionFrames:
      return ExecutionFrames.get(
          (ExecutionFrames.run_id == run_id) &
          (ExecutionFrames.sequence_no == seq))

  def get_line_code(filename: str, line_no: int) -> str:
      """Lazily fetch source line via linecache (not stored in DB)."""
      import linecache
      return linecache.getline(filename, line_no).strip()
  ```

### Create

**Evolve** (replace Phase 0 stubs with full implementations):
- `ext/recorder.c/h` — full implementation (PEP 523 hook, frame event serialization, version-gated APIs)
- `ext/ringbuf.c/h` — full implementation (SPSC ring buffer, string pool, atomic operations)
- `ext/frame_event.h` — FrameEvent struct (may need additional fields vs Phase 0 stub)
- `pyttd/cli.py` — full record and query subcommands (serve and replay subcommands print stubs)

**Create** (new files not present in Phase 0):
- `pyttd/recorder.py` — Python wrapper around C recorder
- `pyttd/query.py` — trace data query functions
- `pyttd/runner.py` — user script execution
- `tests/test_recorder.py`:
  - Test recording a simple function: create a function with known number of lines, record it via `Recorder`, assert `total_frames` matches expected count
  - Test `sequence_no` ordering: all frames have strictly monotonically increasing `sequence_no` values
  - Test frame events: first event is `'call'`, last is `'return'`, intermediate events are `'line'`
  - Test `call_depth`: top-level function has `call_depth == 0`, nested calls increment depth
  - Test `locals_snapshot`: `'line'` events have non-NULL `locals_snapshot` that parses as valid JSON
  - Test ignore patterns: stdlib frames are NOT recorded (only user code frames appear)
  - Test exception recording: function that raises produces `'exception'` event
  - All tests use `tmp_path` for DB isolation
- `tests/test_ringbuf.py`:
  - Test basic flush: record a few frames, verify they appear in DB after `stop_recording()`
  - Test flush callback receives correctly formatted dicts with expected keys
  - Test recording stats: `get_recording_stats()` returns correct `frame_count` after recording
- `samplecode/` — populate `edge_case_samples.py` (generators, exceptions, nested calls, recursion) and `threads_sample.py` (basic threading, ignored in Phase 1)

**Testing C code:** Run tests under AddressSanitizer (ASAN) during development: `CFLAGS="-fsanitize=address" LDFLAGS="-fsanitize=address" .venv/bin/pip install -e .` catches buffer overflows and use-after-free in the ring buffer and checkpoint code. Both `CFLAGS` and `LDFLAGS` are needed (the sanitizer runtime must be linked). Add a CI step for ASAN builds.

### Verify

1. `.venv/bin/pip install -e .` compiles the full C extension without warnings
2. `.venv/bin/python -m pyttd record samplecode/basic_sample_function.py` populates SQLite DB (`samplecode/basic_sample_function.pyttd.db` — in the script's directory)
3. `.venv/bin/python -m pyttd query --last-run --frames --limit 20 --db samplecode/basic_sample_function.pyttd.db` dumps first 20 frames with sequence numbers, line numbers, function names, call depths, and locals
4. `.venv/bin/pytest tests/test_recorder.py` — records a known function, asserts correct frame count and sequence ordering
5. `.venv/bin/pytest tests/test_ringbuf.py` — overflow handling, flush batch sizes
6. Performance: recording overhead < 5x for compute-bound code, < 2x for I/O-bound code

---

## Phase 2: Fork-Based Checkpointing

**Goal:** Periodically `fork()` during recording to create full-process snapshots. Each checkpoint is a frozen child waiting on a pipe. Restore = signal child to resume, fast-forward to target frame, report state back via result pipe.

### ExecutionFrames schema — no changes needed

The `ExecutionFrames` model is unchanged in Phase 2. The `Checkpoint` table records `sequence_no`, which is the primary lookup key for `find_nearest_checkpoint`. A `checkpoint_id` field on `ExecutionFrames` was considered but dropped — no downstream phase reads it, the UPDATE that sets it may match zero rows (frame not yet flushed from ring buffer), and the `Checkpoint` table provides the same information via a JOIN on `(run_id, sequence_no)`. This avoids schema migration complexity.

### Key C components

**`checkpoint.c` — internal function `checkpoint_do_fork()`:**

The eval hook calls `checkpoint_do_fork(sequence_no, checkpoint_callback)` (an internal C function, NOT the Python-facing `pyttd_create_checkpoint`). Pipes are created inside the function. Returns 0 on success, -1 on failure (caller continues recording without checkpoint).

**Pre-fork synchronization (corrected GIL ordering):**

The parent must release the GIL BEFORE waiting for `pause_ack`. Otherwise, if the flush thread is blocked on `PyGILState_Ensure()`, it can never check `pause_requested`, causing deadlock.

```c
/* New condvar/flag globals (alongside existing g_flush_mutex/g_flush_cond): */
static pthread_cond_t g_pause_ack_cv = PTHREAD_COND_INITIALIZER;
static pthread_cond_t g_resume_cv = PTHREAD_COND_INITIALIZER;
static _Atomic int g_pause_requested = 0;
static _Atomic int g_pause_acked = 0;
```

**`checkpoint_do_fork()` implementation:**

```c
int checkpoint_do_fork(uint64_t sequence_no, PyObject *checkpoint_callback) {
    /* 0. Create pipes */
    int cmd_pipe[2], result_pipe[2];
    if (pipe(cmd_pipe) < 0 || pipe(result_pipe) < 0) return -1;

    /* 1. Collect fds from existing checkpoints for child cleanup */
    int prior_fds[MAX_CHECKPOINTS * 2];
    int n_prior_fds = checkpoint_store_get_all_fds(prior_fds);

    /* 2. Pre-fork sync: pause flush thread */
    atomic_store(&g_pause_acked, 0);
    atomic_store(&g_pause_requested, 1);
    pthread_mutex_lock(&g_flush_mutex);
    pthread_cond_signal(&g_flush_cond);       /* wake flush thread if sleeping */
    PyThreadState *saved = PyEval_SaveThread(); /* release GIL */

    struct timespec timeout;
    clock_gettime(CLOCK_REALTIME, &timeout);
    timeout.tv_sec += 1;  /* 1-second timeout */
    while (!atomic_load(&g_pause_acked)) {
        int rc = pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
        if (rc == ETIMEDOUT) {
            /* Flush thread stuck — skip checkpoint, resume */
            atomic_store(&g_pause_requested, 0);
            pthread_cond_signal(&g_resume_cv);
            pthread_mutex_unlock(&g_flush_mutex);
            PyEval_RestoreThread(saved);
            close(cmd_pipe[0]); close(cmd_pipe[1]);
            close(result_pipe[0]); close(result_pipe[1]);
            return -1;
        }
    }
    pthread_mutex_unlock(&g_flush_mutex);  /* unlock BEFORE fork */
    /* Holding g_flush_mutex here was intentional — pthread_cond_timedwait
     * atomically releases it, allowing the flush thread to proceed
     * with its pause acknowledgment. */

    /* 3. Thread safety check (all PYTTD_HAS_FORK platforms, not just macOS) */
    /* Note: this check must happen before fork, while GIL is released.
     * We skip it here for simplicity — the check is done in Python before
     * calling start_recording with a checkpoint_callback. */

    /* 4. Fork */
    pid_t pid = fork();
    if (pid < 0) {
        /* Fork failed — MUST resume flush thread */
        PyEval_RestoreThread(saved);
        pthread_mutex_lock(&g_flush_mutex);
        atomic_store(&g_pause_requested, 0);
        pthread_cond_signal(&g_resume_cv);
        pthread_mutex_unlock(&g_flush_mutex);
        close(cmd_pipe[0]); close(cmd_pipe[1]);
        close(result_pipe[0]); close(result_pipe[1]);
        return -1;
    }

    if (pid == 0) {
        /* === CHILD PROCESS === */
        checkpoint_child_init(cmd_pipe, result_pipe, prior_fds, n_prior_fds);
        /* checkpoint_child_init never returns — blocks on read() or _exit() */
        _exit(1);  /* unreachable */
    }

    /* === PARENT PROCESS === */
    /* 5. Re-acquire GIL */
    PyEval_RestoreThread(saved);

    /* 6. Resume flush thread */
    pthread_mutex_lock(&g_flush_mutex);
    atomic_store(&g_pause_requested, 0);
    pthread_cond_signal(&g_resume_cv);
    pthread_mutex_unlock(&g_flush_mutex);

    /* 7. Close unneeded pipe ends */
    close(cmd_pipe[0]);     /* parent doesn't read cmd */
    close(result_pipe[1]);  /* parent doesn't write result */

    /* 8. Add to checkpoint store */
    int idx = checkpoint_store_add(pid, cmd_pipe[1], result_pipe[0], sequence_no);
    if (idx < 0) {
        /* Store full and eviction failed — kill child */
        uint8_t die[9] = {0xFF};
        write(cmd_pipe[1], die, 9);
        close(cmd_pipe[1]);
        close(result_pipe[0]);
        waitpid(pid, NULL, 0);
        return -1;
    }

    /* 9. Call Python checkpoint callback (non-fatal on failure) */
    if (checkpoint_callback) {
        PyObject *args = Py_BuildValue("(iK)", (int)pid,
                                       (unsigned long long)sequence_no);
        if (args) {
            PyObject *result = PyObject_Call(checkpoint_callback, args, NULL);
            if (!result) {
                PyErr_WriteUnraisable(checkpoint_callback);
                PyErr_Clear();
                /* C store has the entry — continue without DB row */
            }
            Py_XDECREF(result);
            Py_DECREF(args);
        }
    }
    return 0;
}
```

**Flush thread pause check** — add after `flush_batch()` in the flush thread loop:
```c
/* After flush_batch() returns (GIL released, all Python calls done): */
if (atomic_load_explicit(&g_pause_requested, memory_order_acquire)) {
    pthread_mutex_lock(&g_flush_mutex);
    atomic_store(&g_pause_acked, 1);
    pthread_cond_signal(&g_pause_ack_cv);
    while (atomic_load(&g_pause_requested)) {
        pthread_cond_wait(&g_resume_cv, &g_flush_mutex);
    }
    pthread_mutex_unlock(&g_flush_mutex);
}
```

**Child post-fork initialization** — `checkpoint_child_init()`:

```c
static void checkpoint_child_init(int cmd_pipe[2], int result_pipe[2],
                                   int *prior_fds, int n_prior_fds) {
    /* 1. Reinitialize Python runtime */
    PyOS_AfterFork_Child();
    /* PyOS_AfterFork_Child() reinitializes GIL (child now holds it),
     * thread state, and import lock. Does NOT reset PEP 523 eval hook
     * or trace function — we rely on both surviving via COW. */

    /* 2. Update thread identity */
    g_main_thread_id = PyThread_get_thread_ident();

    /* 3. Disable recording state */
    g_recording = 0;              /* prevent stop_recording issues */
    g_flush_thread_created = 0;   /* prevent flush thread join */
    g_fast_forward = 0;           /* not yet — set on RESUME */
    g_inside_repr = 0;            /* reset reentrancy guard */
    g_frame_count = 0;            /* reset stats (cosmetic) */
    g_last_checkpoint_seq = 0;    /* irrelevant in child */
    /* NOTE: Do NOT call stop_recording() or clear_filters() in child —
     * fast-forward requires the inherited ignore filter arrays
     * (g_dir_filter/g_exact_filter strdup'd strings) to remain intact. */

    /* 4. Signal handling — child lifecycle is managed via cmd_pipe only */
    signal(SIGINT, SIG_IGN);
    signal(SIGTERM, SIG_IGN);
    signal(SIGPIPE, SIG_IGN);     /* write() returns EPIPE, not SIGPIPE */
    atomic_store(&g_stop_requested, 0);

    /* 5. Clear inherited trace functions (e.g., coverage.py).
     * The fast-forward eval hook re-installs pyttd_trace_func on each
     * frame entry (step 6 of eval hook), so clearing here only prevents
     * inherited external traces from running. */
    PyEval_SetTrace(NULL, NULL);

    /* 6. Reinitialize pthreads objects (undefined state after fork) */
    g_flush_mutex = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
    g_flush_cond = (pthread_cond_t)PTHREAD_COND_INITIALIZER;
    g_pause_ack_cv = (pthread_cond_t)PTHREAD_COND_INITIALIZER;
    g_resume_cv = (pthread_cond_t)PTHREAD_COND_INITIALIZER;

    /* 7. Free ring buffer memory (~17MB). Safe because g_recording=0
     * and g_fast_forward=0 — no eval hook will call ringbuf_push().
     * The child blocks on read() before any Python code executes. */
    ringbuf_destroy();

    /* 8. Close inherited file descriptors */
    close(cmd_pipe[1]);           /* child doesn't write to cmd */
    close(result_pipe[0]);        /* child doesn't read from result */
    /* Close prior checkpoint pipe fds to prevent fd/kernel buffer leak */
    for (int i = 0; i < n_prior_fds; i++) {
        close(prior_fds[i]);
    }
    /* Close inherited SQLite fd at C level — do NOT use db.close()
     * (Peewee's threading.Lock may be in locked state from flush thread) */
    /* close(sqlite_fd); — retrieve via g_sqlite_fd global set during connect */
    /* Close TCP socket fd if in server mode */
    /* close(g_tcp_socket_fd); */

    /* 9. Clear atexit handlers (CPython-specific: atexit._clear()).
     * Prevents inherited atexit handlers from running if the child's
     * fast-forward raises an unhandled exception. If _clear doesn't
     * exist (non-CPython), the call fails silently — mitigated by
     * the child using _exit(0) on DIE. */
    PyObject *atexit_mod = PyImport_ImportModule("atexit");
    if (atexit_mod) {
        PyObject *r = PyObject_CallMethod(atexit_mod, "_clear", NULL);
        Py_XDECREF(r);
        if (PyErr_Occurred()) PyErr_Clear();
        Py_DECREF(atexit_mod);
    } else {
        PyErr_Clear();
    }

    /* 10. Release GIL and block on command pipe */
    int cmd_fd = cmd_pipe[0];
    int result_fd = result_pipe[1];
    PyThreadState *saved_tstate = PyEval_SaveThread();

    /* Command loop — child never returns from here */
    checkpoint_child_command_loop(cmd_fd, result_fd, saved_tstate);
    /* unreachable */
}
```

**Child command loop** — `checkpoint_child_command_loop()`:

```c
static void checkpoint_child_command_loop(int cmd_fd, int result_fd,
                                           PyThreadState *saved_tstate) {
    while (1) {
        uint8_t cmd_buf[9];
        ssize_t n = read_all(cmd_fd, cmd_buf, 9);
        if (n <= 0) _exit(0);  /* pipe closed or error */

        uint8_t opcode = cmd_buf[0];
        uint64_t payload = pyttd_be64toh(*(uint64_t *)(cmd_buf + 1));

        if (opcode == 0xFF) {
            /* DIE — immediate exit, no cleanup */
            _exit(0);
        }

        /* Re-acquire GIL for Python operations */
        PyEval_RestoreThread(saved_tstate);

        if (opcode == 0x01) {  /* RESUME */
            uint64_t target_seq = payload;
            if (target_seq <= g_sequence_counter) {
                /* Backward RESUME — impossible, write error */
                serialize_error_result(result_fd, "already_past_target",
                                       g_sequence_counter);
            } else {
                /* Set fast-forward and return to eval hook.
                 * The eval hook continues: install trace → g_original_eval.
                 * This function call is the FIRST RESUME only —
                 * subsequent RESUMEs arrive while blocked inside
                 * checkpoint_wait_for_command() in the trace function. */
                recorder_set_fast_forward(1, target_seq);
                g_cmd_fd = cmd_fd;
                g_result_fd = result_fd;
                g_saved_tstate = saved_tstate;
                return;  /* return to eval hook (checkpoint_do_fork caller) */
            }
        } else if (opcode == 0x02) {  /* STEP */
            uint64_t delta = payload;
            if (delta == 0) {
                /* Re-serialize current state (no-op advance) */
                serialize_target_state(result_fd, -1, NULL);
            } else {
                /* Forward STEP — but child hasn't entered fast-forward yet.
                 * This case shouldn't arise for the initial command loop
                 * (first command should be RESUME). Write error. */
                serialize_error_result(result_fd, "step_before_resume",
                                       g_sequence_counter);
            }
        }

        /* Release GIL and wait for next command */
        saved_tstate = PyEval_SaveThread();
    }
}
```

**`checkpoint_wait_for_command()`** — called from trace function / eval hook when target is reached:

```c
/* Block on cmd_pipe, process next command, update g_fast_forward_target.
 * Returns 0 to continue fast-forward, never returns on DIE (_exit). */
static int checkpoint_wait_for_command(int cmd_fd) {
    PyThreadState *tstate = PyEval_SaveThread();  /* release GIL */
    uint8_t cmd_buf[9];
    ssize_t n = read_all(cmd_fd, cmd_buf, 9);
    if (n <= 0) _exit(0);

    uint8_t opcode = cmd_buf[0];
    uint64_t payload = pyttd_be64toh(*(uint64_t *)(cmd_buf + 1));

    PyEval_RestoreThread(tstate);  /* re-acquire GIL */

    if (opcode == 0xFF) _exit(0);  /* DIE */

    if (opcode == 0x01) {  /* RESUME(target) */
        if (payload <= g_sequence_counter) {
            serialize_error_result(g_result_fd, "already_past_target",
                                   g_sequence_counter);
            /* Recurse to wait for next command */
            return checkpoint_wait_for_command(cmd_fd);
        }
        recorder_set_fast_forward(1, payload);
        return 0;
    }

    if (opcode == 0x02) {  /* STEP(delta) */
        if (payload == 0) {
            serialize_target_state(g_result_fd, -1, NULL);
            return checkpoint_wait_for_command(cmd_fd);
        }
        recorder_set_fast_forward(1, g_sequence_counter + payload);
        return 0;
    }

    _exit(1);  /* unknown opcode */
}
```

**Child end-of-script handling:** When `g_original_eval` returns for the checkpoint frame and `g_fast_forward` is still set (script ended before or after reaching target), the eval hook must NOT return to the interpreter (which would trigger Python shutdown). Instead it enters a permanent command loop:

```c
/* In eval hook, after g_original_eval returns for a fast-forward frame: */
if (g_fast_forward) {
    /* Script ended during fast-forward */
    if (g_sequence_counter < g_fast_forward_target) {
        serialize_error_result(g_result_fd, "target_seq_unreachable",
                               g_sequence_counter);
    }
    /* Permanent loop — only DIE exits (via _exit) */
    while (1) {
        checkpoint_wait_for_command(g_cmd_fd);
        /* Any RESUME/STEP: write error since script completed */
        serialize_error_result(g_result_fd, "script_completed",
                               g_sequence_counter);
    }
}
```

**Checkpoint store** — `checkpoint_store.c/h`:

`#define MAX_CHECKPOINTS 32` hardcoded (no runtime parameter needed). Array of `CheckpointEntry`:

```c
typedef struct {
    int child_pid;
    int cmd_fd;
    int result_fd;
    uint64_t sequence_no;       /* original checkpoint position (immutable) */
    uint64_t current_position;  /* updated after each RESUME/STEP */
    int is_alive;
    int is_busy;                /* 1 during active RESUME/STEP I/O */
} CheckpointEntry;
```

Complete API:
```c
void checkpoint_store_init(void);
int  checkpoint_store_add(int child_pid, int cmd_fd, int result_fd,
                          uint64_t sequence_no);
     /* Handles eviction if store is full. Returns index (0..MAX-1) or -1. */
int  checkpoint_store_find_nearest(uint64_t target_seq);
     /* Finds entry with largest current_position <= target_seq. Returns index or -1. */
int  checkpoint_store_find_by_pid(int child_pid);
     /* Lookup by pid (stable id after GIL-released I/O). Returns index or -1. */
void checkpoint_store_update_position(int index, uint64_t new_position);
void checkpoint_store_evict(int index);
     /* Send DIE, close fds, waitpid, mark dead. */
int  checkpoint_to_evict(void);
     /* Thinning algorithm. Returns index of entry to evict. */
CheckpointEntry *checkpoint_store_get(int index);
int  checkpoint_store_count(void);   /* live count */
int  checkpoint_store_get_all_fds(int *out_fds);
     /* Populate out_fds with cmd_fd/result_fd from all live entries.
      * Returns count. out_fds must have space for MAX_CHECKPOINTS * 2. */
```

**Eviction algorithm** (smallest-gap thinning):
```c
int checkpoint_to_evict(void) {
    /* Sort live checkpoints by sequence_no (ascending).
     * Find the pair with the smallest gap.
     * Evict the earlier checkpoint of that pair.
     * Never evict the most recent checkpoint. */
    /* O(K) scan for K live checkpoints — trivial for K=32. */
    /* Naturally produces logarithmic spacing: dense regions thinned first. */
}
```

**`kill_all_checkpoints()`** — batch DIE then batch waitpid:
```c
/* Phase 1: Send DIE to all, close pipe fds */
for (int i = 0; i < count; i++) {
    if (!entries[i].is_alive) continue;
    uint8_t die[9] = {0xFF};
    write(entries[i].cmd_fd, die, 9);  /* best-effort */
    close(entries[i].cmd_fd);
    close(entries[i].result_fd);
}
/* Phase 2: Reap all (most already exited) */
for (int i = 0; i < count; i++) {
    if (!entries[i].is_alive) continue;
    if (waitpid(entries[i].child_pid, NULL, WNOHANG) == 0) {
        usleep(10000);  /* 10ms grace */
        if (waitpid(entries[i].child_pid, NULL, WNOHANG) == 0) {
            kill(entries[i].child_pid, SIGKILL);
            waitpid(entries[i].child_pid, NULL, 0);
        }
    }
    entries[i].is_alive = 0;
}
```

Initialize the checkpoint store in `start_recording()` (lazy init, matches ring buffer pattern).

**Python-level `checkpoints.py`**: Peewee model persisted to the DB. Records `{checkpoint_id, run_id, sequence_no}` for diagnostics and Phase 3 session recovery. The `child_pid` field is for diagnostics only (null after the session ends). The DB is NOT the source of truth for runtime checkpoint selection — the C-level `checkpoint_store` (with `current_position` tracking) is.

**`replay.c`** — `pyttd_restore_checkpoint()`:

```c
PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args) {
    uint64_t target_seq;
    if (!PyArg_ParseTuple(args, "K", &target_seq))  /* K = unsigned long long */
        return NULL;

    int idx = checkpoint_store_find_nearest(target_seq);
    if (idx < 0) {
        PyErr_SetString(PyExc_RuntimeError, "No usable checkpoint found");
        return NULL;
    }

    CheckpointEntry *e = checkpoint_store_get(idx);
    int cmd_fd = e->cmd_fd;
    int result_fd = e->result_fd;
    int child_pid = e->child_pid;
    e->is_busy = 1;

    /* Build and send RESUME command */
    uint8_t cmd[9] = {0x01};
    *(uint64_t *)(cmd + 1) = pyttd_htobe64(target_seq);

    Py_BEGIN_ALLOW_THREADS  /* release GIL for blocking I/O */
    write_all(cmd_fd, cmd, 9);

    /* Read result with timeout (5 seconds) */
    struct pollfd pfd = { .fd = result_fd, .events = POLLIN };
    int rc = poll(&pfd, 1, 5000);
    Py_END_ALLOW_THREADS

    e->is_busy = 0;

    if (rc <= 0) {
        /* Timeout or error — kill child, remove from store */
        kill(child_pid, SIGKILL);
        waitpid(child_pid, NULL, 0);
        checkpoint_store_evict(idx);
        PyErr_SetString(PyExc_RuntimeError, "Checkpoint child timed out");
        return NULL;
    }

    /* Read length-prefixed JSON result */
    uint32_t net_len;
    Py_BEGIN_ALLOW_THREADS
    read_all(result_fd, &net_len, 4);
    Py_END_ALLOW_THREADS
    uint32_t len = ntohl(net_len);

    char *buf = (char *)malloc(len + 1);
    if (!buf) return PyErr_NoMemory();
    Py_BEGIN_ALLOW_THREADS
    read_all(result_fd, buf, len);
    Py_END_ALLOW_THREADS
    buf[len] = '\0';

    /* Update current_position (lookup by pid in case array was compacted) */
    int new_idx = checkpoint_store_find_by_pid(child_pid);
    if (new_idx >= 0) {
        checkpoint_store_update_position(new_idx, target_seq);
    }

    /* Parse JSON to Python dict via json.loads() */
    PyObject *json_mod = PyImport_ImportModule("json");
    PyObject *result = PyObject_CallMethod(json_mod, "loads", "s", buf);
    Py_DECREF(json_mod);
    free(buf);
    if (!result) return NULL;  /* json.loads failed — malformed JSON */
    return result;  /* Python dict */
}
```

**Target-reached state serialization** — `serialize_target_state()`:

Called by the trace function (or eval hook) when `g_sequence_counter == g_fast_forward_target`. Must handle event-type-specific locals (`__return__`, `__exception__`):

```c
static int serialize_target_state(int result_fd, int event_type,
                                   PyObject *trace_arg) {
    PyFrameObject *frame = PyThreadState_GetFrame(PyThreadState_Get());
    PyCodeObject *code = PyFrame_GetCode(frame);
    int line_no = PyFrame_GetLineNumber(frame);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);

    /* Event-type-specific extra locals */
    PyObject *extra_key = NULL, *extra_val = NULL;
    if (event_type == PyTrace_RETURN && trace_arg != NULL) {
        extra_key = PyUnicode_FromString("__return__");
        extra_val = trace_arg;
    } else if (event_type == PyTrace_EXCEPTION && trace_arg != NULL) {
        extra_key = PyUnicode_FromString("__exception__");
        if (PyTuple_Check(trace_arg) && PyTuple_GET_SIZE(trace_arg) >= 2)
            extra_val = PyTuple_GET_ITEM(trace_arg, 1);
    }

    const char *locals_json = serialize_locals(
        (PyObject *)frame, g_locals_buf, sizeof(g_locals_buf),
        extra_key, extra_val);
    Py_XDECREF(extra_key);

    char escaped_filename[512], escaped_funcname[512];
    json_escape_string(filename, escaped_filename, sizeof(escaped_filename));
    json_escape_string(funcname, escaped_funcname, sizeof(escaped_funcname));

    char result_buf[sizeof(g_locals_buf) + 1024];
    int len = snprintf(result_buf, sizeof(result_buf),
        "{\"status\": \"ok\", \"seq\": %llu, \"file\": \"%s\", \"line\": %d, "
        "\"function_name\": \"%s\", \"call_depth\": %d, \"locals\": %s}",
        (unsigned long long)g_sequence_counter, escaped_filename, line_no,
        escaped_funcname, g_call_depth,
        locals_json ? locals_json : "{}");

    Py_DECREF(code);
    Py_DECREF(frame);

    if (len < 0 || (size_t)len >= sizeof(result_buf)) {
        /* Truncated — send error */
        const char *err = "{\"status\": \"error\", \"error\": \"result_too_large\"}";
        uint32_t err_len = htonl((uint32_t)strlen(err));
        write_all(result_fd, &err_len, 4);
        write_all(result_fd, err, strlen(err));
        return -1;
    }

    uint32_t net_len = htonl((uint32_t)len);
    write_all(result_fd, &net_len, 4);
    write_all(result_fd, result_buf, len);
    return 0;
}
```

**Error result serialization:**
```c
static void serialize_error_result(int result_fd, const char *error_code,
                                    uint64_t last_seq) {
    char buf[256];
    int len = snprintf(buf, sizeof(buf),
        "{\"status\": \"error\", \"error\": \"%s\", \"last_seq\": %llu}",
        error_code, (unsigned long long)last_seq);
    uint32_t net_len = htonl((uint32_t)len);
    write_all(result_fd, &net_len, 4);
    write_all(result_fd, buf, len);
}
```

**Note on child result trust model:** The child's `seq`, `file`, `line`, `function_name`, and `call_depth` fields are redundant with the DB (included for cross-validation). For non-deterministic code (before Phase 4), these may differ from the recording. The canonical source for metadata is the DB; the child's result is canonical only for `locals` (live objects vs DB's repr snapshots). `ReplayController.goto_frame()` merges DB metadata with child locals.

**Execution flow detail:** The checkpoint is created inside the frame eval hook, specifically **after recording the `call` event and pushing to the ring buffer, and before installing the trace function and calling the original eval function**. The eval hook uses a delta-based check:

```c
static uint64_t g_last_checkpoint_seq = 0;

/* After ringbuf_push(&call_event) and g_frame_count++: */
if (g_checkpoint_interval > 0 &&
    g_checkpoint_callback != NULL &&
    call_event.sequence_no > 0 &&
    (call_event.sequence_no - g_last_checkpoint_seq) >= (uint64_t)g_checkpoint_interval) {
    checkpoint_do_fork(call_event.sequence_no, g_checkpoint_callback);
    g_last_checkpoint_seq = call_event.sequence_no;
}
```

The delta-based check (not `sequence_no % interval`) guarantees a checkpoint is created within `checkpoint_interval` events of the last one, even when call events have non-contiguous sequence numbers (the counter increments for all event types). Reset `g_last_checkpoint_seq = 0` in `start_recording()`.

After `fork()`:
- The child enters `checkpoint_child_init()` → blocks on `read(cmd_pipe)`.
- When `RESUME` arrives, `checkpoint_child_command_loop` sets fast-forward and returns to the eval hook, which continues at step 6 (install trace) → step 7 (call original eval).
- The interpreter resumes executing the user's script from the checkpointed frame.

**Fast-forward mode** uses two separate functions, checked BEFORE the `g_recording` check:

```c
/* Eval hook entry: */
if (g_fast_forward) {
    return pyttd_eval_hook_fast_forward(tstate, iframe, throwflag);
}
if (!atomic_load_explicit(&g_recording, memory_order_relaxed) || g_inside_repr) {
    return g_original_eval(tstate, iframe, throwflag);
}
/* ... normal recording path ... */

/* Trace function entry: */
if (g_fast_forward) {
    return pyttd_trace_func_fast_forward(frame, what, arg);
}
if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) return 0;
/* ... normal recording path ... */
```

This cleanly separates fast-forward from recording. The child sets `g_recording = 0` (safe for flush thread protection) and `g_fast_forward = 1` (when RESUME arrives). The `g_fast_forward` check comes first, so `g_recording = 0` is never reached in fast-forward mode.

**Fast-forward eval hook** (`pyttd_eval_hook_fast_forward`):
- Section B (stop request): included but effectively dead code in child (`g_stop_requested` always 0)
- Section C (code extraction): identical — `PyUnstable_InterpreterFrame_GetCode`, `PyUnicode_AsUTF8`
- Section D (ignore filter): identical — `should_ignore(filename, funcname)`
- Section E (thread check): identical — `PyThread_get_thread_ident() != g_main_thread_id`
- If ignored: save/remove/restore trace (same as normal path)
- If not ignored: `g_call_depth++`, `g_sequence_counter++`, check if target reached, install trace, call `g_original_eval`, check exception_unwind (`g_sequence_counter++` if NULL+PyErr_Occurred), `g_call_depth--`, restore trace
- **Skipped** vs normal path: `serialize_locals`, `ringbuf_push`, `g_frame_count++`, `get_monotonic_time`, flush signal, checkpoint trigger
- COW note: `Py_DECREF(code)` is unavoidable (GetCode returns new reference) and triggers COW per frame. `Py_INCREF/XDECREF(saved_traceobj)` also trigger COW. Optimization: if `tstate->c_tracefunc == pyttd_trace_func`, skip the save/install/restore cycle entirely

**Fast-forward trace function** (`pyttd_trace_func_fast_forward`):
- `PyTrace_CALL`: skip (same as recording)
- `PyTrace_LINE`: `g_sequence_counter++`, check target → if reached: call `serialize_target_state(result_fd, -1, NULL)`, `checkpoint_wait_for_command()`, return 0
- `PyTrace_RETURN`: if `arg == NULL` skip (same as recording), else `g_sequence_counter++`, check target → if reached: call `serialize_target_state(result_fd, PyTrace_RETURN, arg)`, `checkpoint_wait_for_command()`, return 0
- `PyTrace_EXCEPTION`: `g_sequence_counter++`, check target → if reached: call `serialize_target_state(result_fd, PyTrace_EXCEPTION, arg)`, `checkpoint_wait_for_command()`, return 0
- **Skipped** vs normal path: `serialize_locals`, `ringbuf_push`, `g_frame_count++`, `get_monotonic_time`, flush signal

**Fast-forward sequence counter correctness:** Both the eval hook and trace function must increment `g_sequence_counter` at the same five points as during recording (INC-1 through INC-5), with identical gating conditions (ignore filter, thread check, PyTrace_CALL skip, arg==NULL skip). `g_call_depth` must be maintained (needed for target state serialization). The `g_inside_repr` guard (widened in recording to cover all of `serialize_locals()`) is irrelevant in fast-forward — `serialize_locals` is never called during fast-forward (only at the target). Any `__del__`-triggered frames during recording's `serialize_locals()` are suppressed by `g_inside_repr`; in fast-forward, `serialize_locals` is skipped entirely, so zero events from both paths (match).

**Known divergence vectors:**
- Non-deterministic execution (different branches from I/O, random) → Phase 4 fix with I/O hooks
- If non-deterministic values cause re-executed code to take a different branch, the fast-forward sequence counter will diverge and the target frame may not correspond to the original recording

**Thread safety check (all `PYTTD_HAS_FORK` platforms, not just macOS):** Before forking, check `threading.active_count()`. If > 1 (user-created Python threads — the flush thread is a C pthread, not a Python thread), log a warning via `PyErr_WarnEx(PyExc_RuntimeWarning, ...)` and skip the checkpoint. `fork()` with active threads is unsafe on ALL platforms (mutexes in child are in undefined state). On Python 3.12+, the fork itself would also emit `DeprecationWarning` — suppress it since we've already warned. Implementation: call `PyObject_CallNoArgs()` on the `threading.active_count` function from C (cached during `start_recording`).

**Windows:** Stubs return `PYTTD_ERR_NO_FORK` (defined in `platform.h`). All cold navigation requests fall through to warm-only mode.

**Required C includes for Phase 2 files:**
```c
/* checkpoint.c, checkpoint_store.c, replay.c: */
#include <unistd.h>     /* fork, pipe, close, read, write */
#include <sys/wait.h>   /* waitpid, WNOHANG */
#include <signal.h>     /* signal, SIG_IGN, SIGPIPE */
#include <errno.h>      /* EINTR, EPIPE */
#include <poll.h>        /* poll (for result pipe timeout) */
#include <arpa/inet.h>  /* htonl, ntohl */
```

**64-bit byte order helpers** (in `platform.h` — no portable standard function exists):
```c
static inline uint64_t pyttd_htobe64(uint64_t host) {
    uint32_t hi = htonl((uint32_t)(host >> 32));
    uint32_t lo = htonl((uint32_t)(host & 0xFFFFFFFF));
    return ((uint64_t)lo << 32) | hi;
}
static inline uint64_t pyttd_be64toh(uint64_t big) {
    uint32_t hi = ntohl((uint32_t)(big >> 32));
    uint32_t lo = ntohl((uint32_t)(big & 0xFFFFFFFF));
    return ((uint64_t)lo << 32) | hi;
}
```

**Pipe I/O helpers** (in `checkpoint.c` or a shared utility):
```c
static ssize_t write_all(int fd, const void *buf, size_t len) {
    size_t written = 0;
    while (written < len) {
        ssize_t n = write(fd, (const char *)buf + written, len - written);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;  /* EPIPE or other error */
        }
        written += n;
    }
    return (ssize_t)written;
}

static ssize_t read_all(int fd, void *buf, size_t len) {
    size_t total = 0;
    while (total < len) {
        ssize_t n = read(fd, (char *)buf + total, len - total);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            return n;  /* 0 = EOF, -1 = error */
        }
        total += n;
    }
    return (ssize_t)total;
}
```

**Ring buffer defense-in-depth** — add `initialized` guards to `ringbuf_push()`, `ringbuf_pop_batch()`, `ringbuf_pool_copy()`, and `ringbuf_fill_percent()`:
```c
int ringbuf_push(const FrameEvent *event) {
    if (!g_rb.initialized) return PYTTD_RINGBUF_ERROR;
    /* ... existing code ... */
}
```
This prevents NULL dereference crashes if the ring buffer is accidentally accessed after `ringbuf_destroy()` in a checkpoint child.

**`recorder.h` Phase 2 additions:**
```c
void recorder_set_fast_forward(int enabled, uint64_t target_seq);
uint64_t recorder_get_sequence_counter(void);
int recorder_get_call_depth(void);
```
Variables remain `static` in `recorder.c` — exposed only via getter/setter functions. `checkpoint.c` calls `recorder_set_fast_forward(1, target_seq)` when RESUME arrives.

**New globals in `recorder.c`:**
```c
static int g_fast_forward = 0;
static uint64_t g_fast_forward_target = 0;
static uint64_t g_last_checkpoint_seq = 0;
static PyObject *g_checkpoint_callback = NULL;
static int g_checkpoint_interval = 0;
/* Child-only globals (set during child init): */
static int g_cmd_fd = -1;
static int g_result_fd = -1;
static PyThreadState *g_saved_tstate = NULL;
```

Initialize all in `start_recording()`, reset in `stop_recording()`. `g_checkpoint_callback` follows the same `Py_INCREF`/`Py_XDECREF` pattern as `g_flush_callback`.

### Pipe command protocol

Commands sent from parent to checkpoint child via `cmd_pipe`. Each command is a fixed-size binary message:

```
Command format: [1-byte opcode] [8-byte uint64 payload, big-endian]
                 (use pyttd_htobe64/pyttd_be64toh for byte order conversion)

Opcodes:
  0x01 = RESUME   payload = target_seq (fast-forward to this sequence number)
  0x02 = STEP     payload = delta as uint64 (forward only: advance delta events from current position)
  0xFF = DIE      payload = ignored (child calls _exit(0) immediately)

Protocol is strictly request-response: parent sends one command, reads one result, repeat.
```

**RESUME semantics:** Child fast-forwards from its current position to `target_seq`. If `target_seq <= current_position`, child writes an error result `{"status": "error", "error": "already_past_target"}` without entering fast-forward.

**STEP semantics:**
- `delta == 0`: re-serialize current state without advancing (useful for refreshing locals)
- `delta > 0`: fast-forward `delta` events from current position. If the script ends before advancing `delta` events, write error `{"status": "error", "error": "step_beyond_end", "actual_delta": <actual>}`

Result sent from child to parent via `result_pipe`. Uses a length-prefixed protocol to handle payloads > 64KB (OS pipe buffer limit):

```
Result format: [4-byte big-endian length N] [N bytes of JSON payload]
               (use htonl/ntohl for the 4-byte length)

Success: {"status": "ok", "seq": <uint64>, "file": "...", "line": <int>,
          "function_name": "...", "call_depth": <int>, "locals": {...}}
Error:   {"status": "error", "error": "<code>", "last_seq": <uint64>}
```

Both parent and child use `write_all()` / `read_all()` helpers that handle `EINTR` and short reads/writes. The child has `signal(SIGPIPE, SIG_IGN)` so pipe write errors return `EPIPE` instead of killing the process. The parent uses `poll()` with a 5-second timeout before reading the result to detect child deadlocks (e.g., from `PyOS_AfterFork_Child` hanging on an internal mutex).

### Checkpoint lifecycle and warm child management

**Checkpoint consumption:** When a checkpoint child receives `RESUME(target_seq)`, it fast-forwards from its checkpoint origin to `target_seq`. After fast-forwarding, the child's process state reflects `target_seq`, NOT its original checkpoint position — the original checkpoint state is irreversibly consumed. The C-level `checkpoint_store` tracks the child's `current_position` (updated after each `RESUME` or `STEP`) separately from its original `sequence_no`. `checkpoint_store_find_nearest(target_seq)` only considers checkpoints whose `current_position` ≤ `target_seq`.

**Warm child:** The most recently resumed child stays alive in a "warm" state. The parent sends `STEP(+N)` to the warm child for forward incremental cold navigation. `STEP` only supports forward movement — a checkpoint child cannot step backward because its prior process state is gone after fast-forward.

**Backward cold navigation:** Step-back and reverse-continue are always warm (SQLite read). Backward cold navigation only occurs via `goto_frame` to a position before the warm child's current position. The parent finds a different checkpoint whose `current_position` ≤ the target. If no unconsumed checkpoint covers the target, the system falls back to warm-only navigation (SQLite read with `repr()` snapshots).

**Lifecycle and shutdown:**
- `recorder.stop()` — stops recording (flush thread, eval hook) but leaves checkpoint children alive for replay
- `recorder.kill_checkpoints()` — new method, sends DIE to all children, updates DB
- `recorder.cleanup()` — closes DB, also calls `kill_checkpoints()` if not already called
- `_cmd_record` (CLI): calls `stop()`, then `kill_checkpoints()`, then `cleanup()` (CLI doesn't need cold replay after recording)
- Server mode: calls `stop()` after recording, keeps checkpoints alive for navigation, calls `kill_checkpoints()` + `cleanup()` on disconnect

The child exits when: (a) the user jumps outside this child's forward-reachable window, (b) the session ends (`DIE` command), (c) the parent evicts it for a new checkpoint, or (d) the child's fast-forward finishes (script completes) — in which case the child enters a permanent command loop responding to all RESUME/STEP with `"script_completed"` errors until DIE arrives.

### Checkpoint index model (`pyttd/models/checkpoints.py`)

```python
from peewee import AutoField, BigIntegerField, BooleanField, IntegerField, ForeignKeyField
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs

class Checkpoint(_BaseModel):
    checkpoint_id = AutoField()  # auto-increment primary key
    run_id = ForeignKeyField(Runs, backref='checkpoints', field='run_id')
    sequence_no = BigIntegerField()          # frame seq at which this checkpoint was taken
    child_pid = IntegerField(null=True)      # null after child is killed or session ends
    is_alive = BooleanField(default=True)    # False = evicted/killed

    class Meta:
        indexes = (
            (('run_id', 'sequence_no'), False),
        )
```

**Checkpoint creation flow:** The eval hook uses a delta-based check (see execution flow detail) and calls the internal C function `checkpoint_do_fork(sequence_no, checkpoint_callback)`. After a successful `fork()`, `checkpoint_do_fork` calls the Python callback with `(child_pid, sequence_no)`. The callback is non-fatal — if it raises (e.g., DB write fails), the error is logged via `PyErr_WriteUnraisable` and cleared. The C-level `checkpoint_store` is the source of truth; the DB row is for diagnostics/persistence.

**Stale state on restart:** On startup (`Recorder.start()` or server init), clear stale checkpoint state from any previous crashed session:
```python
Checkpoint.update(is_alive=False, child_pid=None).execute()
```

### CLI replay subcommand

Replace the Phase 1 stub in `cli.py`:
```python
def _cmd_replay(args):
    from pyttd.replay import ReplayController
    from pyttd.query import get_last_run
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models import storage
    import glob as globmod

    db_path = args.db
    if not db_path:
        dbs = sorted(globmod.glob(f"*{DB_NAME_SUFFIX}"), key=os.path.getmtime, reverse=True)
        if not dbs:
            print("No .pyttd.db files found. Use --db to specify path.")
            sys.exit(1)
        db_path = dbs[0]

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(1)

    run = get_last_run(db_path)
    controller = ReplayController()
    # CLI always uses warm-only (no live checkpoint children after recording exits)
    result = controller.warm_goto_frame(run.run_id, args.goto_frame)
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    print(f"Frame {args.goto_frame}: {result}")
    storage.close_db()
```

**Note:** The CLI `replay` command always uses warm-only navigation (SQLite reads) via `warm_goto_frame()` since checkpoint children don't survive the recording process exit. Cold navigation (checkpoint restore) is only available in the server mode (Phase 3+) where the recording and replay occur in the same process within a single session.

### Schema initialization update

Update `recorder.py`'s `start()` method to include the `Checkpoint` model in `initialize_schema`:
```python
from pyttd.models.checkpoints import Checkpoint
storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])
```

### C signature update

Update `pyttd_start_recording()` kwlist from `{"flush_callback", "buffer_size", "flush_interval_ms", NULL}` to `{"flush_callback", "buffer_size", "flush_interval_ms", "checkpoint_callback", "checkpoint_interval", NULL}`. Format string changes from `"O|ii"` to `"O|iiOi"`.

```c
PyObject *checkpoint_cb = NULL;  /* initialized to NULL, not Py_None */
int checkpoint_interval = 0;    /* 0 = disabled (no checkpoints) */

/* After parsing: */
if (checkpoint_cb && checkpoint_cb != Py_None && PyCallable_Check(checkpoint_cb)) {
    g_checkpoint_callback = checkpoint_cb;
    Py_INCREF(g_checkpoint_callback);
}
if (checkpoint_interval > 0) {
    g_checkpoint_interval = checkpoint_interval;
} else if (checkpoint_cb && checkpoint_interval < 0) {
    PyErr_SetString(PyExc_ValueError, "checkpoint_interval must be >= 0");
    return NULL;
}
/* Guard the eval hook modulo: */
/* if (g_checkpoint_interval > 0 && g_checkpoint_callback != NULL && ...) */
```

In `stop_recording()`:
```c
Py_XDECREF(g_checkpoint_callback);
g_checkpoint_callback = NULL;
g_checkpoint_interval = 0;
/* NOTE: do NOT kill checkpoint children here — they're needed for replay */
```

Update `PyttdConfig` to validate:
```python
@dataclass
class PyttdConfig:
    checkpoint_interval: int = 1000
    max_checkpoints: int = 32
    ring_buffer_size: int = 65536
    flush_interval_ms: int = 10
    ignore_patterns: list[str] = field(default_factory=list)
    db_path: str | None = None

    def __post_init__(self):
        if self.checkpoint_interval < 0:
            raise ValueError("checkpoint_interval must be >= 0")
```

Update `recorder.py` with complete Phase 2 changes:
```python
class Recorder:
    def start(self, db_path: str, script_path: str | None = None):
        """Initialize DB, create Runs record, set ignore patterns, install frame eval hook."""
        from pyttd.models.checkpoints import Checkpoint
        storage.connect_to_db(db_path)
        storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])
        # Clear stale checkpoint state from any previous crashed session
        Checkpoint.update(is_alive=False, child_pid=None).execute()
        self._run = Runs.create(script_path=script_path)
        all_ignore = list(INTERNAL_IGNORE) + list(self.config.ignore_patterns)
        pyttd_native.set_ignore_patterns(all_ignore)
        try:
            pyttd_native.start_recording(
                flush_callback=self._on_flush,
                buffer_size=self.config.ring_buffer_size,
                flush_interval_ms=self.config.flush_interval_ms,
                checkpoint_callback=self._on_checkpoint,
                checkpoint_interval=self.config.checkpoint_interval,
            )
        except Exception:
            self._run.delete_instance()
            self._run = None
            storage.close_db()
            raise
        self._recording = True

    def stop(self) -> dict:
        """Stop recording. Does NOT close DB or kill checkpoints —
        they're needed for replay. Call kill_checkpoints() + cleanup()
        during session shutdown."""
        if not self._recording:
            return {}
        pyttd_native.stop_recording()
        self._recording = False
        stats = pyttd_native.get_recording_stats()
        if self._run:
            self._run.timestamp_end = datetime.now().timestamp()
            self._run.total_frames = stats.get('frame_count', 0)
            self._run.save()
        return stats

    def kill_checkpoints(self):
        """Send DIE to all live checkpoint children. Called during shutdown."""
        pyttd_native.kill_all_checkpoints()
        from pyttd.models.checkpoints import Checkpoint
        Checkpoint.update(is_alive=False, child_pid=None).where(
            Checkpoint.run_id == self._run.run_id
        ).execute()

    def cleanup(self):
        """Close DB connection. Called during session shutdown."""
        self.kill_checkpoints()
        storage.close_db()

    def _on_checkpoint(self, child_pid: int, sequence_no: int):
        """Called by C eval hook (with GIL held) after successful fork().
        Non-fatal — exception is logged and cleared by C code."""
        from pyttd.models.checkpoints import Checkpoint
        Checkpoint.create(run_id=self._run.run_id, sequence_no=sequence_no,
                          child_pid=child_pid)

    # ... _on_flush unchanged ...
```

**CLI `_cmd_record` changes:** Don't pass `checkpoint_callback` in CLI record mode — only in `serve` mode. Creating fork children during CLI recording wastes resources (children are killed on exit, never used for cold replay):
```python
def _cmd_record(args):
    # ...
    config = PyttdConfig(checkpoint_interval=0)  # disable checkpoints in CLI mode
    recorder = Recorder(config)
    # ...
    finally:
        stats = recorder.stop()
        recorder.cleanup()  # kills any checkpoints (none created) + closes DB
```

### Create

- `ext/checkpoint.c/h` — full implementation: `checkpoint_do_fork()`, `checkpoint_child_init()`, `checkpoint_child_command_loop()`, `checkpoint_wait_for_command()`, `serialize_target_state()`, `serialize_error_result()`, pre-fork condvar sync, pipe I/O helpers (`write_all`/`read_all`). Header declares internal C function only (not Python-facing):
  ```c
  /* checkpoint.h */
  int checkpoint_do_fork(uint64_t sequence_no, PyObject *checkpoint_callback);
  ```
- `ext/checkpoint_store.c/h` — full implementation: C-level `CheckpointEntry` array, `find_nearest()` (by `current_position`), smallest-gap eviction, batch kill, `get_all_fds()`. See full API in Key C Components section.
- `ext/replay.c/h` — full implementation: `pyttd_restore_checkpoint()` (Python-facing — parses target_seq, finds checkpoint, sends RESUME, reads result with poll timeout, parses JSON via `json.loads()`, updates `current_position` by pid lookup after GIL re-acquire). See implementation in Key C Components section.
- `ext/platform.h` — add `pyttd_htobe64()` / `pyttd_be64toh()` inline helpers
- `pyttd/models/checkpoints.py` — Peewee model with composite index on `(run_id, sequence_no)`
- `pyttd/replay.py` — Python wrapper around C checkpoint/replay:
  ```python
  import json
  import pyttd_native
  from pyttd.models.frames import ExecutionFrames

  class ReplayController:
      def goto_frame(self, run_id, target_seq) -> dict:
          """Cold navigation: restore checkpoint, fast-forward, return frame state.
          Falls back to warm-only navigation if no usable checkpoint.
          Cold result merges DB metadata with child's live locals."""
          try:
              cold_result = pyttd_native.restore_checkpoint(target_seq)
          except Exception:
              return self.warm_goto_frame(run_id, target_seq)

          if cold_result.get("status") == "error":
              return self.warm_goto_frame(run_id, target_seq)

          # Merge: metadata from DB (canonical), locals from child (live objects)
          db_frame = ExecutionFrames.get_or_none(
              (ExecutionFrames.run_id == run_id) &
              (ExecutionFrames.sequence_no == target_seq))
          if db_frame:
              return {
                  "seq": target_seq,
                  "file": db_frame.filename,
                  "line": db_frame.line_no,
                  "function_name": db_frame.function_name,
                  "call_depth": db_frame.call_depth,
                  "locals": cold_result.get("locals", {}),
              }
          # DB frame not found (shouldn't happen for valid target_seq)
          return cold_result

      def warm_goto_frame(self, run_id, target_seq) -> dict:
          """Warm-only navigation: read frame data directly from SQLite
          (repr snapshots only, no live objects)."""
          frame = ExecutionFrames.get_or_none(
              (ExecutionFrames.run_id == run_id) &
              (ExecutionFrames.sequence_no == target_seq))
          if frame is None:
              return {"error": "frame_not_found", "target_seq": target_seq}
          locals_data = json.loads(frame.locals_snapshot) if frame.locals_snapshot else {}
          return {"seq": target_seq, "file": frame.filename, "line": frame.line_no,
                  "function_name": frame.function_name, "call_depth": frame.call_depth,
                  "locals": locals_data, "warm_only": True}
  ```
- `tests/test_checkpoint.py` — detailed test cases:
  - Fork creates child (verify child PID)
  - Pipe IPC round-trip (send RESUME, receive result)
  - Exponential thinning eviction (create > max checkpoints, verify correct ones evicted)
  - Child cleanup on DIE
  - Thread safety check: skip on macOS/Linux with active threads
  - Error on Windows (`PYTTD_ERR_NO_FORK`)
  - Fork failure recovery (mock fork to return -1, verify flush thread resumes)
- `tests/test_replay.py` — detailed test cases:
  - Warm fallback (no live children)
  - Cold replay (in-process record + goto_frame, verify locals match for deterministic code)
  - Target_seq out of range returns error dict
  - Checkpoint consumed past target falls back to warm
  - `__return__` and `__exception__` keys present in cold result for return/exception events
- Recording fixture for Phase 2 tests (extends `conftest.py`):
  ```python
  @pytest.fixture
  def record_with_checkpoints(tmp_path):
      """Record a script with checkpoints enabled. Returns (db_path, run_id, stats)."""
      def _record(script_content, checkpoint_interval=100):
          script_file = tmp_path / "test_script.py"
          script_file.write_text(textwrap.dedent(script_content))
          db_path = str(tmp_path / "test.pyttd.db")
          delete_db_files(db_path)
          config = PyttdConfig(checkpoint_interval=checkpoint_interval)
          recorder = Recorder(config)
          recorder.start(db_path, script_path=str(script_file))
          # ... run script, stop, return ...
      yield _record
  ```

### Update

- **`pyttd/cli.py`** — Update the `main()` dispatch to call `_cmd_replay(args)` instead of the stub print:
  ```python
  elif args.command == 'replay':
      _cmd_replay(args)
  ```
  Update `_cmd_record` to disable checkpoints (CLI record mode exits after recording — checkpoint children would be immediately killed, wasting resources):
  ```python
  config = PyttdConfig(checkpoint_interval=0)  # was: args.checkpoint_interval
  ```
  The `--checkpoint-interval` CLI arg remains for future `serve` mode integration.

- **`tests/conftest.py`** — Update `db_setup` fixture to include `Checkpoint` in schema initialization:
  ```python
  from pyttd.models.checkpoints import Checkpoint
  storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])
  ```
  Update `record_func` fixture similarly:
  ```python
  storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])  # in _record()
  ```

- **`ext/pyttd_native.c`** — Keep `create_checkpoint` as `METH_NOARGS` in the method table (thin wrapper that calls `checkpoint_do_fork()`). Add new entries for Phase 2 functions:
  ```c
  {"restore_checkpoint", (PyCFunction)pyttd_restore_checkpoint, METH_VARARGS, "..."},
  {"kill_all_checkpoints", (PyCFunction)pyttd_kill_all_checkpoints, METH_NOARGS, "..."},
  {"get_checkpoint_count", (PyCFunction)pyttd_get_checkpoint_count, METH_NOARGS, "..."},
  ```

- **`ext/recorder.h`** — Add Phase 2 getter/setter declarations (needed by `checkpoint.c` and `replay.c`):
  ```c
  /* Phase 2 getters/setters */
  extern _Atomic int g_fast_forward;
  extern _Atomic uint64_t g_fast_forward_target;
  uint64_t pyttd_get_sequence_counter(void);
  int pyttd_get_call_depth(void);
  void pyttd_set_recording(int value);
  ```

- **`ext/platform.h`** — Add portable 64-bit byte order helpers:
  ```c
  #if defined(__APPLE__)
    #include <libkern/OSByteOrder.h>
    #define pyttd_htobe64(x) OSSwapHostToBigInt64(x)
    #define pyttd_be64toh(x) OSSwapBigToHostInt64(x)
  #elif defined(__linux__)
    #include <endian.h>
    #define pyttd_htobe64(x) htobe64(x)
    #define pyttd_be64toh(x) be64toh(x)
  #else
    /* Fallback — manual byte swap */
    static inline uint64_t pyttd_htobe64(uint64_t x) {
        return ((x & 0xFF) << 56) | ((x & 0xFF00) << 40) |
               ((x & 0xFF0000) << 24) | ((x & 0xFF000000) << 8) |
               ((x >> 8) & 0xFF000000) | ((x >> 24) & 0xFF0000) |
               ((x >> 40) & 0xFF00) | ((x >> 56) & 0xFF);
    }
    static inline uint64_t pyttd_be64toh(uint64_t x) { return pyttd_htobe64(x); }
  #endif
  ```

- **`setup.py`** — Verify `sources` list includes all new C files: `checkpoint.c`, `checkpoint_store.c`, `replay.c`. (Already present in the existing sources list per R5-2.)

- **`DESIGN.md`** — Update with Phase 2 architectural decisions after implementation is complete (see Phase 2 Verify step 7).

### Verify

1. **CLI record (checkpoints disabled):** `.venv/bin/python -m pyttd record samplecode/basic_sample_function.py` — verify recording works with `checkpoint_interval=0` (no fork calls). DB should contain frames but no Checkpoint rows.

2. **CLI replay (warm-only):** `.venv/bin/python -m pyttd replay --last-run --goto-frame 750 --db samplecode/basic_sample_function.pyttd.db` — verify warm_goto_frame returns correct frame from SQLite (repr snapshot, not live objects). No checkpoint children involved.

3. **Unit tests — checkpoint lifecycle:**
   ```bash
   .venv/bin/pytest tests/test_checkpoint.py -v
   ```
   Test scenarios:
   - Fork creates child process, parent gets (pid, cmd_fd, result_fd) tuple
   - Pipe IPC: RESUME command → child fast-forwards → returns JSON result with status "ok"
   - STEP command: child steps N events forward, returns updated state
   - EXIT command: child exits cleanly
   - Eviction: add `max_checkpoints + 1` entries → oldest non-extreme checkpoint is evicted (smallest gap)
   - Fork failure: `checkpoint_do_fork` returns error, flush thread resumes (not stuck)
   - Inherited fd cleanup: child closes pipe fds from prior checkpoint siblings

4. **Unit tests — replay controller:**
   ```bash
   .venv/bin/pytest tests/test_replay.py -v
   ```
   Test scenarios:
   - `warm_goto_frame(seq)` returns dict with `file`, `line`, `function`, `locals` (repr strings)
   - `warm_goto_frame` with invalid seq returns None (uses `get_or_none`, not `get`)
   - In-process cold replay: record deterministic script, `cold_goto_frame` via checkpoint restore, verify locals match DB recording
   - Cold replay locals contain `__return__` key on return events, `__exception__` on exception events
   - `cold_goto_frame` with no suitable checkpoint falls back to warm

5. **Platform-specific:**
   - macOS: verify warning is logged when threads are active at fork time, checkpoint is skipped gracefully (not silently)
   - Linux: full checkpoint lifecycle works (fork + fast-forward + pipe IPC)
   - Windows/no-fork: `PYTTD_HAS_FORK` not defined → `create_checkpoint` raises `NotImplementedError`, warm navigation still works

6. **Edge cases to verify manually:**
   - `ringbuf_push` after `ringbuf_destroy` in child → no SIGSEGV (guard check on `g_rb.initialized`)
   - Recording a script that calls `sys.exit(0)` → checkpoint children don't prevent clean exit
   - `_cmd_record` with `--checkpoint-interval 500` still accepted by argparse (just overridden to 0 internally)

7. **Documentation:** Update DESIGN.md with Phase 2 architectural decisions:
   - Step back is always warm (confirmed)
   - Delta-based checkpoint trigger (`seq - last >= interval`, not modulo)
   - CLI record disables checkpoints; only serve mode uses them
   - `recorder.stop()` does NOT kill checkpoints (server needs them for replay)
   - Child result trust model: locals from child (live objects), metadata from DB
   - Exponential thinning eviction (smallest-gap algorithm)

### Phase 2 Review: Issues, Gaps, and Required Changes

> **Note:** All findings from Reviews 1-7 below have been incorporated into the Phase 2 plan text above (Create/Update/Verify/Implementation Order sections). These review sections are retained for reference and rationale — the plan above is the authoritative implementation specification.

The following issues were identified during a comprehensive review of Phase 2 against the implemented Phase 0+1 code, DESIGN.md architectural decisions, and the Phase 3+ downstream requirements. Issues are categorized by severity.

---

#### Critical: GIL deadlock in pre-fork synchronization (C1)

**Location:** checkpoint.c step 1 (Pre-fork) and step 2 (Fork)

The plan specifies this ordering:
1. Parent (eval hook, **GIL held**) sets `pause_requested`
2. Parent waits on `pause_ack` ← **still holds GIL**
3. Parent calls `PyEval_SaveThread()` to release GIL
4. Parent calls `fork()`

This deadlocks if the flush thread is blocked on `PyGILState_Ensure()` when `pause_requested` is set. Scenario:
- Parent holds GIL (in eval hook), waits on `pause_ack`
- Flush thread blocked on `PyGILState_Ensure` — can't acquire GIL because parent holds it
- Flush thread never checks `pause_requested`, never signals `pause_ack`
- Deadlock (the 1-second timeout fires, but this happens every time the flush thread is mid-GIL-acquisition)

**Fix:** Release GIL BEFORE waiting for `pause_ack`. The corrected sequence:
1. Parent sets `pause_requested`, signals `g_flush_cond` (wake flush thread if sleeping on condvar)
2. Parent calls `PyEval_SaveThread()` — releases GIL
3. Parent waits on `pause_ack` (now the flush thread can acquire GIL, finish its batch, release GIL, check flag, ack)
4. Parent calls `fork()` (GIL is released — safe)
5. Child: `PyOS_AfterFork_Child()` reinitializes GIL (child now holds it)
6. Parent: `PyEval_RestoreThread()` re-acquires GIL
7. Parent clears `pause_requested`, signals `resume_cv`

This is safe because:
- GIL is released before the blocking wait, so no GIL contention
- Fork happens with GIL released, so the child can safely reinitialize
- Between GIL release (step 2) and fork (step 4), other Python threads can run — but only the flush thread exists, and it will pause after completing its current flush iteration. The eval hook is interpreter-wide, so frames evaluated by the flush thread's Python calls (PyImport, db.close) go through it, but those are ignored by `should_ignore`. No user-code frames are recorded during this window.

#### Critical: Child process must close inherited file descriptors (C2)

**Location:** checkpoint.c step 3 (Child)

The plan says "Close unneeded pipe ends" but does NOT mention closing other inherited fds. After `fork()`, the child inherits:
- **TCP socket** to the debug adapter — if the parent dies, the adapter won't detect disconnect because the child still holds the socket fd
- **DB file handles** — WAL mode with multiple processes accessing the same DB can cause lock contention or corruption
- **Ring buffer memory** — ~1MB+ per child, wasted with up to 32 children (~32MB)

**Fix:** After `fork()` in the child:
1. Close TCP socket fd (pass it to `checkpoint_create` or retrieve from a global)
2. Call `ringbuf_destroy()` to free ring buffer memory
3. Close DB — call `PyImport_ImportModule("pyttd.models.base")` + `db.close()` (same pattern as flush thread cleanup), OR simply `close()` the raw fd (faster, no Python needed)
4. Close unneeded pipe ends (already in plan)

#### Critical: Child signal handling not specified (C3)

**Location:** checkpoint.c step 3 (Child)

After `fork()`, the child inherits the parent's signal handlers. If the user presses Ctrl-C:
- SIGINT is delivered to the entire process group (parent AND all checkpoint children)
- Each child's signal handler calls `request_stop()` → sets `g_stop_requested` in the child's memory
- During fast-forward, the child's eval hook checks this flag and raises `KeyboardInterrupt` — aborting the fast-forward mid-execution
- The child crashes without writing a result to the result pipe
- The parent blocks on `read(result_pipe)` forever

**Fix:** In the child, immediately after `PyOS_AfterFork_Child()`:
1. Reset signal handlers: `signal(SIGINT, SIG_IGN)` and `signal(SIGTERM, SIG_IGN)` — the child's lifecycle is managed exclusively via the `DIE` command on the cmd_pipe
2. Clear `g_stop_requested` to 0 (inherited value might be non-zero if stop was requested between fork and signal reset)

The parent handles Ctrl-C by sending `DIE` to all children via `kill_all_checkpoints`.

#### Critical: Eval hook function naming ambiguity (C4)

**Location:** "The C eval hook detects `sequence_no % checkpoint_interval == 0` and calls `pyttd_create_checkpoint()`"

`pyttd_create_checkpoint` is the Python-facing function in `PyttdMethods` (currently `METH_NOARGS`). The eval hook should call an **internal C function** (e.g., `checkpoint_do_fork()`), not the Python-facing wrapper. Calling the Python-facing function from C would go through the Python method dispatch, which is unnecessary overhead and doesn't match the calling convention (the eval hook has the GIL already, and needs to pass internal state like `g_sequence_counter` and the `checkpoint_callback`).

**Fix:** Define a new internal C function in `checkpoint.c`:
```c
/* Internal — called by eval hook with GIL held */
int checkpoint_do_fork(uint64_t sequence_no, PyObject *callback);
```
Keep `pyttd_create_checkpoint()` as the Python-facing wrapper (for manual/test use), which delegates to the internal function. Update the eval hook to call `checkpoint_do_fork()`.

#### Critical: Fast-forward target reached in trace function needs blocking (C5)

**Location:** "At `target_seq`, the hook/trace function serializes full frame state as JSON, writes to result pipe, then blocks again on `read(cmd_pipe)`"

Blocking in the trace function is architecturally correct (child is single-threaded, no other Python threads after fork), but the implementation is complex and error-prone:

1. The trace function signature returns `int` — it must return 0 to continue or -1 to raise an exception. It can't "block and then continue" without returning to the interpreter first.
2. After writing the result and blocking on `read(cmd_pipe)`, when a new command arrives (e.g., another STEP), the trace function needs to update `g_fast_forward_target` and return normally — the interpreter loop then continues executing the user's code and calls the trace function again on subsequent events.
3. If a `DIE` command arrives while blocked in the trace function, the child should exit. But `exit()` from within a Python trace function is unsafe (doesn't unwind properly). Should use `_exit()` (immediate process termination, no cleanup) since the child is a disposable snapshot.

**Fix:** The blocking logic should be in a shared helper function callable from both the eval hook and trace function:
```c
/* Block on cmd_pipe, process next command, update g_fast_forward_target.
 * Returns 0 to continue fast-forward, -1 on DIE. */
static int checkpoint_wait_for_command(int cmd_fd);
```
On `DIE`: call `_exit(0)` directly (safe in child, no resources to clean up).
On `RESUME`/`STEP`: update `g_fast_forward_target`, return 0.

The trace function calls this helper when `g_sequence_counter == g_fast_forward_target`, serializes state, writes to result pipe, then calls the helper. If helper returns 0, the trace function returns 0 (continue). If the helper indicates DIE (unreachable since `_exit` is called), it returns -1.

#### Critical: Fast-forward must handle child exiting before target (C6)

**Location:** "the fast-forward sequence counter will diverge and the target frame may not correspond to the original recording"

The plan acknowledges non-determinism (Phase 4 fixes it with I/O hooks) but doesn't specify what happens when the child's re-execution finishes before reaching `target_seq`. Scenarios:
- The user's script exits normally before the child reaches `target_seq` (non-deterministic branch taken)
- The user's script raises an unhandled exception during fast-forward
- The child re-executes fewer events than the original recording (determinism failure)

In all cases, the child exits `g_original_eval` without hitting `target_seq`. The eval hook finishes, control returns to the checkpoint function, and the child has nowhere to go — its call stack has unwound back to the `read(cmd_pipe)` loop.

**Fix:** The child must detect this and report an error:
1. After the child's top-level `g_original_eval` call returns (from `RESUME`), check if `g_sequence_counter < g_fast_forward_target`
2. If so, write an error result to the result pipe: `{"error": "target_seq_unreachable", "last_seq": <actual last seq>}`
3. Block on `cmd_pipe` for the next command (or exit if the script completed)

The parent's `pyttd_restore_checkpoint()` should check for the error field and raise `ReplayError` to the Python caller. `ReplayController.goto_frame()` already catches exceptions and falls back to warm.

---

#### Significant: `max_checkpoints` not passed to C code (S1)

**Location:** C signature update section

The plan adds `checkpoint_callback` and `checkpoint_interval` to `start_recording`, but NOT `max_checkpoints`. The C `checkpoint_store` needs to know the maximum to enforce eviction. `PyttdConfig.max_checkpoints = 32` exists in Python but is never communicated to C.

**Fix:** Either:
- (a) Add `max_checkpoints` as a parameter to `start_recording` and pass to `checkpoint_store_init(max)`, OR
- (b) Hardcode `#define MAX_CHECKPOINTS 32` in `checkpoint_store.h` (simpler, the value is unlikely to change, and the Python config is already 32)

Option (b) is recommended — avoids parameter bloat on `start_recording`.

#### Significant: `ReplayController` DB query vs C store inconsistency (S2)

**Location:** `replay.py` — `get_nearest_checkpoint()` and `goto_frame()`

The Python-level `get_nearest_checkpoint` queries the DB for `Checkpoint.sequence_no <= target_seq AND is_alive == 1`. But the C-level `checkpoint_store_find_nearest` checks `current_position <= target_seq` (tracking consumed checkpoints). These can disagree:
- DB says: checkpoint at seq 500 is alive (original position)
- C store says: this checkpoint was consumed to position 800 (can't serve target 600)

The Python code tries the C path and catches the exception on failure, falling back to warm. This works but is wasteful — every cold navigation attempt for a consumed checkpoint does a round-trip through C before falling back.

**Fix:** Add a C function `checkpoint_store_has_usable(uint64_t target_seq)` that returns 1/0 — the Python code can check this before attempting restore. Or simply accept the try/except pattern (it's fast, and the exception case is rare in practice).

#### Significant: CLI verify steps 2-3 incorrectly claim cold replay (S3)

**Location:** Verify section steps 2 and 3

The plan's verify step 2 says "restores from checkpoint at 500, fast-forwards to 750" — but the same plan says "The CLI `replay` command always uses warm-only navigation (SQLite reads) since checkpoint children don't survive the recording process exit." These contradict each other.

**Fix (already applied above):** Verify steps updated to reflect warm-only CLI behavior. Cold replay must be tested via an in-process test (record + replay in the same process, same as `test_replay.py`).

#### Significant: `_cmd_record` shutdown must kill checkpoint children (S4)

**Location:** `cli.py` `_cmd_record()` function

Currently `_cmd_record` calls `recorder.stop()` then `recorder.cleanup()`. It does NOT kill checkpoint children. After Phase 2, checkpoint children created during recording would become orphan processes when the recording process exits.

**Fix:** Add `pyttd_native.kill_all_checkpoints()` call (and DB update) to the shutdown path. Options:
- (a) Add to `recorder.stop()` — call `kill_all_checkpoints` before `stop_recording` (since stop_recording destroys the ring buffer, which the checkpoint children might reference indirectly)
- (b) Add to `recorder.cleanup()` — more explicit lifecycle
- (c) Add a new method `recorder.kill_checkpoints()` called by both `_cmd_record` and the server's shutdown handler

Option (a) is simplest: `recorder.stop()` should kill all checkpoint children as part of stopping the recording, since checkpoints are only meaningful during the recording session.

#### Significant: Thread safety check should apply on Linux too (S5)

**Location:** macOS safety section

The plan says to check `threading.active_count()` only on macOS. But `fork()` with active threads is unsafe on ALL platforms (mutexes in child are in undefined state). The only reason it's specifically mentioned for macOS is that macOS is more aggressive about detecting it (Python 3.12+ DeprecationWarning).

**Fix:** Check `threading.active_count()` on all `PYTTD_HAS_FORK` platforms, not just macOS. Log warning and skip checkpoint if user threads are detected. The pyttd flush thread is a C pthread (not a Python thread) and is handled separately via pre-fork synchronization.

#### Significant: `_warm_fallback` returns inconsistent `locals` type (S6)

**Location:** `replay.py` `_warm_fallback()`

The cold path (`pyttd_native.restore_checkpoint`) returns a dict where `"locals"` is a parsed dict (from the JSON written to the result pipe). The warm fallback returns `"locals": frame.locals_snapshot` — a raw JSON **string** from the DB.

Callers that do `result["locals"]["some_var"]` would work for cold path but crash for warm path (string indexing gives single characters, not dict values).

**Fix:** Parse the JSON string in the warm fallback:
```python
import json
locals_data = json.loads(frame.locals_snapshot) if frame.locals_snapshot else {}
return {"seq": target_seq, ..., "locals": locals_data, "warm_only": True}
```

#### Significant: Fast-forward `g_fast_forward` and `g_fast_forward_target` lifecycle (S7)

**Location:** Not explicitly specified in the plan

These globals need explicit lifecycle management:
- **`start_recording()`**: initialize `g_fast_forward = 0`, `g_fast_forward_target = 0`
- **`stop_recording()`**: reset both to 0
- **Child after fork**: both inherited as 0 (correct — fast-forward is set when RESUME arrives)
- **Child on RESUME**: set `g_fast_forward = 1`, `g_fast_forward_target = target_seq`
- **Child on target hit**: set `g_fast_forward = 0` (or keep it set until next command)
- **Child on DIE**: `_exit(0)` (no cleanup needed)

**Fix:** Add explicit initialization in `start_recording()` and `stop_recording()`. Document in DESIGN.md.

#### Significant: Result pipe payload format needs an envelope (S8)

**Location:** Pipe command protocol section

The result pipe currently specifies: `[4-byte length] [N bytes JSON]`. But the JSON payload is assumed to be a success response. There's no way to distinguish:
- Success: `{"seq": 750, "file": "...", "locals": {...}}`
- Error: `{"error": "target_seq_unreachable", "last_seq": 600}`
- Child exit: (pipe closed, parent gets EOF)

**Fix:** The JSON payload should include a `"status"` field:
```
Success: {"status": "ok", "seq": ..., "file": ..., "line": ..., ...}
Error:   {"status": "error", "error": "target_seq_unreachable", "last_seq": ...}
```
Or simply: the parent checks for the `"error"` key in the parsed JSON to distinguish success from failure. The pipe-closed (EOF) case is handled by `read()` returning 0.

#### Significant: `Checkpoint.is_alive` should use `BooleanField` (S9)

**Location:** `checkpoints.py` model

`is_alive = IntegerField(default=1)` works but `BooleanField(default=True)` is more semantic. Peewee's `BooleanField` maps to INTEGER 0/1 in SQLite (identical storage), but the Python API returns `True`/`False` instead of `1`/`0`, and queries use `.where(Checkpoint.is_alive == True)`.

**Fix:** Use `BooleanField(default=True)`.

---

#### Minor Issues

**M1. Child should clear `g_recording` flag**
After `fork()`, the child inherits `g_recording = 1`. In fast-forward mode, the eval hook checks `g_recording` (trace function line 361: `if (!g_recording) return 0;`). The child IS re-executing code, so `g_recording` being 1 is correct for the trace function counter-increment path. But it should be set to a distinct value (or a separate flag should be used) so that `stop_recording()` in the child (if called accidentally) doesn't try to restore eval hooks or join a non-existent flush thread.

**M2. Checkpoint creation should be gated on `call` events only**
The plan says "sequence_no % checkpoint_interval == 0" without specifying which event type. Since the checkpoint is created in the eval hook "after recording the call event," it is implicitly gated on call events. But the `g_sequence_counter` increments for ALL event types (call, line, return, exception, exception_unwind). If `sequence_no 1000` happens to be a `line` event in the trace function (not the eval hook), no checkpoint is created, and the next call event might be at sequence_no 1003. The plan should specify: "checkpoint check triggers on call events only, checking `call_event.sequence_no % checkpoint_interval == 0`" (which IS what the plan describes, since it's in the eval hook).

**M3. `ReplayController.kill_all()` executes bare `Checkpoint.update(...).execute()` without specifying `run_id`**
This updates ALL checkpoints across ALL runs. If multiple recordings share the same DB (unlikely given `delete_db_files`, but possible with `--db`), this would incorrectly mark other runs' checkpoints as dead. Add `.where(Checkpoint.run_id == run_id)`.

**M4. The plan doesn't mention updating `DESIGN.md`**
Phase 2 introduces significant new architectural concepts (fast-forward mode, pre-fork sync, pipe protocol, checkpoint consumption). These should be documented in DESIGN.md's architectural decisions section.

**M5. Project structure section lists `performance/clock.py` and `performance/performance.py`**
These files were deleted during the Phase 0+1 code review fixes. Update the project structure listing.

**M6. `pyttd_native.c` method table needs signature updates**
`pyttd_create_checkpoint` is currently `METH_NOARGS`. If it's kept as a Python-facing function (for testing/manual use), it should be updated to match the new internal API. Or it can remain a thin wrapper that calls the internal function with `g_checkpoint_callback` and `g_sequence_counter`.

**M7. `_cmd_replay` should check DB existence**
Same as the fix applied to `_cmd_query` in Phase 0+1 review — add `os.path.exists(db_path)` check before `get_last_run()`.

**M8. Test specifications are too vague**
`test_checkpoint.py` and `test_replay.py` need detailed test case descriptions:
- `test_checkpoint.py` should cover: fork creates child (verify child PID), pipe IPC round-trip (send RESUME, receive result), exponential thinning eviction (create > max checkpoints, verify correct ones are evicted), child cleanup on DIE, skip on macOS with threads, error on Windows
- `test_replay.py` should cover: warm fallback (no live children), cold replay (in-process record + goto_frame), locals match between recording and replay, target_seq out of range, checkpoint consumed past target

**M9. Missing `conftest.py` recording fixture for Phase 2 tests**
`test_checkpoint.py` and `test_replay.py` need a recording fixture that creates checkpoints. The Phase 1 `record_func` fixture in `test_recorder.py` doesn't pass `checkpoint_callback` or `checkpoint_interval` to `start_recording`. Either extend the existing fixture or create a new one in `conftest.py` (also addresses the duplicated fixture concern from Phase 1 review).

---

#### Implementation Order

1. **Platform helpers:** Update `ext/platform.h` with `pyttd_htobe64`/`pyttd_be64toh` byte-order macros.

2. **C internals (no Python interaction):**
   - `ext/checkpoint_store.c` — fixed-size array, `checkpoint_store_add`, `checkpoint_store_find_nearest`, `checkpoint_store_evict` (smallest-gap algorithm), `checkpoint_store_get_all_fds` (for child fd cleanup), `checkpoint_store_kill_all`. Full API per the Key C Components section above.
   - `ext/checkpoint.c` — `checkpoint_do_fork()` with corrected GIL ordering (release GIL → wait ack → fork → child init → parent re-acquire GIL). Pre-fork ring buffer flush. Fork failure recovery (resume flush thread). Child init sequence: `PyOS_AfterFork_Child`, `g_main_thread_id` update, `signal(SIGPIPE, SIG_IGN)`, `ringbuf_destroy`, inherited fd cleanup, `g_recording = 0`.
   - `ext/replay.c` — `pyttd_eval_hook_fast_forward` and `pyttd_trace_func_fast_forward` (with ignore filter, code extraction, depth tracking, trace install — only serialization/ringbuf/timing skipped). Child command loop (`cmd_loop`): RESUME/STEP/EXIT dispatch, `serialize_target_state` (with `__return__`/`__exception__` keys, `snprintf` truncation check), pipe I/O helpers (`write_all`, `read_all`). End-of-script handling (permanent command loop, not return to interpreter).

3. **recorder.c integration:**
   - Add globals: `g_fast_forward` (`_Atomic int`), `g_fast_forward_target` (`_Atomic uint64_t`), `g_checkpoint_callback` (PyObject*), `g_checkpoint_interval` (int), `g_last_checkpoint_seq` (uint64_t).
   - Update `start_recording()`: accept `checkpoint_callback` and `checkpoint_interval` kwargs, initialize new globals. When `checkpoint_interval == 0`, skip all checkpoint logic.
   - Update eval hook: add fast-forward check BEFORE `g_recording` check. Add checkpoint trigger after `ringbuf_push` (delta-based: `seq - g_last_checkpoint_seq >= interval`).
   - Update trace function: add fast-forward check BEFORE `g_recording` check.
   - Update `stop_recording()`: reset fast-forward globals. Do NOT kill checkpoints (server needs them).
   - Add `ringbuf_push` guard: check `g_rb.initialized` before access (defense against child use after destroy).
   - Update `ext/recorder.h`: add Phase 2 extern declarations and getter/setter prototypes.

4. **Python models:**
   - `pyttd/models/checkpoints.py` — Peewee `Checkpoint` model with `BooleanField` for `is_alive`, index on `(run_id, sequence_no)`.
   - Update `conftest.py` — add `Checkpoint` to `initialize_schema` calls.
   - `ExecutionFrames` — NO changes (checkpoint_id field removed from plan per review D6).

5. **Python wrapper:**
   - `pyttd/replay.py` — `ReplayController` with `warm_goto_frame` (uses `get_or_none`, returns dict with repr locals), `cold_goto_frame` (pipe RESUME, parse JSON result with `json.loads`, trust child locals over DB), `_warm_fallback` for error cases.
   - `pyttd/recorder.py` — Add `_on_checkpoint` callback (Checkpoint.create in DB, calls `pyttd_native.create_checkpoint`), `kill_checkpoints` method. Update `start()` to accept checkpoint params (pass `checkpoint_callback=None` when `interval == 0`). Update `cleanup()` to call `kill_checkpoints()`.

6. **CLI:**
   - `pyttd/cli.py` — `_cmd_record` uses `PyttdConfig(checkpoint_interval=0)`. Add `_cmd_replay` (warm-only: connects to DB, calls `warm_goto_frame`, prints result). Update dispatch.
   - `pyttd/config.py` — Add `__post_init__` validation (`checkpoint_interval` must be 0 or >= 64).

7. **Tests:**
   - `tests/test_checkpoint.py` — fork lifecycle, pipe IPC, eviction, fork failure recovery, fd cleanup.
   - `tests/test_replay.py` — warm_goto_frame, cold_goto_frame (deterministic scripts only), edge cases (invalid seq, end of recording, __return__/__exception__ keys).
   - Both use `conftest.py` `record_func` fixture (updated with Checkpoint in schema).

8. **Documentation:** Update DESIGN.md with Phase 2 architectural decisions per Verify step 7.

---

### Phase 2 Deep-Dive Review: Fork Semantics, Eviction, and Downstream Integration

Second-pass review focusing on three areas: (1) CPython internals and fork() semantics, (2) exponential thinning eviction algorithm, and (3) downstream Phase 3+ integration. All issues below are additive to the first review above.

---

#### CRITICAL: `g_main_thread_id` not updated in child after fork (D1)

**Location:** `ext/recorder.c` eval hook (line ~515), `ext/checkpoint.c` child post-fork

After `fork()`, the child process has a new OS thread ID (`gettid()` returns a different value). But `g_main_thread_id` still holds the parent's thread ID. The eval hook's thread check:

```c
if (PyThread_get_thread_ident() == g_main_thread_id && ...) {
    // stop-request check
}
```

This comparison will NEVER match in the child (child's thread ident ≠ parent's `g_main_thread_id`). While the stop-request check being skipped is benign (child ignores SIGINT anyway per C3), the deeper problem is that ANY logic gated on `g_main_thread_id` silently breaks in the child.

More critically, if fast-forward mode adds any recording logic gated on `g_main_thread_id` (e.g., future phases), it will silently skip all frames in the child.

**Fix:** In the child, immediately after `PyOS_AfterFork_Child()`:
```c
g_main_thread_id = PyThread_get_thread_ident();
```

This must happen before any eval hook invocation in the child. Add this to the checkpoint child initialization sequence documented in the plan.

#### CRITICAL: Pipe write-side error handling not specified (D2)

**Location:** Pipe command protocol section, `ext/replay.c` (child result writes)

The plan specifies the read side must handle `EINTR` and partial reads, but says nothing about the write side. Issues:

1. **`SIGPIPE`** — If the parent closes its read end of `result_pipe` (e.g., parent crashes or kills child mid-write), the child's `write()` receives `SIGPIPE` (default: terminate process). While child termination is acceptable (it's disposable), unhandled `SIGPIPE` leaves no diagnostic trace.

2. **Short writes** — `write()` to a pipe can return fewer bytes than requested if the pipe buffer is full and a signal interrupts. The child must retry in a loop.

3. **`EINTR`** — `write()` can return -1 with `errno == EINTR` on signal delivery.

**Fix:** In the child, after `PyOS_AfterFork_Child()`:
```c
signal(SIGPIPE, SIG_IGN);  // write() returns EPIPE instead of killing process
```
All `write()` calls in the child must use a `write_all()` helper that handles short writes and `EINTR`:
```c
static ssize_t write_all(int fd, const void *buf, size_t len) {
    size_t written = 0;
    while (written < len) {
        ssize_t n = write(fd, (const char *)buf + written, len - written);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;  // EPIPE or other error
        }
        written += n;
    }
    return (ssize_t)written;
}
```
Similarly, the parent's read side needs a `read_all()` helper (the plan mentions this but doesn't specify the implementation).

#### CRITICAL: `checkpoint_interval == 0` causes undefined behavior (D3)

**Location:** Eval hook checkpoint trigger: `sequence_no % checkpoint_interval == 0`

If the user passes `checkpoint_interval=0` (via `PyttdConfig` or CLI `--checkpoint-interval 0`), the modulo operation is division by zero — undefined behavior in C (typically SIGFPE/crash).

**Fix:** Two-level defense:
1. **Python level:** Validate in `PyttdConfig.__post_init__` — raise `ValueError` if `checkpoint_interval < 0`. Treat `checkpoint_interval == 0` as "checkpoints disabled" (don't pass callback to C).
2. **C level:** In `start_recording()`, validate `checkpoint_interval > 0` if `checkpoint_callback` is not NULL. If callback is NULL, skip all checkpoint logic regardless of interval value. Guard the modulo: `if (g_checkpoint_interval > 0 && g_sequence_counter % g_checkpoint_interval == 0)`.

#### SIGNIFICANT: Exponential thinning algorithm is underspecified (D4)

**Location:** Architecture section (line 126-132), checkpoint_store.c

The plan says:
```
Keep: latest, latest-1000, latest-2000, latest-4000, latest-8000, latest-16000, ...
```

This description has several gaps:

1. **"Latest" is ambiguous** — Does it mean the most recently created checkpoint, or the checkpoint nearest to the current navigation position? These differ after `goto_frame` jumps backward.

2. **No concrete algorithm** — The plan says "retain checkpoints at exponentially-spaced intervals" but doesn't specify: When a new checkpoint is added and the store is full, which specific checkpoint is evicted? The exponential spacing is described as a desired outcome, not as an eviction procedure.

3. **Interaction with consumed checkpoints** — A checkpoint consumed to position 800 (originally at 500) cannot serve targets < 800. The thinning algorithm must consider `current_position`, not `original_sequence_no`, when computing coverage.

4. **Off-by-one at boundaries** — With `checkpoint_interval=1000` and thinning at 1000/2000/4000/..., the first few checkpoints (at seq 0, 1000, 2000) are exactly at the thinning boundaries. As recording progresses, the boundaries shift relative to "latest." This means checkpoints at fixed positions get repeatedly re-evaluated — the algorithm must be idempotent.

**Fix:** Specify a concrete eviction algorithm. Recommended approach (simple, O(N) per eviction):

```
When checkpoint count == MAX_CHECKPOINTS and a new checkpoint needs to be added:
1. Sort checkpoints by original_sequence_no (ascending)
2. Compute the "gap" between each consecutive pair of checkpoints
3. Find the pair with the smallest gap
4. Evict the older checkpoint of that pair (kill child, mark dead)
5. Add the new checkpoint

This naturally produces logarithmic spacing: dense regions get thinned first,
sparse regions are preserved. The newest checkpoint is never evicted.
```

This is simpler than the exponential formula and produces similar coverage. For 32 checkpoints over a long recording, the result approximates exponential spacing without needing to track "current position" in the formula.

Alternative: Use the plan's exponential formula but with concrete implementation:
```
For each checkpoint at position P, compute its "ideal slot" as:
  slot = floor(log2((latest_seq - P) / checkpoint_interval + 1))
If two checkpoints map to the same slot, evict the one farther from the slot's ideal position.
```

Either way, the algorithm MUST be fully specified before implementation.

#### SIGNIFICANT: `CheckpointEntry` struct missing `current_position` field (D5)

**Location:** checkpoint_store.h/c (currently stub)

The plan describes checkpoint consumption (current position diverges from original sequence_no after RESUME), but the `CheckpointEntry` struct in the plan only shows `{child_pid, cmd_pipe_fd, result_pipe_fd, sequence_no, is_alive}`. There is no `current_position` field.

`checkpoint_store_find_nearest(target_seq)` must compare against `current_position`, not `sequence_no`, to avoid sending a child backward (children can only move forward). The plan's `CheckpointEntry` needs an additional field.

**Fix:** Update `CheckpointEntry`:
```c
typedef struct {
    int child_pid;
    int cmd_fd;
    int result_fd;
    uint64_t sequence_no;       // original checkpoint position (immutable)
    uint64_t current_position;  // updated after each RESUME/STEP (monotonically increasing)
    int is_alive;
} CheckpointEntry;
```

`checkpoint_store_find_nearest(target_seq)` must find the entry with the largest `current_position <= target_seq` (not `sequence_no`). After a successful RESUME/STEP, the caller updates `current_position` via:
```c
void checkpoint_store_update_position(int index, uint64_t new_position);
```

#### SIGNIFICANT: `ExecutionFrames.checkpoint_id` is unused by any downstream phase (D6)

**Location:** Phase 2 frames.py update, Phase 3 session.py, Phase 4 replay

Analysis of Phases 3-7 shows that NO downstream code reads `ExecutionFrames.checkpoint_id`. The field was intended as a "convenience denormalization" but:
- Phase 3 `session.py` uses `Checkpoint` table directly (queries by `run_id` and `sequence_no`)
- Phase 4 `goto_frame` uses `ReplayController.get_nearest_checkpoint()` which queries `Checkpoint` table
- The `checkpoint_id` field is nullable, only populated for checkpoint frames, and the UPDATE that sets it (in `_on_checkpoint`) may match zero rows (frame not yet flushed)

The field adds complexity (schema migration concern, UPDATE logic) with zero downstream consumers.

**Recommendation:** Remove `checkpoint_id` from `ExecutionFrames`. The `Checkpoint` table already records `sequence_no`, which is the primary lookup key. If a future phase needs to identify which frame triggered a checkpoint, it can JOIN `Checkpoint.sequence_no = ExecutionFrames.sequence_no WHERE Checkpoint.run_id = ExecutionFrames.run_id`.

If kept, acknowledge in the plan that it's decorative and the UPDATE may be a no-op.

#### SIGNIFICANT: `recorder.stop()` vs `kill_all_checkpoints()` lifecycle conflict (D7)

**Location:** S4 in first review, `recorder.py`, Phase 3 `session.py`

The first review (S4) recommends killing checkpoints in `recorder.stop()`. But this conflicts with cold replay testing:
- Test flow: `recorder.start()` → record events (creates checkpoints) → `recorder.stop()` → `ReplayController.goto_frame()` (needs live checkpoints)
- If `stop()` kills checkpoints, cold replay is impossible after recording ends

Phase 3's server mode has the same pattern: recording stops, then the user navigates (requiring live checkpoints). Killing checkpoints on `stop()` would break the core use case.

**Fix:** Revise S4's recommendation. `recorder.stop()` should NOT kill checkpoints. Instead:
1. `recorder.stop()` — stops recording (flush thread, eval hook), but leaves checkpoint children alive
2. `recorder.kill_checkpoints()` — new method, kills all checkpoint children (called during session shutdown)
3. `recorder.cleanup()` — closes DB (already exists), should also call `kill_checkpoints()` if not already called
4. `_cmd_record` in CLI mode: call `recorder.stop()`, then `recorder.kill_checkpoints()`, then `recorder.cleanup()` (CLI doesn't need cold replay after recording)
5. Server mode: call `recorder.stop()` after recording, keep checkpoints alive for navigation, call `kill_checkpoints()` + `cleanup()` on disconnect

#### SIGNIFICANT: `max_checkpoints` should be hardcoded in C (D8)

**Location:** S1 in first review

Reinforcing S1's option (b): `max_checkpoints` should be `#define MAX_CHECKPOINTS 32` in `checkpoint_store.h`, not a runtime parameter. Reasons:
1. The C array is statically sized — a runtime parameter would require dynamic allocation (`malloc`), adding OOM error paths
2. 32 is a reasonable fixed limit (covers ~32,000 frames at interval=1000, with thinning covering much more)
3. No downstream consumer needs to configure this — `PyttdConfig.max_checkpoints` can be removed or kept as a Python-only validation that warns if it exceeds 32
4. Simplifies `checkpoint_store_init()` — no parameter needed

#### SIGNIFICANT: `_warm_fallback` locals type inconsistency is the highest-impact integration bug (D9)

**Location:** S6 in first review, `replay.py`

Reiterating S6 with additional analysis: this is the single most likely bug to surface during Phase 3 integration. The cold path returns `locals` as a parsed `dict`, the warm path returns it as a raw JSON `str`. Phase 3's `session.py` will pass this to DAP's `variablesRequest`, which iterates `locals.items()`. On the warm path, `str.items()` raises `AttributeError`.

The fix is simple (already specified in S6: `json.loads(frame.locals_snapshot)`), but its impact is disproportionate — every warm navigation in Phase 3+ will crash without this fix.

**Priority:** Fix MUST be applied during Phase 2 implementation, not deferred to Phase 3.

#### MINOR: `PyOS_AfterFork_Child()` does NOT reset PEP 523 eval hook or trace function (D10)

**Location:** checkpoint.c step 3 (Child)

Confirmed via CPython source: `PyOS_AfterFork_Child()` calls `_PyInterpreterState_DeleteExceptMain()` and reinitializes the GIL, but does NOT reset:
- The PEP 523 frame eval hook (`interp->eval_frame`)
- The C-level trace function (`tstate->c_tracefunc`, `tstate->c_traceobj`)

Both survive fork via COW. This is **correct behavior for pyttd** — the child needs the eval hook and trace function to re-execute user code during fast-forward. But it should be explicitly documented as a relied-upon invariant, since a future CPython version could change this behavior.

**Fix:** Add a comment in the child post-fork code:
```c
/* PyOS_AfterFork_Child() reinitializes GIL and thread state but does NOT
 * reset the PEP 523 eval hook or trace function. We rely on both surviving
 * fork (via COW) for fast-forward re-execution. */
```

#### MINOR: `tstate` and `iframe` pointer validity after fork (D11)

**Location:** checkpoint.c child execution

After `fork()`, the child has a copy-on-write clone of the parent's virtual address space. Pointers to `PyThreadState`, `_PyInterpreterFrame`, and all Python objects are valid in the child because:
- Virtual addresses are identical (COW maps same virtual → different physical on write)
- The child is single-threaded (no concurrent modification)
- `PyOS_AfterFork_Child()` patches thread state to reflect the single-thread reality

However, the child must NOT call `Py_DECREF` on any object shared with the parent (would trigger COW write, wasting memory, and reference counts are meaningless in the child). During fast-forward, the eval hook calls `Py_DECREF` on `PyFrame_GetLocals()` results — in fast-forward mode, this should be skipped (don't call `PyFrame_GetLocals()` at all when `g_fast_forward` is set).

**Fix:** Already implied by the plan ("do NOT serialize locals" during fast-forward), but make it explicit: the fast-forward mode must skip ALL `Py_INCREF`/`Py_DECREF` calls on temporary objects. The eval hook and trace function's fast-forward branches should be minimal: increment counter, check target, return.

#### MINOR: Ring buffer cleanup in child (D12)

**Location:** C2 in first review, `ext/ringbuf.c`

The child inherits the ring buffer (`g_rb`) but doesn't need it (no flush thread, no recording). `ringbuf_destroy()` in the child is safe (COW — the parent's ring buffer is unaffected) and frees ~1MB per child. With 32 children, that's ~32MB saved.

However, `ringbuf_destroy()` must be called BEFORE any eval hook invocation in the child (the eval hook calls `ringbuf_push()`, which would write to the ring buffer, triggering COW allocation). In fast-forward mode, `ringbuf_push()` is skipped, so this is only a concern if there's a code path between `PyOS_AfterFork_Child()` and the fast-forward flag being set where the eval hook could fire.

**Fix:** Set `g_fast_forward = 1` BEFORE `PyOS_AfterFork_Child()` (while still in the parent's fork-return path — the child inherits this value). Then call `ringbuf_destroy()` after `PyOS_AfterFork_Child()`. This ensures no ring buffer writes occur in the child.

Wait — `g_fast_forward` can't be set before fork (parent would also see it). Instead:
1. In child, immediately after fork returns 0: set `g_fast_forward = 1` and `g_recording = 0` (prevent any recording before PyOS_AfterFork_Child)
2. Call `PyOS_AfterFork_Child()`
3. Call `ringbuf_destroy()`
4. `g_recording` will be re-set when RESUME arrives

Actually, the child blocks on `read(cmd_pipe)` immediately after fork setup — no Python code executes (no eval hook fires) until RESUME arrives. So `ringbuf_destroy()` can safely be called anytime during child initialization, before the `read()` block.

#### MINOR: Eviction must send `DIE` to the evicted child (D13)

**Location:** checkpoint_store.c eviction logic

When the thinning algorithm decides to evict a checkpoint, it must:
1. Send `DIE` command via `write(cmd_fd, ...)` to the child
2. `waitpid(child_pid, ...)` to reap the zombie (or use `WNOHANG` + periodic reaping)
3. Close `cmd_fd` and `result_fd` pipe ends
4. Mark entry as `is_alive = 0`

The plan mentions `kill_all_checkpoints` sends DIE, but doesn't explicitly describe per-checkpoint eviction cleanup. Without `waitpid`, evicted children become zombies.

**Fix:** Add `checkpoint_store_evict(int index)` function:
```c
void checkpoint_store_evict(int index) {
    CheckpointEntry *e = &g_checkpoints[index];
    if (!e->is_alive) return;
    uint8_t cmd[9] = {0xFF, 0,0,0,0,0,0,0,0};  // DIE
    write(e->cmd_fd, cmd, 9);  // best-effort
    close(e->cmd_fd);
    close(e->result_fd);
    waitpid(e->child_pid, NULL, 0);
    e->is_alive = 0;
}
```

#### SIGNIFICANT: Phase 3 race condition in checkpoint_store (D15)

**Location:** checkpoint_store.c, Phase 3 `session.py` / `server.py`

In Phase 3, the server has two Python threads: the RPC thread (handles navigation requests) and the recording thread (runs user script, creates checkpoints). Both hold the GIL when calling into C, but `restore_checkpoint` releases the GIL during blocking pipe I/O:

1. RPC thread (GIL held): `find_nearest(target)` → gets index `i`, copies `cmd_fd`/`result_fd`
2. RPC thread (GIL released): `write(cmd_fd, RESUME)` + `read(result_fd)` — blocking I/O
3. Recording thread (GIL acquired during step 2): eval hook fires, creates checkpoint → `checkpoint_store_add` triggers eviction → array compacted → index `i` is now stale

If the RPC thread saved the raw index `i` and tries to update `entries[i].current_position` after re-acquiring the GIL, it writes to the wrong entry.

**Fix:** Before releasing the GIL in step 2, copy `cmd_fd`, `result_fd`, and `child_pid` out of the entry. After re-acquiring the GIL, look up the entry by `child_pid` (stable identifier) to update `current_position`. This is tolerant of array compaction during the GIL-released window:
```c
/* Before GIL release: */
int cmd_fd = entries[idx].cmd_fd;
int result_fd = entries[idx].result_fd;
int child_pid = entries[idx].child_pid;
/* GIL released: blocking I/O on cmd_fd/result_fd */
/* GIL re-acquired: */
int new_idx = checkpoint_store_find_by_pid(child_pid);
if (new_idx >= 0) entries[new_idx].current_position = new_pos;
```

This is a Phase 3 issue but must be designed into the Phase 2 API.

#### SIGNIFICANT: `g_flush_thread_created` must be cleared in child (D16)

**Location:** `ext/recorder.c`, checkpoint.c child initialization

The child inherits `g_flush_thread_created = 1` from the parent. If any code path accidentally calls `pyttd_stop_recording()` in the child (e.g., via `atexit` handler or cleanup on exception), it would call `pthread_join(g_flush_thread, NULL)` on a thread ID that doesn't exist in the child (threads don't survive `fork()`). This is undefined behavior.

**Fix:** Add to child initialization sequence (after `PyOS_AfterFork_Child()`):
```c
g_recording = 0;
g_flush_thread_created = 0;
```

This prevents `stop_recording()` from touching flush thread state in the child.

#### SIGNIFICANT: `PyOS_AfterFork_Child()` deadlock mitigation via result pipe timeout (D17)

**Location:** `ext/replay.c` parent side, `ext/checkpoint.c` child side

If `PyOS_AfterFork_Child()` deadlocks in the child (due to an internal CPython mutex locked at fork time, or a third-party C extension's `atfork` handler), the child never reaches the `read(cmd_pipe)` point. The parent's subsequent `RESUME` write succeeds (buffered in pipe), but the `read(result_pipe)` blocks forever.

**Fix:** Use `poll()` with a timeout before `read()` on the result pipe:
```c
struct pollfd pfd = { .fd = result_pipe_fd, .events = POLLIN };
int rc = poll(&pfd, 1, 5000);  /* 5-second timeout */
if (rc <= 0) {
    kill(child_pid, SIGKILL);
    waitpid(child_pid, NULL, 0);
    checkpoint_store_remove(index);
    PyErr_SetString(PyExc_RuntimeError, "checkpoint child timed out");
    return NULL;  /* caller falls back to warm */
}
/* read() now guaranteed to not block indefinitely */
```

A 5-second timeout is generous — a healthy checkpoint responds in <300ms even for large fast-forwards.

#### SIGNIFICANT: Concrete eviction algorithm specification (D18)

**Location:** D4 addendum — checkpoint_store.c

Expanding D4 with a concrete recommended algorithm. The plan's stated pattern ("latest, latest-1000, latest-2000, latest-4000, ...") maps naturally to an **ideal-position distance** approach:

```c
int checkpoint_to_evict(CheckpointEntry *entries, int count,
                        uint64_t latest_seq, uint64_t interval) {
    /* Generate ideal positions: latest, latest-interval, latest-2*interval,
     * latest-4*interval, ..., 0 */
    uint64_t ideal[64];
    int n_ideal = 0;
    ideal[n_ideal++] = latest_seq;
    uint64_t offset = interval;
    while (latest_seq >= offset && n_ideal < 62) {
        ideal[n_ideal++] = latest_seq - offset;
        offset *= 2;
    }
    ideal[n_ideal++] = 0;  /* always keep origin */

    /* For each checkpoint, compute min distance to any ideal position.
     * The checkpoint furthest from all ideals is evicted. */
    int worst_idx = -1;
    uint64_t worst_dist = 0;
    for (int i = 0; i < count; i++) {
        uint64_t seq = entries[i].original_seq;
        /* Penalize consumed checkpoints: use current_position instead */
        if (entries[i].current_position > seq)
            seq = entries[i].current_position;
        uint64_t min_dist = UINT64_MAX;
        for (int j = 0; j < n_ideal; j++) {
            uint64_t d = (seq > ideal[j]) ? seq - ideal[j] : ideal[j] - seq;
            if (d < min_dist) min_dist = d;
        }
        if (min_dist > worst_dist) {
            worst_dist = min_dist;
            worst_idx = i;
        }
    }
    return worst_idx;
}
```

Properties:
- Naturally produces the plan's exponential spacing pattern
- O(N * log(N/interval)) per eviction — trivial for N=32
- Consumed checkpoints are penalized (their effective position shifts forward)
- Interval-agnostic (works for any `checkpoint_interval` value)
- Idempotent — re-running on the same set produces the same result

#### MINOR: `STEP` command's `delta` semantics need clarification (D14)

**Location:** Pipe command protocol, opcodes

`STEP` with `payload = delta` means "advance delta events from current position." But:
1. Is `delta == 0` valid? (no-op — re-serialize current state without advancing)
2. What if the script ends before `delta` events? (same issue as C6 for RESUME)
3. Can the warm child receive multiple STEPs without intervening result reads? (no — pipe protocol is synchronous: command → result → command → ...)

**Fix:** Specify:
- `delta == 0`: re-serialize current state (useful for refreshing locals after external state change — though irrelevant in Phase 2)
- `delta > 0`: fast-forward `delta` events from `current_position`, same early-exit handling as C6
- Protocol is strictly request-response: parent sends one command, reads one result, repeat

#### MINOR: Child must validate backward RESUME (D19)

**Location:** Pipe command handling in child

If the parent sends `RESUME(target_seq)` where `target_seq <= current_position`, the child cannot serve it (it can only move forward). The child must detect this and send an error response rather than silently entering fast-forward with an unreachable target.

**Fix:** In the child's command handler:
```c
if (opcode == 0x01 && target_seq <= current_position) {
    write_error_result(result_fd, "backward_navigation_unsupported");
    /* Continue waiting for next command */
}
```

The parent should check `current_position` before sending RESUME (via the C store), but the child-side check is defense-in-depth.

#### MINOR: `kill_all_checkpoints` should batch DIE then batch waitpid (D20)

**Location:** checkpoint_store.c

Sending DIE sequentially and waiting for each child before moving to the next takes O(N * child_exit_time). Batching is much faster: send all DIE commands first (microseconds each), then reap all children (most have already exited by the time we start reaping):

```c
/* Phase 1: Send DIE to all, close pipe fds */
for (int i = 0; i < count; i++) {
    if (entries[i].state != ALIVE) continue;
    uint8_t die[9] = {0xFF};
    write(entries[i].cmd_fd, die, 9);
    close(entries[i].cmd_fd);
    close(entries[i].result_fd);
}
/* Phase 2: Reap all (most already exited) */
for (int i = 0; i < count; i++) {
    if (entries[i].state != ALIVE) continue;
    if (waitpid(entries[i].child_pid, NULL, WNOHANG) == 0) {
        usleep(10000);  /* 10ms grace */
        if (waitpid(entries[i].child_pid, NULL, WNOHANG) == 0) {
            kill(entries[i].child_pid, SIGKILL);
            waitpid(entries[i].child_pid, NULL, 0);
        }
    }
    entries[i].state = DEAD;
}
```

#### MINOR: Checkpoint callback failure is non-fatal (D21)

**Location:** checkpoint.c parent path after fork

If the Python checkpoint callback (`_on_checkpoint`) raises an exception (e.g., DB insert fails — disk full, table missing), the C code must not abort. The C checkpoint store is the source of truth for live checkpoint management; the DB is for persistence/diagnostics only.

**Fix:** Handle callback failure gracefully:
```c
PyObject *result = PyObject_Call(callback, args, NULL);
if (!result) {
    PyErr_WriteUnraisable(callback);
    PyErr_Clear();
    /* C store has the entry — continue without DB row */
}
```

---

#### Summary of All Phase 2 Review Issues

| ID | Severity | Title | Location |
|----|----------|-------|----------|
| **First review** | | | |
| C1 | Critical | GIL deadlock in pre-fork sync | checkpoint.c |
| C2 | Critical | Child must close inherited fds | checkpoint.c |
| C3 | Critical | Child signal handling | checkpoint.c |
| C4 | Critical | Eval hook naming ambiguity | checkpoint.c, recorder.c |
| C5 | Critical | Trace function blocking + DIE | replay.c |
| C6 | Critical | Fast-forward target unreachable | replay.c |
| S1 | Significant | max_checkpoints not passed to C | start_recording |
| S2 | Significant | ReplayController DB vs C store | replay.py |
| S3 | Significant | CLI verify steps incorrect | Verify section |
| S4 | Significant | _cmd_record must kill children | cli.py |
| S5 | Significant | Thread safety check all platforms | checkpoint.c |
| S6 | Significant | _warm_fallback locals type | replay.py |
| S7 | Significant | Fast-forward globals lifecycle | recorder.c |
| S8 | Significant | Result pipe needs envelope | pipe protocol |
| S9 | Significant | is_alive should be BooleanField | checkpoints.py |
| M1-M9 | Minor | Various | Multiple |
| **Deep-dive review** | | | |
| D1 | Critical | g_main_thread_id stale in child | recorder.c, checkpoint.c |
| D2 | Critical | Pipe write-side error handling | replay.c |
| D3 | Critical | checkpoint_interval==0 div-by-zero | recorder.c |
| D4 | Significant | Eviction algorithm underspecified | checkpoint_store.c |
| D5 | Significant | CheckpointEntry missing current_position | checkpoint_store.h |
| D6 | Significant | checkpoint_id field unused downstream | frames.py |
| D7 | Significant | stop() vs kill_checkpoints() lifecycle | recorder.py |
| D8 | Significant | max_checkpoints should be hardcoded | checkpoint_store.h |
| D9 | Significant | _warm_fallback locals type (reiterated) | replay.py |
| D10 | Minor | PyOS_AfterFork_Child eval hook survival | checkpoint.c |
| D11 | Minor | tstate/iframe COW pointer validity | checkpoint.c |
| D12 | Minor | Ring buffer cleanup in child | checkpoint.c |
| D13 | Minor | Eviction must DIE + waitpid | checkpoint_store.c |
| D14 | Minor | STEP delta==0 and edge cases | pipe protocol |
| D15 | Significant | Phase 3 race: stale index after compaction | checkpoint_store.c |
| D16 | Significant | g_flush_thread_created stale in child | recorder.c, checkpoint.c |
| D17 | Significant | PyOS_AfterFork_Child deadlock mitigation | replay.c |
| D18 | Significant | Concrete eviction algorithm (ideal-position) | checkpoint_store.c |
| D19 | Minor | Child must validate backward RESUME | checkpoint.c |
| D20 | Minor | Batch DIE then batch waitpid | checkpoint_store.c |
| D21 | Minor | Callback failure is non-fatal | checkpoint.c |

---

### Phase 2 Third-Pass Review: Fast-Forward Correctness, Integration Surgery, and Child Safety

Third-pass review focusing on three areas not adequately covered by the first two passes: (1) formal proof of fast-forward sequence counter correctness, (2) exact line-level integration into recorder.c, and (3) child process safety edge cases. All issues below use the prefix FF- (fast-forward), N- (integration), or E- (child safety).

---

#### CRITICAL: `g_recording=0` in child blocks both eval hook and trace function (N8+N9)

**Location:** `ext/recorder.c` lines 431, 316

D16 recommends setting `g_recording = 0` in the child to prevent `stop_recording()` issues. But the eval hook's first check is:
```c
if (!atomic_load_explicit(&g_recording, memory_order_relaxed) || g_inside_repr) {
    return g_original_eval(tstate, iframe, throwflag);
}
```
And the trace function's first check is:
```c
if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) return 0;
```

If `g_recording == 0`, BOTH functions bail out immediately — the eval hook calls `g_original_eval` without incrementing `g_sequence_counter` or `g_call_depth`, and the trace function returns without incrementing the counter for `line`/`return`/`exception` events. **Fast-forward produces zero counter increments. The child re-executes with no instrumentation at all.**

This is the single most important integration issue. Three solutions:

**Solution A:** Don't clear `g_recording` in child. Risk: ring buffer use-after-free if D12 (destroy ring buffer) is applied, because the eval hook calls `ringbuf_push()` before `g_fast_forward` can be checked.

**Solution B (RECOMMENDED):** Check `g_fast_forward` BEFORE `g_recording` in both functions:
```c
/* Eval hook entry: */
if (g_fast_forward) {
    return pyttd_eval_hook_fast_forward(tstate, iframe, throwflag);
}
if (!atomic_load_explicit(&g_recording, memory_order_relaxed) || g_inside_repr) {
    return g_original_eval(tstate, iframe, throwflag);
}
/* ... normal recording path ... */

/* Trace function entry: */
if (g_fast_forward) {
    return pyttd_trace_func_fast_forward(frame, what, arg);
}
if (!atomic_load_explicit(&g_recording, memory_order_relaxed)) return 0;
/* ... normal recording path ... */
```

This cleanly separates fast-forward from recording. The child sets `g_recording = 0` (safe for D16) and `g_fast_forward = 1` (when RESUME arrives). The `g_fast_forward` check comes first, so `g_recording = 0` is never reached in fast-forward mode.

**Solution C:** Combine flags: `if (!(g_recording || g_fast_forward) || g_inside_repr)`. Messier than B — mixes paths with scattered `if (g_fast_forward)` branches. Solution B is cleaner.

**Child initialization sequence must be:**
1. `PyOS_AfterFork_Child()` — reinitializes GIL
2. `g_recording = 0` (D16)
3. `g_flush_thread_created = 0` (D16)
4. `g_fast_forward = 0` (not yet — set when RESUME arrives)
5. Block on `read(cmd_pipe)`
6. On RESUME: set `g_fast_forward = 1`, `g_fast_forward_target = target_seq`

No eval hook fires between steps 1-5 because no Python code executes (the child is in C code, blocking on `read()`). Step 6 sets `g_fast_forward` before returning to the eval hook (which then continues execution).

#### CRITICAL: `serialize_locals()` can trigger `__del__` outside `g_inside_repr` guard — deterministic divergence (FF-1)

**Location:** `ext/recorder.c` lines 242-306 (`serialize_locals`), line 193 (`g_inside_repr` scope)

During recording, the trace function calls `serialize_locals()`, which calls `PyFrame_GetLocals()` and later `Py_DECREF(locals)`. If these operations trigger `__del__` destructors on local variables, those destructors execute arbitrary Python code. New frames created by `__del__` go through the eval hook.

The `g_inside_repr` flag (set in `serialize_one_local` around `PyObject_Repr()`) suppresses recording of `repr()`-triggered frames. But `g_inside_repr` is NOT set during:
- `PyFrame_GetLocals()` (line 242)
- `Py_DECREF(locals)` (line 294)
- `PyMapping_Items(locals)` / `Py_DECREF(items)` (lines 266-278)

If `__del__` fires during any of these and creates frames in non-ignored files, those frames are recorded with `g_sequence_counter++`. During fast-forward, `serialize_locals()` is skipped entirely — these phantom frames never exist. **The sequence counter diverges even for fully deterministic programs.**

This is distinct from the known Phase 4 non-determinism (I/O, random) — this is a recorder-induced divergence.

**Fix:** Widen the reentrancy guard to cover the entire `serialize_locals()` call:
```c
/* In trace function, before calling serialize_locals: */
g_inside_repr = 1;  /* or rename to g_inside_serialize */
const char *locals_json = serialize_locals(...);
g_inside_repr = 0;
```

This ensures any `__del__`-triggered frames during serialization are suppressed in both recording and fast-forward (zero events from either path).

**Impact:** Without this fix, any code with `__del__` methods on local variables could produce unreproducible sequence numbers. The fix is two lines per call site (three call sites in the trace function: LINE, RETURN, EXCEPTION).

#### CRITICAL: Pause ack condvar signal can be lost — deadlock on every checkpoint (N12)

**Location:** `checkpoint.c` (planned), `ext/recorder.c` flush thread

The C1 fix specifies: parent signals `g_flush_cond`, then waits on `pause_ack_cv`. But the standard condvar race applies:

1. Parent: set `pause_requested = 1`, signal `g_flush_cond`
2. Flush thread: wakes, calls `flush_batch()`, finishes, checks `pause_requested`
3. Flush thread: signals `pause_ack_cv` **← parent isn't waiting yet**
4. Flush thread: blocks on `resume_cv`
5. Parent: starts waiting on `pause_ack_cv` **← signal already lost**
6. **DEADLOCK** (or 1-second timeout, degrading every checkpoint to worst-case latency)

**Fix:** Use a persistent `g_pause_acked` flag alongside the condvar:
```c
static _Atomic int g_pause_acked = 0;

/* Parent: */
atomic_store(&g_pause_acked, 0);
atomic_store(&g_pause_requested, 1);
pthread_mutex_lock(&g_flush_mutex);
pthread_cond_signal(&g_flush_cond);
while (!atomic_load(&g_pause_acked)) {
    pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
    if (rc == ETIMEDOUT) break;
}
pthread_mutex_unlock(&g_flush_mutex);

/* Flush thread: */
if (atomic_load(&g_pause_requested)) {
    pthread_mutex_lock(&g_flush_mutex);
    atomic_store(&g_pause_acked, 1);
    pthread_cond_signal(&g_pause_ack_cv);
    while (atomic_load(&g_pause_requested)) {
        pthread_cond_wait(&g_resume_cv, &g_flush_mutex);
    }
    pthread_mutex_unlock(&g_flush_mutex);
}
```

The `while` loop on `g_pause_acked` handles both the lost-signal case AND spurious wakeups. The `while` loop on `g_pause_requested` in the flush thread handles spurious resume signals.

Also: the parent must **unlock `g_flush_mutex` before `fork()`**. Otherwise the child inherits a locked mutex. After fork, the parent re-locks the mutex to signal `resume_cv`.

#### CRITICAL: Pre-fork condvar protocol fully unspecified — mutex ownership, spurious wakeups, fork state (E5)

**Location:** `checkpoint.c` (planned), `ext/recorder.c` lines 79-80

Expanding N12: the plan mentions `pause_ack` and `resume_cv` but never specifies:
- Which mutex protects them (answer: `g_flush_mutex`, shared with `g_flush_cond`)
- Where in the flush thread loop the pause check goes (answer: after `flush_batch()`, outside the existing mutex hold)
- That `pthread_cond_wait` MUST be in a `while` loop for spurious wakeups
- That the parent must unlock `g_flush_mutex` before `fork()` (otherwise child inherits locked mutex)
- That `g_flush_mutex` and `g_flush_cond` must be reinitialized in the child (E2)

**Fix:** Complete modified flush thread loop:
```c
while (!g_flush_stop) {
    /* Step 1-4: Timed wait (unchanged) */
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_nsec += (long)g_flush_interval_ms * 1000000L;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
    pthread_mutex_lock(&g_flush_mutex);
    pthread_cond_timedwait(&g_flush_cond, &g_flush_mutex, &ts);
    pthread_mutex_unlock(&g_flush_mutex);

    /* Step 5: Check stop */
    if (atomic_load(&g_flush_stop)) break;

    /* Step 6: Flush batch (acquires/releases GIL internally) */
    flush_batch();

    /* Step 7: Phase 2 — pause check (AFTER flush, GIL released, all Python done) */
    if (atomic_load_explicit(&g_pause_requested, memory_order_acquire)) {
        pthread_mutex_lock(&g_flush_mutex);
        atomic_store(&g_pause_acked, 1);
        pthread_cond_signal(&g_pause_ack_cv);
        while (atomic_load(&g_pause_requested)) {
            pthread_cond_wait(&g_resume_cv, &g_flush_mutex);
        }
        pthread_mutex_unlock(&g_flush_mutex);
    }
}
```

Parent side in `checkpoint_do_fork()`:
```c
atomic_store(&g_pause_acked, 0);
atomic_store(&g_pause_requested, 1);
pthread_mutex_lock(&g_flush_mutex);
pthread_cond_signal(&g_flush_cond);  /* wake flush thread */
PyThreadState *saved = PyEval_SaveThread();  /* release GIL */
while (!atomic_load(&g_pause_acked)) {
    int rc = pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
    if (rc == ETIMEDOUT) { /* skip checkpoint */ break; }
}
pthread_mutex_unlock(&g_flush_mutex);  /* unlock BEFORE fork */
pid_t pid = fork();
/* Parent: */
PyEval_RestoreThread(saved);  /* re-acquire GIL */
pthread_mutex_lock(&g_flush_mutex);
atomic_store(&g_pause_requested, 0);
pthread_cond_signal(&g_resume_cv);
pthread_mutex_unlock(&g_flush_mutex);
```

Child must reinitialize:
```c
g_flush_mutex = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
g_flush_cond = (pthread_cond_t)PTHREAD_COND_INITIALIZER;
/* g_pause_ack_cv and g_resume_cv also reinitialized */
```

---

#### SIGNIFICANT: Child inherits coverage.py / third-party trace functions (E10)

**Location:** `checkpoint.c` child initialization

When `pytest --cov` is used, coverage.py installs a trace function via `sys.settrace()`. After `fork()`, the child inherits this trace function (`tstate->c_tracefunc`). During fast-forward:
- pyttd's eval hook saves/restores `tstate->c_tracefunc` around each eval call (sections G/K)
- The saved trace is coverage.py's trace function
- Each frame restore calls `PyEval_SetTrace(coverage_trace, ...)` — coverage.py's trace fires during fast-forward
- This causes: COW page writes for coverage data structures, memory bloat, potential crashes (coverage.py's internal state is not fork-safe)

**Fix:** In child initialization, before any eval hook invocation:
```c
PyEval_SetTrace(NULL, NULL);  /* clear inherited third-party trace */
```
This ensures the eval hook's `saved_trace` (section G) picks up NULL as the previous trace, not coverage.py's function. When fast-forward begins, only pyttd's trace function is installed.

For test infrastructure, document that `pytest --no-cov` should be used for fork-based checkpoint tests, or add a `conftest.py` marker.

#### SIGNIFICANT: Child inherits `atexit` handlers — unhandled exception during fast-forward triggers parent's cleanup (E6)

**Location:** `checkpoint.c` child, fast-forward re-execution

The child uses `_exit(0)` on DIE, skipping atexit handlers. But if the child's fast-forward code raises an unhandled exception that propagates beyond all Python frames, Python's normal shutdown sequence begins: `atexit` handlers (registered by the parent process — stdlib modules like `logging`, `tempfile`, user code) run in the child. This could:
- Delete the parent's temp files
- Close shared sockets
- Write to shared log files
- Corrupt any mutable shared state

**Fix:** Clear atexit handlers in child immediately after `PyOS_AfterFork_Child()`:
```c
PyObject *atexit_mod = PyImport_ImportModule("atexit");
if (atexit_mod) {
    PyObject *r = PyObject_CallMethod(atexit_mod, "_clear", NULL);
    Py_XDECREF(r);
    if (PyErr_Occurred()) PyErr_Clear();
    Py_DECREF(atexit_mod);
}
```

Also set `sys.excepthook = lambda *a: None` to suppress default exception printing on stderr (which the child inherits).

#### SIGNIFICANT: Peewee's `threading.Lock` in child may be in locked state (E1)

**Location:** `pyttd/models/base.py`, peewee internals

Peewee's `Database` object has `self._lock = threading.Lock()`. The C2 fix says to close the DB in the child, but `db.close()` acquires `self._lock`. If the flush thread was holding this lock at fork time (e.g., mid-`autoconnect`), the child's `db.close()` deadlocks.

The pre-fork sync ensures the flush thread has completed all Python operations before acknowledging pause, so the lock SHOULD be released. But this is timing-dependent.

**Fix:** In the child, close the SQLite fd at the C level (bypassing Peewee entirely):
```c
/* Store parent's SQLite fd before fork (retrieve via sqlite3_db_handle) */
/* In child: close(sqlite_fd); — no Peewee lock needed */
```
Or reinitialize the lock: `db._lock = threading.Lock()` via Python from C.

#### SIGNIFICANT: `g_inside_repr` inherited by child — latent divergence bug (E9)

**Location:** `ext/recorder.c` line 51

If `g_inside_repr == 1` at fork time (theoretically impossible given current checkpoint trigger location, but a latent risk), the child's eval hook skips ALL recording/counting. The fast-forward check (Solution B from N8+N9) must come BEFORE the `g_inside_repr` check to prevent this.

**Fix:** Reset `g_inside_repr = 0` in child post-fork initialization. Also, Solution B's structure naturally avoids this: `g_fast_forward` is checked first, `g_inside_repr` is only in the normal recording path.

#### SIGNIFICANT: Checkpoint trigger insertion point must be between F and G (N2/N3)

**Location:** `ext/recorder.c` lines 497-500

The checkpoint trigger inserts between step F (ringbuf_push, line 497) and step G (install trace, line 500):
```c
ringbuf_push(&call_event);         // line 496
g_frame_count++;                   // line 497

/* Phase 2: Checkpoint trigger */
if (g_checkpoint_interval > 0 &&
    g_checkpoint_callback != NULL &&
    call_event.sequence_no > 0 &&
    call_event.sequence_no % g_checkpoint_interval == 0) {
    checkpoint_do_fork(call_event.sequence_no, g_checkpoint_callback);
}

/* Save current trace function, install ours */   // line 500
Py_tracefunc saved_trace = tstate->c_tracefunc;
```

At this point, `saved_trace` and `saved_traceobj` are not yet initialized (they're declared at line 500). After fork, the child inherits the stack with these as uninitialized locals — this is safe because the child continues to line 500, which initializes them by reading `tstate->c_tracefunc`.

`checkpoint_do_fork()` internally releases/reacquires the GIL (per C1 fix). Between GIL release and reacquire, other Python threads can run (only the flush thread in Phase 1, which is paused). After `checkpoint_do_fork` returns, the eval hook's `tstate` is still valid (same thread state restored).

#### SIGNIFICANT: `g_sequence_counter` and `g_call_depth` must lose `static` linkage (N10)

**Location:** `ext/recorder.c` lines 44-45

Currently:
```c
static uint64_t g_sequence_counter = 0;
static int g_call_depth = -1;
```

`checkpoint.c` and `replay.c` need to read/write `g_sequence_counter` (for fast-forward target detection at the point where the child reports state) and `g_call_depth` (for state serialization at target).

**Fix (recommended):** Keep variables `static` in recorder.c but expose via setter/getter functions:
```c
/* recorder.h */
void recorder_set_fast_forward(int enabled, uint64_t target_seq);
uint64_t recorder_get_sequence_counter(void);
int recorder_get_call_depth(void);
```

This maintains encapsulation. `checkpoint.c` calls `recorder_set_fast_forward(1, target_seq)` when RESUME arrives. The eval hook and trace function read `g_fast_forward` directly (defined in recorder.c).

---

#### MODERATE: SQLite WAL mmap inherited by child — corruption risk (E7)

**Location:** `checkpoint.c` child, SQLite internals

SQLite WAL mode mmaps the `-shm` file. After fork, the child inherits this mmap. If the child writes to it (e.g., during a stale `close()` that touches the WAL index), it corrupts the parent's view (the mmap is shared memory, not COW).

**Fix:** Close the DB fd at the C level (raw `close()`) BEFORE any SQLite operation in the child. Do NOT call `sqlite3_close()` or Peewee's `db.close()` — both may touch the WAL index. Just `close(fd)` to release the kernel reference.

#### MODERATE: Child memory growth during fast-forward undocumented (E12)

**Location:** `checkpoint.c` child, fast-forward re-execution

During fast-forward, the child re-executes user code. Each Python object allocation triggers COW page faults. For 10,000 frames with ~5 locals each at ~100 bytes: ~5MB per child. With 32 children and varying fast-forward distances: ~50-200MB total.

Mitigated by exponential thinning (most children fast-forward short distances) and the fact that far-back checkpoints are few (O(log N)).

**Fix:** Document in DESIGN.md. No code change needed for Phase 2.

#### MODERATE: Fast-forward COW overhead from `PyEval_SetTrace` save/restore (FF-3/N2)

**Location:** `ext/recorder.c` eval hook sections G/K

During fast-forward, every non-ignored frame does `Py_XINCREF(saved_traceobj)` and `Py_XDECREF(saved_traceobj)` (lines 502, 538), triggering COW page writes. For nested user frames, `saved_trace` is typically `pyttd_trace_func` — the same function we'd install.

**Fix (optimization):** In the fast-forward eval hook, if `tstate->c_tracefunc == pyttd_trace_func`, skip the save/install/restore cycle entirely:
```c
if (g_fast_forward && tstate->c_tracefunc == (Py_tracefunc)pyttd_trace_func) {
    /* Trace already installed — skip save/restore to avoid COW writes */
    PyObject *result = g_original_eval(tstate, iframe, throwflag);
    /* ... check exception_unwind, decrement depth ... */
    return result;
}
```

This is an optimization, not a correctness fix. Saves ~1 COW page per frame.

---

#### MINOR: `start_recording()` kwlist extension for checkpoint parameters (N1)

**Location:** `ext/recorder.c` line 706

Format string changes from `"O|ii"` to `"O|iiOi"`. The `checkpoint_callback` C variable must be initialized to `NULL` (not `Py_None`) — `PyArg_ParseTupleAndKeywords` with `O` format leaves the variable untouched when the kwarg is absent:
```c
PyObject *checkpoint_cb = NULL;
int checkpoint_interval = 1000;
```
Guard after parsing: `if (checkpoint_cb && checkpoint_cb != Py_None && PyCallable_Check(checkpoint_cb))`.

#### MINOR: `g_fast_forward` variable ownership (N7)

**Location:** `ext/recorder.c` (define), `ext/checkpoint.c` (write via setter)

Define `g_fast_forward` and `g_fast_forward_target` in `recorder.c`. Expose via `recorder_set_fast_forward(int enabled, uint64_t target_seq)` in `recorder.h`. `checkpoint.c` calls the setter when RESUME arrives. No extern globals — setter pattern maintains encapsulation.

#### MINOR: Child monotonic timestamp at target is wall-clock, not recording time (E14)

**Location:** `ext/recorder.c` get_monotonic_time() usage during fast-forward

When the child serializes state at `target_seq`, `get_monotonic_time() - g_start_time` reflects the wall-clock time during fast-forward, not the original recording time. The result JSON should include `sequence_no` so the parent can look up the original timestamp from the DB. Do NOT use the child's timestamp as the display timestamp.

#### MINOR: Debug assertion for sequence counter invariant at fast-forward start (E15)

**Location:** `checkpoint.c` child RESUME handler

Add a debug assertion:
```c
#ifdef Py_DEBUG
assert(g_sequence_counter == checkpoint_original_seq + 1);
#endif
```
This catches divergence early if the child's counter doesn't match expectations.

#### MINOR: `g_inside_repr` scope should be documented as intentionally NOT covering `PyFrame_GetLocals()` (FF-1 addendum)

**Location:** `ext/recorder.c` line 193

Even after FF-1's fix (widening the guard to cover `serialize_locals()`), document why: `PyFrame_GetLocals()` can trigger `__del__` → Python frames → eval hook. The wider guard prevents these phantom events from being recorded.

---

#### Fast-Forward Sequence Counter Correctness Proof

Five increment points in the current code:
| ID | Location | Event | When |
|----|----------|-------|------|
| INC-1 | eval hook line 487 | `call` | Non-ignored, main-thread frame entry |
| INC-2 | eval hook line 516 | `exception_unwind` | `g_original_eval` returned NULL with `PyErr_Occurred()` |
| INC-3 | trace func line 333 | `line` | Every line event in traced frame |
| INC-4 | trace func line 373 | `return` | Normal return (`arg != NULL`) |
| INC-5 | trace func line 405 | `exception` | Exception raised within frame |

**Fast-forward correctness requirements:**
1. All five increment points must execute in fast-forward with identical gating conditions
2. Sections A-E of the eval hook (recording check, stop request, code extraction, ignore filter, thread check) must be preserved verbatim — they control whether the counter increments at all
3. The trace function must remain installed via `PyEval_SetTrace` — without it, CPython's eval loop doesn't generate `PyTrace_LINE`/`RETURN`/`EXCEPTION` callbacks, and INC-3/4/5 never fire
4. The `arg == NULL` gate in `PyTrace_RETURN` (line 356) must be preserved — it determines whether INC-4 or INC-2 fires for exception propagation
5. `g_call_depth++`/`--` must be maintained (eval hook sections F/J) — needed for target frame state serialization

**Proven safe:** `g_inside_repr` prevents repr-triggered frames from incrementing the counter during recording. Fast-forward skips repr entirely → zero increments from repr in both paths (match). Ring buffer fullness doesn't affect counter (increments happen before `ringbuf_push`). `PyEval_SetTrace` side effects are C-only (no additional trace events).

**Known divergence vectors:**
- Non-deterministic execution (different branches from I/O, random) → Phase 4 fix
- `serialize_locals()` `__del__` trigger → FF-1 fix (widen `g_inside_repr` scope)

---

#### Complete Child Post-Fork Initialization Sequence

Consolidating all review findings (C2, C3, D1, D12, D16, E2, E6, E9, E10, N8+N9), the child must execute this sequence immediately after `fork()` returns 0:

```c
/* 1. Reinitialize Python runtime */
PyOS_AfterFork_Child();
/* PyOS_AfterFork_Child() reinitializes GIL (child now holds it),
 * thread state, and import lock. Does NOT reset PEP 523 eval hook
 * or trace function (D10 — we rely on both surviving via COW). */

/* 2. Update thread identity (D1) */
g_main_thread_id = PyThread_get_thread_ident();

/* 3. Disable recording state (D16, N8+N9) */
g_recording = 0;                /* prevent stop_recording issues */
g_flush_thread_created = 0;     /* prevent flush thread join */
g_fast_forward = 0;             /* not yet — set on RESUME */
g_inside_repr = 0;              /* reset reentrancy guard (E9) */

/* 4. Signal handling (C3) */
signal(SIGINT, SIG_IGN);
signal(SIGTERM, SIG_IGN);
signal(SIGPIPE, SIG_IGN);      /* for pipe writes (D2) */
atomic_store(&g_stop_requested, 0);

/* 5. Clear inherited trace functions (E10) */
PyEval_SetTrace(NULL, NULL);

/* 6. Reinitialize pthreads objects (E2) */
g_flush_mutex = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
g_flush_cond = (pthread_cond_t)PTHREAD_COND_INITIALIZER;
/* Also reinitialize g_pause_ack_cv and g_resume_cv */

/* 7. Free ring buffer memory (D12) */
ringbuf_destroy();  /* ~17MB freed, safe because g_recording=0 */

/* 8. Close inherited file descriptors (C2) */
close(tcp_socket_fd);           /* TCP socket to debug adapter */
close(sqlite_fd);               /* Raw SQLite fd — do NOT use db.close() (E1/E7) */
close(cmd_pipe_write_end);      /* Unneeded pipe ends */
close(result_pipe_read_end);

/* 9. Clear atexit handlers (E6) */
PyObject *atexit_mod = PyImport_ImportModule("atexit");
if (atexit_mod) {
    PyObject *r = PyObject_CallMethod(atexit_mod, "_clear", NULL);
    Py_XDECREF(r); Py_DECREF(atexit_mod);
    if (PyErr_Occurred()) PyErr_Clear();
}

/* 10. Release GIL and block on command pipe */
PyThreadState *saved_tstate = PyEval_SaveThread();
/* read(cmd_pipe_read_end, ...) — blocks until RESUME or DIE */
```

---

#### Summary of All Phase 2 Review Issues (Complete)

| ID | Severity | Title | Location |
|----|----------|-------|----------|
| **First review** | | | |
| C1 | Critical | GIL deadlock in pre-fork sync | checkpoint.c |
| C2 | Critical | Child must close inherited fds | checkpoint.c |
| C3 | Critical | Child signal handling | checkpoint.c |
| C4 | Critical | Eval hook naming ambiguity | checkpoint.c, recorder.c |
| C5 | Critical | Trace function blocking + DIE | replay.c |
| C6 | Critical | Fast-forward target unreachable | replay.c |
| S1-S9 | Significant | Various | Multiple |
| M1-M9 | Minor | Various | Multiple |
| **Deep-dive (2nd pass)** | | | |
| D1-D3 | Critical | g_main_thread_id, pipe writes, interval==0 | Multiple |
| D4-D9 | Significant | Eviction, current_position, checkpoint_id, lifecycle, locals type | Multiple |
| D10-D14 | Minor | PyOS_AfterFork_Child, COW, ring buffer, eviction DIE, STEP | Multiple |
| D15-D18 | Significant | Phase 3 race, flush_thread, AfterFork timeout, eviction algo | Multiple |
| D19-D21 | Minor | Backward RESUME, batch kill, callback failure | Multiple |
| **Third pass (this review)** | | | |
| N8+N9 | Critical | g_recording=0 blocks fast-forward in eval hook AND trace func | recorder.c |
| N12/E5 | Critical | Pause ack condvar signal lost + protocol unspecified | checkpoint.c, recorder.c |
| FF-1 | Critical | serialize_locals() __del__ triggers deterministic divergence | recorder.c |
| E10 | Significant | coverage.py trace inherited by child breaks fast-forward | checkpoint.c |
| E6 | Significant | atexit handlers in child on unhandled exception | checkpoint.c |
| E1 | Significant | Peewee threading.Lock in child may deadlock | checkpoint.c |
| E9 | Significant | g_inside_repr inherited by child (latent bug) | recorder.c |
| E2 | Significant | g_flush_mutex/cond undefined state after fork | recorder.c, checkpoint.c |
| N10 | Significant | g_sequence_counter/g_call_depth lose static linkage | recorder.c |
| N2 | Significant | Checkpoint trigger insertion point: between F and G | recorder.c |
| E7 | Moderate | SQLite WAL mmap inherited by child | checkpoint.c |
| E12 | Moderate | Child memory growth during fast-forward undocumented | checkpoint.c |
| FF-3 | Moderate | PyEval_SetTrace COW overhead per frame during fast-forward | recorder.c |
| N1 | Minor | PyArg_ParseTupleAndKeywords defaults for checkpoint_callback | recorder.c |
| N7 | Minor | g_fast_forward ownership: recorder.c with setter | recorder.c |
| E14 | Minor | Child's monotonic timestamp is wall-clock, not recording time | recorder.c |
| E15 | Minor | Debug assertion for sequence counter invariant | checkpoint.c |

---

### Phase 2 Fourth-Pass Review: Contradictions, Missing Edge Cases, and Implementation Hazards

Fourth-pass review with fresh eyes against the actual implemented code (`recorder.c`, `ringbuf.c`, `recorder.py`, `cli.py`, `pyttd_native.c`, stubs). This pass focuses on: (1) contradictions between the plan text and existing reviews, (2) integration hazards with the actual C code, (3) edge cases in the child lifecycle, and (4) missing specification for testability. Issues use prefix R4-.

---

#### CRITICAL: Architecture section says `STEP -1` but pipe protocol says forward-only (R4-1)

**Location:** Architecture section line 111 vs Phase 2 pipe protocol section

The architecture section (line 111) says:
> For sequential rewinds (user steps back multiple times within one checkpoint window), the parent sends incremental `(STEP, -1)` commands to the already-warm child

But the Phase 2 pipe protocol section explicitly states:
> `STEP` only supports forward movement — a checkpoint child cannot step backward because its prior process state is gone after fast-forward

And the DESIGN.md architectural decision says:
> The pipe protocol's `STEP` command is forward-only (no negative deltas — children cannot step backward)

These directly contradict each other. A `STEP(-1)` is physically impossible — the child has already re-executed past that point and the process state is consumed.

**Fix:** Remove or correct the architecture section line 111. The correct behavior for sequential step-back is **always warm** (SQLite read per the DESIGN.md architectural decision: "step back is always warm"). The `STEP` command in the pipe protocol is only for forward incremental movement from the warm child's current position (e.g., step-forward within a checkpoint window without needing a fresh RESUME).

#### CRITICAL: `g_call_depth` is NOT reset correctly for fast-forward counter match (R4-2)

**Location:** `ext/recorder.c` line 44-45, checkpoint child RESUME handler

When a checkpoint is taken at `sequence_no = N`, the child inherits `g_call_depth = D` (whatever depth the checkpoint frame is at). When the child receives RESUME and starts fast-forward, the eval hook begins with `g_call_depth++` at line 483. The counter increments mirror the recording, so `g_sequence_counter` advances correctly.

**But:** Consider a checkpoint taken at depth 3. After fork, the child blocks. On RESUME, control returns to the eval hook *at the checkpoint call's position* (between recording the call event and calling `g_original_eval`). The eval hook proceeds to step G (install trace) and step H (call `g_original_eval`). When `g_original_eval` returns for this frame, the eval hook decrements `g_call_depth` (line 530). If the user's code returns from multiple nested calls before reaching `target_seq`, `g_call_depth` can go negative in the child.

This doesn't affect sequence counter correctness (depth isn't part of the counter), but if the target-reached serialization reads `g_call_depth`, it reports a wrong depth. The child's `g_call_depth` only matches the recording's depth if the child re-executes the exact same call pattern — which is true for deterministic code but NOT guaranteed before Phase 4.

**Fix:** Document that `g_call_depth` in the child's serialized result is **from re-execution** and may differ from the recording's depth for non-deterministic code. The parent should prefer the DB's `call_depth` value for the target `sequence_no` over the child's reported value. Only the child's `locals` (live object state) should be used from the cold result — metadata comes from the DB.

#### CRITICAL: `checkpoint_do_fork` releases GIL while `code` PyObject* is borrowed (R4-3)

**Location:** `ext/recorder.c` lines 443-444, checkpoint trigger insertion point (N2/N3)

The checkpoint trigger is inserted between line 497 (`ringbuf_push`) and line 500 (`install trace`). At this point, `code` (PyCodeObject*) was obtained at line 443 via `PyUnstable_InterpreterFrame_GetCode(iframe)` (returns a new reference — caller must `Py_DECREF`). The `filename` and `funcname` C strings are borrowed from `code->co_filename` and `code->co_qualname` via `PyUnicode_AsUTF8()`.

`checkpoint_do_fork()` calls `PyEval_SaveThread()` (releases GIL), waits for flush thread, then calls `fork()`. While the GIL is released:
- `code` remains valid (we hold a reference via `Py_INCREF` from `GetCode`)
- `filename` and `funcname` are pointers into `PyUnicodeObject` internal buffers — valid as long as `code` is alive
- The flush thread (before it pauses) could trigger garbage collection via `PyGILState_Ensure()`, but `code` is protected by our reference count

This is **safe** because `code` is ref-counted and alive throughout the eval hook. The `filename`/`funcname` pointers are stable as long as `code` exists. No fix needed, but document this invariant at the checkpoint trigger insertion point.

**Revised assessment:** Safe. Add a comment noting the invariant.

#### SIGNIFICANT: `Py_DECREF(code)` at eval hook line 539 happens in child's fast-forward path (R4-4)

**Location:** `ext/recorder.c` line 539, child fast-forward

After the checkpoint child's `g_original_eval` returns for each frame during fast-forward, the eval hook hits `Py_DECREF(code)` at line 539. In the child process, this triggers COW writes (modifying the PyObject reference count in the child's memory page). For code objects, this is especially wasteful — they're typically long-lived and shared.

D11 says "the fast-forward mode must skip ALL `Py_INCREF`/`Py_DECREF` calls on temporary objects." But the eval hook's fast-forward path (Solution B from N8+N9) routes through `pyttd_eval_hook_fast_forward()`, which must still get the code object to check `should_ignore()`. The code object is obtained via `PyUnstable_InterpreterFrame_GetCode(iframe)` which returns a new reference — it MUST be `Py_DECREF`'d, even in fast-forward mode, to avoid a leak.

**Fix:** Accept the COW overhead for `Py_DECREF(code)` — it's unavoidable since `GetCode` returns a new reference. To reduce overhead, the fast-forward eval hook could cache the ignore filter result per code object (pointer comparison, no need to re-check `should_ignore` for the same `co_filename`). But this is an optimization for later.

#### SIGNIFICANT: `checkpoint_store_find_nearest` is called from Python via `pyttd_restore_checkpoint`, but the stub signature takes `(self, args)` (R4-5)

**Location:** `ext/replay.c`, `ext/checkpoint_store.c`, `ext/pyttd_native.c`

The current `pyttd_restore_checkpoint` stub in `replay.c` takes `PyObject *args` but doesn't parse it. The plan says it takes `target_seq` as an argument. The `pyttd_native.c` method table registers it as `METH_VARARGS`.

The plan's `ReplayController.goto_frame()` calls:
```python
result_json = pyttd_native.restore_checkpoint(target_seq)
```

But the C function must:
1. Parse `target_seq` from args
2. Call `checkpoint_store_find_nearest(target_seq)` to get the entry
3. Send RESUME to the child
4. Read the result

None of this is specified in the C function signature. The plan shows `pyttd_native.restore_checkpoint(target_seq)` but never shows the C implementation that parses this argument.

**Fix:** Specify the C implementation skeleton:
```c
PyObject *pyttd_restore_checkpoint(PyObject *self, PyObject *args) {
    uint64_t target_seq;
    if (!PyArg_ParseTuple(args, "K", &target_seq))  /* K = unsigned long long */
        return NULL;
    int idx = checkpoint_store_find_nearest(target_seq);
    if (idx < 0) {
        PyErr_SetString(PyExc_RuntimeError, "No usable checkpoint found");
        return NULL;
    }
    /* ... copy fds, release GIL, send RESUME, read result, re-acquire GIL ... */
}
```

#### SIGNIFICANT: `setup.py` doesn't include `checkpoint_store.c` in ext_modules sources (R4-6)

**Location:** `setup.py`

The plan creates new C files (`checkpoint.c`, `checkpoint_store.c`, `replay.c`) but doesn't mention updating `setup.py`'s `ext_modules` to include them in the `sources` list. Without this, the C extension won't compile the new files.

**Fix:** Add a reminder to update `setup.py`:
```python
Extension(
    'pyttd_native',
    sources=[
        'ext/pyttd_native.c',
        'ext/recorder.c',
        'ext/ringbuf.c',
        'ext/checkpoint.c',       # Phase 2
        'ext/checkpoint_store.c',  # Phase 2
        'ext/replay.c',            # Phase 2
        'ext/iohook.c',
    ],
)
```

#### SIGNIFICANT: `_on_checkpoint` called from eval hook creates a DB write on the recording thread (R4-7)

**Location:** Phase 2 C signature update section, `recorder.py`

The plan says `_on_checkpoint` is called from the eval hook (with GIL held) after successful fork. This callback does:
```python
Checkpoint.create(run_id=..., sequence_no=..., child_pid=...)
```

This is a **synchronous SQLite INSERT on the recording (main) thread**. The flush thread also does DB writes (via `_on_flush` → `batch_insert`). Both use Peewee's `autoconnect=True` thread-local connections, so they don't share a connection object.

However, SQLite WAL mode with `busy_timeout=5000` allows concurrent readers but only ONE concurrent writer. If the flush thread is mid-`batch_insert` when the eval hook triggers a checkpoint:
1. Eval hook calls `checkpoint_do_fork()` → pre-fork sync → flush thread pauses
2. Fork succeeds, parent resumes
3. Parent calls Python `_on_checkpoint` → `Checkpoint.create()` → INSERT
4. Parent signals `resume_cv` → flush thread resumes → `batch_insert()` → INSERT

Steps 3 and 4 are sequential (flush thread is paused during step 3), so there's no actual contention. **This is safe** because the pre-fork sync guarantees the flush thread is paused when `_on_checkpoint` runs.

**No fix needed.** Document this as a verified-safe invariant.

#### SIGNIFICANT: `PyttdConfig.checkpoint_interval` is passed to `_cmd_record` but never forwarded to `Recorder.start()` (R4-8)

**Location:** `pyttd/cli.py` line 64, `pyttd/recorder.py` line 15

Currently `_cmd_record` creates `PyttdConfig(checkpoint_interval=args.checkpoint_interval)` but `Recorder.start()` doesn't pass `checkpoint_interval` or `checkpoint_callback` to `pyttd_native.start_recording()`. The plan says Phase 2 updates `start_recording()` to accept these parameters and `recorder.py.start()` to pass them.

But the plan's `recorder.py` update only shows the `start_recording()` call being extended — it doesn't show updating `Recorder.start()`'s method signature or the `_on_checkpoint` method being added to the `Recorder` class.

**Fix:** The plan should show the complete `Recorder` class changes:
```python
class Recorder:
    def start(self, db_path, script_path=None):
        storage.connect_to_db(db_path)
        storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])
        self._run = Runs.create(script_path=script_path)
        # ...
        pyttd_native.start_recording(
            flush_callback=self._on_flush,
            buffer_size=self.config.ring_buffer_size,
            flush_interval_ms=self.config.flush_interval_ms,
            checkpoint_callback=self._on_checkpoint,
            checkpoint_interval=self.config.checkpoint_interval,
        )

    def _on_checkpoint(self, child_pid, sequence_no):
        from pyttd.models.checkpoints import Checkpoint
        Checkpoint.create(
            run_id=self._run.run_id,
            sequence_no=sequence_no,
            child_pid=child_pid
        )

    def kill_checkpoints(self):
        pyttd_native.kill_all_checkpoints()
        from pyttd.models.checkpoints import Checkpoint
        Checkpoint.update(is_alive=False, child_pid=None).where(
            Checkpoint.run_id == self._run.run_id
        ).execute()
```

#### SIGNIFICANT: `_cmd_record` checkpoint children are created but `_cmd_record` only uses warm replay — wasted resources (R4-9)

**Location:** `pyttd/cli.py` `_cmd_record()`

With Phase 2, `_cmd_record` will create checkpoint children during recording (via `checkpoint_callback`). But the CLI `replay` command uses warm-only navigation, and `_cmd_record` calls `recorder.stop()` then `recorder.cleanup()` — children are orphaned or need to be killed.

The plan (S4, D7) discusses the lifecycle but doesn't address the simpler question: **should `_cmd_record` create checkpoints at all?** Creating fork children during CLI recording wastes memory (~1MB+ per child, up to 32 children = ~32MB) with zero benefit — the children are killed on exit and can never be used for cold replay.

**Fix:** Either:
- (a) Don't pass `checkpoint_callback` in `_cmd_record` (set to `None` → C code skips checkpoint creation). Only pass it in `_cmd_serve` where cold replay is useful.
- (b) Add a `--no-checkpoint` CLI flag (default: no checkpoints in `record`, yes in `serve`).

Option (a) is simplest and matches the design intent. The `checkpoint_interval` CLI arg on `record` becomes a no-op or is only used to write the interval to the DB for a future `serve` session to read.

#### SIGNIFICANT: `Checkpoint` model's `is_alive` and `child_pid` are runtime state persisted to DB — stale after process restart (R4-10)

**Location:** `pyttd/models/checkpoints.py`

The `Checkpoint` model stores `child_pid` and `is_alive` in the DB. But checkpoint children only exist within a single OS process session. If the server crashes and restarts, the DB has `is_alive=1` rows with `child_pid` values pointing to dead processes.

The plan notes `child_pid` is "null after child is killed or session ends" but doesn't specify **when** `is_alive` gets set to 0 on crash/restart. `ReplayController.kill_all()` does it, but if the process crashes (SIGKILL, OOM), the update never runs.

**Fix:** On startup (`Recorder.start()` or server init), run:
```python
Checkpoint.update(is_alive=False, child_pid=None).execute()
```
This clears stale state from any previous session. Fresh checkpoints are created during the new recording. This is safe because `delete_db_files()` already deletes the DB before recording — but in server mode (Phase 3), the DB might be re-opened without recreation.

#### SIGNIFICANT: `get_nearest_checkpoint` queries DB but C store is the source of truth for live children (R4-11)

**Location:** `replay.py` `get_nearest_checkpoint()`, D5 `current_position` field

The `ReplayController.get_nearest_checkpoint()` queries the DB:
```python
Checkpoint.select().where(
    (Checkpoint.sequence_no <= target_seq) & (Checkpoint.is_alive == 1))
```

But the DB stores `sequence_no` (original checkpoint position), not `current_position` (which only exists in the C-level `CheckpointEntry`). After a RESUME, the C store knows the child moved to position 800, but the DB still says `sequence_no = 500`.

This means the DB query can return a checkpoint that's been consumed past the target. The plan acknowledges this (S2) and says "try the C path, catch exception, fall back to warm." But this is worse than S2 describes — the Python code queries the DB first, THEN calls C. If the DB says "checkpoint at 500 is alive," Python calls `pyttd_native.restore_checkpoint(target_seq=600)`, C sees this checkpoint is at position 800, and returns an error. Every such call wastes a round-trip.

**Fix (recommended):** Don't query the DB for cold navigation at all. Have `pyttd_native.restore_checkpoint(target_seq)` internally call `checkpoint_store_find_nearest()` (which uses `current_position`). If no usable checkpoint exists, it raises an exception, and Python falls back to warm. The DB `Checkpoint` table is for diagnostics/persistence only, not for runtime checkpoint selection. Simplify `ReplayController`:
```python
def goto_frame(self, run_id, target_seq):
    try:
        return pyttd_native.restore_checkpoint(target_seq)
    except Exception:
        return self._warm_fallback(run_id, target_seq)
```

#### MINOR: Architecture section's pre-fork sync doesn't mention signaling `g_flush_cond` (R4-12)

**Location:** Architecture section lines 113-118

The architecture section's pre-fork description says:
> 1. Parent sets an atomic `pause_requested` flag
> 2. Flush thread checks this flag at the top of each iteration

But the flush thread might be sleeping on `pthread_cond_timedwait(&g_flush_cond, ...)` with up to `flush_interval_ms` (10ms) remaining. Without signaling `g_flush_cond`, the parent waits up to 10ms for the flush thread to wake and check the flag. The C1 fix in the Phase 2 review section correctly adds `pthread_cond_signal(&g_flush_cond)`, but the architecture section doesn't mention this.

**Fix:** Update architecture section line 113-118 to include the condvar signal, or add a note: "See Phase 2 review C1 fix for the corrected pre-fork sequence."

#### MINOR: `checkpoint_store_add` stub returns 0 but callers never check the return value (R4-13)

**Location:** `ext/checkpoint_store.c` line 8, `ext/checkpoint_store.h` line 8

The stub `checkpoint_store_add` returns `int` (0), suggesting success/failure. But the plan never specifies what the return value means or shows callers checking it. When the store is full and eviction is needed, does `add` handle eviction internally and always succeed, or does it return -1 and the caller must evict first?

**Fix:** Specify: `checkpoint_store_add` handles eviction internally (calls `checkpoint_to_evict` + `checkpoint_store_evict` when full). Returns the index where the entry was added (0 to MAX_CHECKPOINTS-1), or -1 on failure (e.g., all entries are in active RESUME — can't evict a child mid-operation). Update the header:
```c
/* Add a checkpoint. Handles eviction if store is full.
 * Returns index of new entry, or -1 if add failed. */
int checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no);
```

#### MINOR: Plan doesn't specify `g_checkpoint_callback` reference counting (R4-14)

**Location:** Phase 2 C signature update, `ext/recorder.c`

The plan adds `checkpoint_callback` as a kwarg to `start_recording`. Like `g_flush_callback` (line 748-749), the C code must `Py_INCREF` it and store it as a global. `stop_recording` must `Py_XDECREF` it and set it to NULL.

The existing pattern for `g_flush_callback`:
```c
g_flush_callback = callback;
Py_INCREF(g_flush_callback);
// ... in stop_recording:
Py_XDECREF(g_flush_callback);
g_flush_callback = NULL;
```

**Fix:** Document that the same pattern must be applied to `g_checkpoint_callback`. Also: `g_checkpoint_callback` must be `Py_XDECREF`'d in `stop_recording` (not `cleanup` or `kill_checkpoints`), and set to NULL.

#### MINOR: Fast-forward eval hook needs ignore filter to check against `filename`/`funcname` but hasn't extracted them yet (R4-15)

**Location:** N8+N9 Solution B, `ext/recorder.c` eval hook structure

Solution B defines `pyttd_eval_hook_fast_forward()` as the first check in the eval hook. But `should_ignore(filename, funcname)` requires extracting `filename` and `funcname` from the iframe. The current eval hook extracts these at lines 443-445 (after the `g_recording` check).

In fast-forward mode, the fast-forward hook function must ALSO extract `filename`/`funcname` and call `should_ignore()` — the same code extraction that the normal path does. This means the fast-forward path is NOT minimal (just "increment counter, check target, return") as D11 claims — it must do code extraction, ignore filtering, depth tracking, and trace installation.

**Fix:** Specify that `pyttd_eval_hook_fast_forward` does:
1. Extract code object (unavoidable — needed for ignore filter)
2. `should_ignore()` check (must match recording's filter exactly — FF proof requirement 2)
3. If ignored: save/remove/restore trace (same as normal path)
4. If not ignored: increment depth, increment counter, check target, install trace, call `g_original_eval`, check exception_unwind, decrement depth, restore trace
5. Skip: `serialize_locals`, `ringbuf_push`, `g_frame_count++`, flush signal, `get_monotonic_time`

The fast-forward trace function similarly must:
1. Skip: `serialize_locals`, `ringbuf_push`, `g_frame_count++`, `get_monotonic_time`
2. Keep: counter increment, event type gating (`PyTrace_CALL` skip, `arg==NULL` skip)

#### MINOR: `pyttd_native.c` method table needs `checkpoint_store_init()` call in module init (R4-16)

**Location:** `ext/pyttd_native.c` `PyInit_pyttd_native()`

`checkpoint_store_init()` initializes the C-level array. The plan doesn't specify when this is called. It should be called during module initialization (`PyInit_pyttd_native`) so the store is ready before any recording starts. Currently `PyInit_pyttd_native()` just calls `PyModule_Create` — no initialization hooks.

**Fix:** Either:
- (a) Call `checkpoint_store_init()` in `PyInit_pyttd_native()` — simple, runs once at import
- (b) Call it in `start_recording()` — lazy init, matches ring buffer pattern
Option (b) is more consistent with the existing code pattern (ring buffer is initialized in `start_recording`).

#### MINOR: The plan's `_cmd_replay` uses `controller.goto_frame(run.run_id, args.goto_frame)` but `goto_frame` signature in plan takes `(self, run_id, target_seq)` — warm-only (R4-17)

**Location:** Phase 2 CLI replay section, `replay.py`

The CLI `_cmd_replay` calls `controller.goto_frame(run.run_id, args.goto_frame)`. The `ReplayController.goto_frame` tries cold first then falls back to warm. But the plan also says CLI replay is always warm-only.

Since CLI mode has no live checkpoint children (recording process already exited), `pyttd_native.restore_checkpoint()` will always fail (C store is empty). The try/except pattern works correctly (falls back to warm), but it's misleading and wasteful.

**Fix:** The CLI `_cmd_replay` should call `_warm_fallback` directly, bypassing the cold path:
```python
controller = ReplayController()
result = controller._warm_fallback(run.run_id, args.goto_frame)
```
Or `ReplayController` should expose a public `warm_goto_frame()` method.

---

#### Summary of Fourth-Pass Review Issues

| ID | Severity | Title | Location |
|----|----------|-------|----------|
| R4-1 | Critical | Architecture says STEP -1 but protocol is forward-only | Architecture section vs pipe protocol |
| R4-2 | Critical | g_call_depth may diverge in child for non-deterministic code | recorder.c, checkpoint child |
| R4-3 | Critical (resolved) | code PyObject* during GIL release — actually safe | recorder.c checkpoint trigger |
| R4-4 | Significant | Py_DECREF(code) unavoidable COW in fast-forward | recorder.c |
| R4-5 | Significant | pyttd_restore_checkpoint C implementation unspecified | replay.c |
| R4-6 | Significant | setup.py sources list not updated for new C files | setup.py |
| R4-7 | Significant (resolved) | _on_checkpoint DB write race — safe due to pre-fork sync | recorder.py |
| R4-8 | Significant | Recorder.start() changes not fully specified | recorder.py |
| R4-9 | Significant | _cmd_record creates useless checkpoint children | cli.py |
| R4-10 | Significant | Stale is_alive/child_pid after process crash | checkpoints.py |
| R4-11 | Significant | DB query for checkpoint selection ignores current_position | replay.py |
| R4-12 | Minor | Architecture pre-fork sync missing condvar signal | Architecture section |
| R4-13 | Minor | checkpoint_store_add return value unspecified | checkpoint_store.h |
| R4-14 | Minor | g_checkpoint_callback refcount not specified | recorder.c |
| R4-15 | Minor | Fast-forward eval hook is NOT minimal — needs ignore filter | recorder.c |
| R4-16 | Minor | checkpoint_store_init() call site unspecified | pyttd_native.c |
| R4-17 | Minor | CLI replay should bypass cold path | replay.py |

### Phase 2 Fifth-Pass Review: Code-Level Correctness, Cross-Review Errors, and Undocumented Hazards

Fifth-pass review cross-referencing all four prior reviews against the actual implemented code (`recorder.c` 914 lines, `ringbuf.c` 229 lines, `pyttd_native.c` 52 lines, all stubs/headers) and Phase 2 plan text. This pass focuses on: (1) bugs in prior review code snippets, (2) cross-review contradictions, (3) undocumented hazards from the actual C code, and (4) specification gaps that would block implementation. Issues use prefix R5-.

---

#### BUG IN REVIEW: N12 condvar code has undeclared `rc` variable (R5-1)

**Location:** N12 fix code block (line 2947-2960)

The N12 code snippet for the parent-side condvar wait:
```c
while (!atomic_load(&g_pause_acked)) {
    pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
    if (rc == ETIMEDOUT) break;
}
```

`pthread_cond_timedwait` is called but its return value is not captured. `rc` is undeclared — this code does not compile. The E5 expansion (line 3027-3029) correctly writes `int rc = pthread_cond_timedwait(...)`, but N12's version is broken and will mislead any implementer who uses it as reference.

**Fix:** Update N12 code to match E5:
```c
while (!atomic_load(&g_pause_acked)) {
    int rc = pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
    if (rc == ETIMEDOUT) break;
}
```

#### BUG IN REVIEW: R4-6 is incorrect — `setup.py` already includes all C files (R5-2)

**Location:** R4-6 (line 3462-3482)

R4-6 claims "`setup.py` doesn't include `checkpoint_store.c` in ext_modules sources." This is wrong. Reading the actual `setup.py` confirms it already lists `ext/checkpoint.c`, `ext/checkpoint_store.c`, `ext/replay.c`, and `ext/iohook.c` in the `sources` list (added during Phase 0 scaffolding as stub files).

**Fix:** Strike R4-6 or mark it as resolved/incorrect. No `setup.py` changes needed for Phase 2.

#### SIGNIFICANT: D12 self-contradicts three times — final conclusion is unclear (R5-3)

**Location:** D12 (lines 2567-2583)

D12 goes through three different proposals for ring buffer cleanup in the child, each contradicting the previous:

1. "Set `g_fast_forward = 1` BEFORE `PyOS_AfterFork_Child()`" — then immediately says "Wait — `g_fast_forward` can't be set before fork (parent would also see it)."
2. "In child, immediately after fork returns 0: set `g_fast_forward = 1` and `g_recording = 0`" — but N8+N9 (written later) specifies `g_fast_forward` should be 0 after fork, set to 1 only on RESUME.
3. "Actually, the child blocks on `read(cmd_pipe)` immediately after fork setup — no Python code executes... so `ringbuf_destroy()` can safely be called anytime during child initialization, before the `read()` block."

The third conclusion is correct and matches the consolidated child init sequence (line 3272-3324), but the text is confusing — a reader might implement conclusion 1 or 2 instead.

**Fix:** Replace D12's body with a single clear statement:
> The child blocks on `read(cmd_pipe)` during initialization — no Python code (and hence no eval hook) executes until RESUME arrives. `ringbuf_destroy()` can be called at any point during child initialization (after `PyOS_AfterFork_Child()`). See the consolidated child post-fork init sequence for the canonical ordering (step 7).

#### SIGNIFICANT: `g_dir_filter`/`g_exact_filter` contain `strdup`'d pointers — child must NOT call `clear_filters()` (R5-4)

**Location:** `ext/recorder.c` lines 68-69, 103-114, child post-fork init sequence (line 3272-3324)

The ignore filter arrays (`g_dir_filter`, `g_exact_filter`) contain `strdup()`'d string pointers. After `fork()`, the child inherits these pointers via COW — they point to valid memory in the child's address space. The child MUST keep these filters intact because:
1. Fast-forward requires the exact same `should_ignore()` behavior (FF proof requirement 2, line 3262)
2. The strings are allocated heap memory that the child can safely read (COW)

However, no review has specified that the child must NOT call `clear_filters()` — which is called by `stop_recording()` and `pyttd_set_ignore_patterns()`. If the child accidentally calls either function, `free()` is called on COW-shared memory, which is safe (the child gets its own copy), but the filter data is lost and subsequent `should_ignore()` calls during fast-forward would return 0 for everything — causing sequence counter divergence.

The consolidated child init sequence (line 3272-3324) does not call `clear_filters()`, which is correct. But the hazard is undocumented, and `stop_recording()` calls it (line 791-836 of `recorder.c`). If someone adds a `stop_recording()` call to the child cleanup path, the filters are destroyed.

**Fix:** Add a comment to the child post-fork init sequence:
```c
/* NOTE: Do NOT call stop_recording() or clear_filters() in child —
 * fast-forward requires the inherited ignore filter arrays to remain intact.
 * The strdup'd strings in g_dir_filter/g_exact_filter are valid via COW. */
```

#### SIGNIFICANT: `g_start_time` inherited by child makes fast-forward timestamps meaningless (R5-5)

**Location:** `ext/recorder.c` line 86-98, child fast-forward

The child inherits `g_start_time` (the monotonic time when recording started in the parent). During fast-forward, if the child calls `get_monotonic_time() - g_start_time`, it gets wall-clock elapsed since the parent started recording, not the recording-relative timestamp of the target frame. E14 (line 3363) notes "Child's monotonic timestamp is wall-clock, not recording time" but only as a minor issue.

The real concern is: when the child reaches the target and serializes frame state for the result pipe, does it include a timestamp? The pipe protocol JSON (line 1906) specifies `"seq"`, `"file"`, `"line"`, `"function_name"`, `"call_depth"`, `"locals"` — no timestamp field. This is correct: the parent should use the DB's recorded timestamp for the target `sequence_no`, not the child's.

However, R4-2 (line 3391-3401) recommends using the DB's `call_depth` instead of the child's reported value. The same logic should apply to ALL metadata fields — the child's result should only be trusted for `locals` (live object state). Everything else comes from the DB.

**Fix:** Explicitly specify in the pipe result protocol that the child's `seq`, `file`, `line`, `function_name`, `call_depth` fields are **redundant with the DB** and included for cross-validation only. The canonical source for metadata is the DB; the child's result is canonical only for `locals` (which the DB stores as `repr()` snapshots, while the child has live objects). In `ReplayController.goto_frame()`:
```python
# Merge: metadata from DB, locals from child
db_frame = ExecutionFrames.get(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.sequence_no == target_seq))
cold_result = pyttd_native.restore_checkpoint(target_seq)
return {
    "seq": target_seq,
    "file": db_frame.filename,
    "line": db_frame.line_no,
    "function_name": db_frame.function_name,
    "call_depth": db_frame.call_depth,
    "locals": cold_result["locals"],  # Live objects, not repr snapshots
}
```

#### SIGNIFICANT: E5 parent-side code calls `PyEval_SaveThread()` while holding `g_flush_mutex` — thread starvation risk (R5-6)

**Location:** E5 parent-side code (lines 3020-3039)

The E5 code sequence is:
```c
pthread_mutex_lock(&g_flush_mutex);
pthread_cond_signal(&g_flush_cond);          /* wake flush thread */
PyThreadState *saved = PyEval_SaveThread();  /* release GIL */
while (!atomic_load(&g_pause_acked)) {
    int rc = pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
```

After `pthread_mutex_lock(&g_flush_mutex)`, the parent signals `g_flush_cond` and releases the GIL. But the flush thread's normal loop (E5 lines 2991-3017) does:
```c
pthread_mutex_lock(&g_flush_mutex);
pthread_cond_timedwait(&g_flush_cond, &g_flush_mutex, &ts);
pthread_mutex_unlock(&g_flush_mutex);
/* ... flush_batch() ... */
if (atomic_load_explicit(&g_pause_requested, memory_order_acquire)) {
    pthread_mutex_lock(&g_flush_mutex);   /* <-- blocks here */
```

If the flush thread is in `flush_batch()` (not holding `g_flush_mutex`) when the parent signals, the sequence is:
1. Parent locks `g_flush_mutex` ✓
2. Parent signals `g_flush_cond` (no one listening — flush thread is in `flush_batch()`) ✓
3. Parent releases GIL ✓
4. Flush thread finishes `flush_batch()`, checks `g_pause_requested` (set) ✓
5. Flush thread tries `pthread_mutex_lock(&g_flush_mutex)` — **blocks** because parent holds it
6. Parent is in `pthread_cond_timedwait` which atomically releases `g_flush_mutex` and waits

Step 6 releases the mutex, unblocking step 5. This works correctly because `pthread_cond_timedwait` atomically releases the mutex. **No actual bug** — the condvar protocol is correct despite the apparent ordering concern. But the code is subtle and needs a comment explaining why holding the mutex across the GIL release is intentional and safe.

**Fix:** Add comment: `/* Holding g_flush_mutex here is intentional — pthread_cond_timedwait atomically releases it, allowing the flush thread to proceed with its pause acknowledgment. */`

#### SIGNIFICANT: `timeout` variable in E5 parent-side code is never constructed (R5-7)

**Location:** E5 parent-side code (line 3028)

```c
int rc = pthread_cond_timedwait(&g_pause_ack_cv, &g_flush_mutex, &timeout);
```

`timeout` is referenced but never declared or initialized. `pthread_cond_timedwait` requires an absolute `struct timespec` (not relative). The code needs:
```c
struct timespec timeout;
clock_gettime(CLOCK_REALTIME, &timeout);
timeout.tv_sec += 1;  /* 1-second timeout per original plan */
```

The original plan (line 1862) says "Timeout after 1 second" but the E5 code never constructs the timeout. The N12 code (line 2957) has the same issue.

**Fix:** Add timeout construction before the while loop in E5:
```c
struct timespec timeout;
clock_gettime(CLOCK_REALTIME, &timeout);
timeout.tv_sec += 1;  /* 1-second timeout — skip checkpoint if flush thread is stuck */
```

Note: `pthread_cond_timedwait` uses `CLOCK_REALTIME` by default. For robustness against system clock changes, use `pthread_condattr_setclock(CLOCK_MONOTONIC)` when initializing the condvar, then use `clock_gettime(CLOCK_MONOTONIC, ...)`. This is a minor robustness improvement.

#### SIGNIFICANT: Child post-fork init step 5 clears trace function but fast-forward needs it (R5-8)

**Location:** Child post-fork init sequence step 5 (line 3296-3297), N8+N9 Solution B

Step 5 of the child init:
```c
/* 5. Clear inherited trace functions (E10) */
PyEval_SetTrace(NULL, NULL);
```

This removes the trace function so external tools (coverage.py) don't interfere. But N8+N9 Solution B and R4-15 both specify that fast-forward mode MUST install the trace function (via `PyEval_SetTrace` in the eval hook's step 6) to count `line`/`return`/`exception` events.

The sequence is:
1. Child init: `PyEval_SetTrace(NULL, NULL)` — clears trace ✓
2. Child blocks on `read(cmd_pipe)` — no Python runs ✓
3. RESUME arrives → child returns into eval hook → step 6 installs `pyttd_trace_func` ✓

This works because clearing the trace in step 5 only affects the global trace state. When the eval hook resumes after RESUME, it re-installs `pyttd_trace_func` via `PyEval_SetTrace` (step 6, line 504 of `recorder.c`). Each frame entry through the eval hook installs the trace function.

**No bug** — the sequence is correct. But the E10 comment at step 5 should note: "The fast-forward eval hook re-installs pyttd_trace_func on each frame entry (step 6), so clearing here only prevents inherited external traces from running before RESUME."

#### MODERATE: `STEP` command semantics vs consumed checkpoint interaction not fully specified (R5-9)

**Location:** Pipe protocol section (lines 1896), warm child section (line 2912)

The `STEP` command payload is `delta` (advance N events forward). After a `RESUME(target_seq)` completes and the child writes the result, the child blocks on `read(cmd_pipe)` again. The parent can then send `STEP(delta)` to advance the warm child incrementally.

But what happens when the child reaches the end of the script during a STEP? The plan specifies C6 (target unreachable) for RESUME but not for STEP. The same scenarios apply:
- The script finishes before advancing `delta` events
- An unhandled exception terminates the script
- Non-deterministic divergence causes fewer events

**Fix:** STEP must have the same error handling as RESUME (C6): if the child's re-execution ends before advancing `delta` events, write an error result `{"status": "error", "error": "step_beyond_end", "actual_delta": <actual>}` to the result pipe, then block on `cmd_pipe` for the next command.

#### MODERATE: No specification for child's RESUME handler when `g_fast_forward_target < g_sequence_counter` (R5-10)

**Location:** Pipe protocol, replay.c, child RESUME handler

After a child has been consumed to position P (via a previous RESUME), its `g_sequence_counter` is at P. If the parent sends `RESUME(target_seq)` where `target_seq <= P`, the child is already past the target — it cannot go backward.

The C-level `checkpoint_store_find_nearest()` should prevent this by only selecting checkpoints with `current_position <= target_seq`. But if there's a race or bug, the child receives an impossible command.

**Fix:** The child's RESUME handler must validate: if `target_seq <= g_sequence_counter`, write an error result `{"status": "error", "error": "already_past_target", "current_seq": <g_sequence_counter>}` and block on `cmd_pipe` without entering fast-forward. This is a defensive check that should never trigger in correct operation, but prevents the child from entering an infinite fast-forward (waiting for a sequence number that was already passed).

#### MODERATE: `g_frame_count` in child is wasted but not reset (R5-11)

**Location:** `ext/recorder.c` line 44 (`g_frame_count`), child post-fork init

`g_frame_count` is a counter used for stats (`get_recording_stats()`). The child inherits it from the parent. During fast-forward, R4-15 specifies `g_frame_count++` is skipped. But:
1. `g_frame_count` is never reset in the child init sequence
2. If someone calls `get_recording_stats()` in the child (unlikely but possible), it returns the parent's count
3. `g_frame_count` is not atomic — but that's fine since the child is single-threaded

This is cosmetic — `g_frame_count` serves no purpose in the child. But for cleanliness:

**Fix:** Add `g_frame_count = 0;` to the child init sequence (after step 3, alongside `g_inside_repr = 0`). Or document it as intentionally ignored.

#### MINOR: `conftest.py` `record_func` fixture doesn't clean up ignore filters between tests (R5-12)

**Location:** `tests/conftest.py` lines 26-60, `ext/recorder.c` `start_recording`

The `record_func` fixture calls `recorder.start()` → `pyttd_native.start_recording()`. In `recorder.c`, `start_recording` does NOT call `clear_filters()` — filters are set separately via `pyttd_set_ignore_patterns()`. But `stop_recording()` does NOT clear filters either (it only clears the callback and destroys the ring buffer).

This means ignore filter state persists across test recordings in the same process. If one test calls `pyttd_native.set_ignore_patterns(["/site-packages/"])` and a subsequent test doesn't reset it, the filter leaks. Currently no test sets ignore patterns (the Phase 1 `Recorder.start()` doesn't call `set_ignore_patterns`), so this isn't a problem yet. But Phase 2 or Phase 3 tests that set filters would need to reset them.

**Fix:** Either:
- (a) Call `clear_filters()` at the start of `start_recording()` (most robust — ensures clean state)
- (b) Add `pyttd_native.set_ignore_patterns([])` to the test fixture teardown

Option (a) is preferred — it prevents filter leakage regardless of how `start_recording` is called.

#### MINOR: The plan's exponential thinning eviction algorithm is referenced but never specified (R5-13)

**Location:** Line 1866, M8 (line 2322), D4 (line 2506), Architecture section

Multiple locations reference "exponential thinning eviction" and "O(log N) coverage," but no review or plan section provides the actual algorithm. D4 (line 2506-2540) discusses eviction implementation details (sentinel `sequence_no`, skip active children) but not the thinning algorithm itself. The Architecture section says "exponential thinning" but doesn't define it.

The typical algorithm: maintain checkpoints at positions {N, N/2, N/4, N/8, ...} relative to the recording timeline. When a new checkpoint arrives and the store is full, evict the checkpoint that minimizes the maximum gap between consecutive checkpoints. This requires O(K) scan of K checkpoints.

**Fix:** Add the actual algorithm to `checkpoint_store.c` specification:
```c
/* Exponential thinning: maintain O(log N) coverage of the recording timeline.
 * When store is full, find the pair of adjacent checkpoints with the smallest gap
 * and evict the earlier one (preserving the more recent checkpoint for better
 * coverage of recent history). This naturally produces exponential spacing:
 * dense near the end of the recording, sparse near the beginning.
 *
 * Algorithm: Sort live checkpoints by sequence_no. For each adjacent pair (i, i+1),
 * compute gap = seq[i+1] - seq[i]. Evict the checkpoint with the smallest gap
 * to its successor (ties broken by evicting the earlier one).
 * Skip the most recent checkpoint (never evict it — it's the freshest snapshot).
 * Skip checkpoints with active RESUME/STEP operations (is_busy flag). */
int checkpoint_to_evict(void);
```

#### MINOR: `checkpoint_do_fork` needs `target_seq` and `cmd_fd`/`result_fd` parameters but no signature is shown (R5-14)

**Location:** C4 fix (line 2143-2148), E5 parent-side code

C4 specifies:
```c
int checkpoint_do_fork(uint64_t sequence_no, PyObject *callback);
```

But this signature doesn't include the pipe file descriptors. The function must create the pipes (`pipe()` call), fork, and return the `cmd_fd`/`result_fd` to the caller (or directly add to the checkpoint store). The full signature should be:
```c
/* Create a checkpoint via fork(). Creates pipes internally.
 * On success: adds entry to checkpoint_store, calls callback(child_pid, sequence_no).
 * Returns 0 on success, -1 on failure (fork failed, timeout, etc.).
 * Called from eval hook with GIL held. */
int checkpoint_do_fork(uint64_t sequence_no, PyObject *checkpoint_callback);
```

The pipes are created inside the function (not passed in), and the function internally manages pre-fork sync, fork, parent/child divergence, and checkpoint store insertion. The caller (eval hook) just needs to know success/failure.

**Fix:** Update C4's signature to clarify that pipes are internal. Also document return values: 0 = success, -1 = failure (caller continues recording without checkpoint).

---

#### Summary of Fifth-Pass Review Issues

| ID | Severity | Title | Location |
|----|----------|-------|----------|
| R5-1 | Bug in review | N12 code has undeclared `rc` variable | N12 fix code (line 2958) |
| R5-2 | Bug in review | R4-6 is wrong — setup.py already has all files | R4-6 (line 3462) |
| R5-3 | Significant | D12 self-contradicts three times — unclear final answer | D12 (line 2567) |
| R5-4 | Significant | Child must not call clear_filters() — undocumented hazard | recorder.c, child init |
| R5-5 | Significant | Child result should only be trusted for locals, not metadata | pipe protocol, replay.py |
| R5-6 | Significant (resolved) | E5 parent holds mutex across GIL release — actually safe | E5 parent code |
| R5-7 | Significant | `timeout` variable in E5/N12 parent code never constructed | E5/N12 code |
| R5-8 | Moderate (resolved) | Child init clears trace but fast-forward re-installs — safe | Child init step 5 |
| R5-9 | Moderate | STEP command end-of-script handling unspecified | Pipe protocol |
| R5-10 | Moderate | RESUME with target <= current_position needs defensive check | Child RESUME handler |
| R5-11 | Moderate | g_frame_count not reset in child (cosmetic) | recorder.c, child init |
| R5-12 | Minor | Ignore filter state persists across test recordings | conftest.py, recorder.c |
| R5-13 | Minor | Exponential thinning algorithm never actually specified | checkpoint_store.c |
| R5-14 | Minor | checkpoint_do_fork signature incomplete (missing pipe creation) | C4 fix |

### Phase 2 Sixth-Pass Review: Specification Gaps That Block Implementation

Sixth-pass review focused exclusively on gaps that would force an implementer to make unguided design decisions. All prior passes found bugs and edge cases but largely assumed the "happy path" C code was specified. This pass checks whether there is enough detail to actually write the code. Issues use prefix R6-.

---

#### CRITICAL: Child target-reached serialization is never specified — no C code to build the pipe result JSON (R6-1)

**Location:** Phase 2 execution flow detail (line 1880), replay.c, checkpoint.c child handler

The plan says "At `target_seq`, the hook/trace function serializes full frame state as JSON, writes to result pipe." But **no plan section or review specifies how the child builds this JSON**. The only serialization function in the codebase is `serialize_locals()` (recorder.c lines 240-306), which produces a JSON object of locals (`{"x": "42", "y": "'hello'"}`). The pipe result requires a *wrapping* JSON envelope:

```json
{"status": "ok", "seq": 750, "file": "foo.py", "line": 42,
 "function_name": "bar", "call_depth": 3, "locals": {"x": "42"}}
```

Building this requires:
1. Extracting `filename`, `line_no`, `function_name` from the current frame (the eval hook has `code`, but the trace function would need to call `PyFrame_GetCode` and `PyFrame_GetLineNumber`)
2. Calling `serialize_locals()` to get the locals JSON string
3. Embedding that string as a raw JSON fragment inside the envelope (NOT as an escaped string — it's already valid JSON)
4. JSON-escaping `filename` and `function_name` (they may contain backslashes on Windows, quotes in generated code)
5. Converting the complete JSON string to bytes and writing via the length-prefixed pipe protocol

Two implementation approaches:

**Approach A (C-only):** Build the JSON string in C using `snprintf`:
```c
static int serialize_target_state(int result_fd, PyFrameObject *frame, char *locals_buf, size_t locals_buf_size) {
    PyCodeObject *code = PyFrame_GetCode(frame);
    int line_no = PyFrame_GetLineNumber(frame);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    const char *funcname = PyUnicode_AsUTF8(code->co_qualname);
    const char *locals_json = serialize_locals((PyObject *)frame, locals_buf, locals_buf_size, NULL, NULL);

    char escaped_filename[512], escaped_funcname[512];
    json_escape_string(filename, escaped_filename, sizeof(escaped_filename));
    json_escape_string(funcname, escaped_funcname, sizeof(escaped_funcname));

    char result[65536 + 1024];
    int len = snprintf(result, sizeof(result),
        "{\"status\": \"ok\", \"seq\": %llu, \"file\": \"%s\", \"line\": %d, "
        "\"function_name\": \"%s\", \"call_depth\": %d, \"locals\": %s}",
        (unsigned long long)g_sequence_counter, escaped_filename, line_no,
        escaped_funcname, g_call_depth, locals_json ? locals_json : "{}");
    Py_DECREF(code);
    /* Write length-prefixed result to pipe */
    uint32_t net_len = htonl((uint32_t)len);
    write_all(result_fd, &net_len, 4);
    write_all(result_fd, result, len);
    return 0;
}
```

**Approach B (Python):** Use Python's `json.dumps()` from C:
```c
/* Build Python dict, call json.dumps, write string to pipe */
PyObject *result_dict = PyDict_New();
/* ... set keys ... */
PyObject *json_mod = PyImport_ImportModule("json");
PyObject *json_str = PyObject_CallMethod(json_mod, "dumps", "O", result_dict);
/* ... write to pipe ... */
```

Approach A is recommended (no Python import needed, works with GIL held, faster). The plan MUST include this function or a reference implementation.

**Also unspecified:** Where does this function get the `PyFrameObject*`? In the trace function, the `frame` parameter is available. In the eval hook, only `iframe` (`_PyInterpreterFrame*`) is available — there's no public API to get a `PyFrameObject*` from it in 3.12+. The eval hook CAN get `code`, `line_no`, and `funcname` from `iframe`, but `serialize_locals()` requires a `PyFrameObject*` (it calls `PyFrame_GetLocals()`). If the target is reached at a `call` event (INC-1 in the eval hook), the implementation needs a way to get the frame object. `PyThreadState_GetFrame(tstate)` returns the current `PyFrameObject*` — this should work since the eval hook is called during frame evaluation.

**Fix:** Add a `serialize_target_state()` function specification to the plan, using Approach A. Document that `PyThreadState_GetFrame(tstate)` is used to get the `PyFrameObject*` in the eval hook.

#### SIGNIFICANT: Checkpoint trigger modulo on non-contiguous call sequence numbers can skip checkpoints entirely (R6-2)

**Location:** N2/N3 checkpoint trigger code (line 3124-3128)

The checkpoint trigger:
```c
if (call_event.sequence_no > 0 &&
    call_event.sequence_no % g_checkpoint_interval == 0) {
```

`g_sequence_counter` increments for ALL event types (call, line, return, exception, exception_unwind). Call events have non-contiguous sequence numbers. Example with `checkpoint_interval=1000`:

```
seq 0:    call (excluded by > 0)
seq 1-99: line, return, exception events
seq 100:  call (100 % 1000 ≠ 0)
...
seq 998:  return
seq 999:  call (999 % 1000 ≠ 0)
seq 1000: line (not in eval hook — modulo check never runs)
seq 1001: call (1001 % 1000 ≠ 0)
...
```

If the typical line/call ratio is ~10 lines per call, a call event hitting exactly `seq % 1000 == 0` requires a call event landing on a multiple of 1000 — roughly a 1-in-10 chance per checkpoint interval. On average, checkpoints are created at ~10× the specified interval. For pathological code (functions with thousands of lines), checkpoints might be created at 100× the interval or never.

**Fix:** Replace the modulo check with a delta-based check:
```c
static uint64_t g_last_checkpoint_seq = 0;

if (g_checkpoint_interval > 0 &&
    g_checkpoint_callback != NULL &&
    call_event.sequence_no > 0 &&
    (call_event.sequence_no - g_last_checkpoint_seq) >= (uint64_t)g_checkpoint_interval) {
    checkpoint_do_fork(call_event.sequence_no, g_checkpoint_callback);
    g_last_checkpoint_seq = call_event.sequence_no;
}
```

This guarantees a checkpoint is created within `checkpoint_interval` events of the last one (plus at most the gap to the next call event). The child inherits `g_last_checkpoint_seq` — since the child doesn't create new checkpoints, this is harmless. Reset `g_last_checkpoint_seq = 0` in `start_recording()`.

#### SIGNIFICANT: `_warm_fallback` crashes with `DoesNotExist` for out-of-range `target_seq` (R6-3)

**Location:** `replay.py` `_warm_fallback()` (line 2023-2028)

```python
frame = ExecutionFrames.get(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.sequence_no == target_seq))
```

Peewee's `.get()` raises `DoesNotExist` when no matching row exists. If the user requests `goto_frame(run_id, target_seq=999999)` and only 5000 frames were recorded, this crashes with an unhandled exception.

This propagates through `ReplayController.goto_frame()` → CLI `_cmd_replay` → unhandled crash with a Peewee traceback (poor UX).

**Fix:** Use `.get_or_none()` and return an error dict:
```python
def _warm_fallback(self, run_id, target_seq) -> dict:
    frame = ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.sequence_no == target_seq))
    if frame is None:
        return {"error": "frame_not_found", "target_seq": target_seq}
    import json
    locals_data = json.loads(frame.locals_snapshot) if frame.locals_snapshot else {}
    return {"seq": target_seq, "file": frame.filename, "line": frame.line_no,
            "function_name": frame.function_name, "call_depth": frame.call_depth,
            "locals": locals_data, "warm_only": True}
```

#### SIGNIFICANT: `checkpoint_store.h` API not updated for Phase 2 requirements from reviews (R6-4)

**Location:** `ext/checkpoint_store.h` (14 lines), D5, D15, N10

Five prior reviews established API requirements that are not reflected in the current header:

1. **D5:** `CheckpointEntry` needs `current_position` field. `checkpoint_store_add()` needs to initialize it. A new `checkpoint_store_update_position(int idx, uint64_t pos)` function is needed.
2. **D15:** `checkpoint_store_find_by_pid(int child_pid)` needed for stale-index recovery after GIL release.
3. **N10:** `recorder.h` needs Phase 2 getters/setters (`recorder_set_fast_forward()`, `recorder_get_sequence_counter()`, `recorder_get_call_depth()`).
4. **D13:** `checkpoint_store_evict(int index)` function for per-checkpoint cleanup.
5. **D18:** `checkpoint_to_evict()` function for the eviction algorithm.

**Fix:** Specify the complete Phase 2 header updates:

```c
/* checkpoint_store.h — Phase 2 complete API */
typedef struct {
    int child_pid;
    int cmd_fd;
    int result_fd;
    uint64_t sequence_no;       /* original position (immutable) */
    uint64_t current_position;  /* updated after RESUME/STEP */
    int is_alive;
    int is_busy;                /* 1 during active RESUME/STEP I/O */
} CheckpointEntry;

void checkpoint_store_init(void);
int  checkpoint_store_add(int child_pid, int cmd_fd, int result_fd, uint64_t sequence_no);
int  checkpoint_store_find_nearest(uint64_t target_seq);  /* uses current_position */
int  checkpoint_store_find_by_pid(int child_pid);         /* D15 */
void checkpoint_store_update_position(int index, uint64_t new_position);
void checkpoint_store_evict(int index);                    /* D13: DIE + waitpid + close fds */
int  checkpoint_to_evict(void);                            /* D18: thinning algorithm */
CheckpointEntry *checkpoint_store_get(int index);          /* accessor */
int  checkpoint_store_count(void);                         /* live count */
```

```c
/* recorder.h — Phase 2 additions */
void recorder_set_fast_forward(int enabled, uint64_t target_seq);
uint64_t recorder_get_sequence_counter(void);
int recorder_get_call_depth(void);
```

#### SIGNIFICANT: `Checkpoint` model has no index on `(run_id, sequence_no)` — `get_nearest_checkpoint` does full table scan (R6-5)

**Location:** `pyttd/models/checkpoints.py` (line 1920-1931), `replay.py` `get_nearest_checkpoint`

The `get_nearest_checkpoint` query:
```python
Checkpoint.select().where(
    (Checkpoint.run_id == run_id) &
    (Checkpoint.sequence_no <= target_seq) &
    (Checkpoint.is_alive == 1)
).order_by(Checkpoint.sequence_no.desc()).first()
```

This does `ORDER BY sequence_no DESC` with a `WHERE` filter on `run_id` and `sequence_no`. Without a composite index, SQLite scans all rows in the `Checkpoint` table. With 32 max checkpoints this is trivially fast, but it's bad practice — especially if R4-11's recommendation (let C handle selection) isn't adopted.

**Fix:** Add index to `Checkpoint`:
```python
class Checkpoint(_BaseModel):
    # ... fields ...
    class Meta:
        indexes = (
            (('run_id', 'sequence_no'), False),
        )
```

#### SIGNIFICANT: Architecture section line 111 still says `STEP -1` — R4-1 documented fix but never applied (R6-6)

**Location:** Architecture section line 111

R4-1 (fourth-pass review) correctly identified that line 111 says `(STEP, -1)` which contradicts the pipe protocol (forward-only). R4-1 says "Remove or correct the architecture section line 111." But reviews only add new sections — the original plan text was never modified. An implementer reading the architecture section top-down would see `STEP -1` as the canonical design before discovering the review fix 2000+ lines later.

**Fix:** Actually modify line 111 to:
> For sequential rewinds (user steps back within a checkpoint window), the system uses **warm-only navigation** (SQLite reads). Step-back is always warm — checkpoint children can only move forward. For forward incremental steps within a warm child's window, the parent sends `(STEP, +N)` commands.

#### MODERATE: `atexit._clear()` is a CPython private API — may break on alternative implementations (R6-7)

**Location:** Child post-fork init step 9 (line 3313-3319)

```c
PyObject *r = PyObject_CallMethod(atexit_mod, "_clear", NULL);
```

`atexit._clear()` is a CPython implementation detail (not documented in the `atexit` module's public API). It works on CPython 3.12+ but could break on PyPy, GraalPy, or future CPython versions that restructure the atexit module.

The error handling (`PyErr_Occurred() → PyErr_Clear()`) is correct — if `_clear` doesn't exist, the call fails silently. But the atexit handlers then remain active, which is the very hazard E6 was trying to prevent.

**Fix:** Use the public API `atexit.unregister()` for known dangerous handlers, or accept the risk and document it:
```c
/* CPython-specific: atexit._clear() is not part of the public atexit API.
 * If this call fails (non-CPython implementation), atexit handlers remain
 * active in the child. Mitigated by: child uses _exit(0) on DIE (skips
 * atexit), and the only atexit risk is from unhandled exceptions during
 * fast-forward (which are caught and suppressed). */
```

Alternatively, since Python >= 3.12 is required (DESIGN.md), and `atexit._clear()` exists in CPython 3.12+, document this as a CPython-only dependency (which pyttd already is, given PEP 523 and `_PyInterpreterFrame`).

#### MODERATE: `_cmd_replay` imports `get_frame_at_seq` and `get_line_code` but never uses them (R6-8)

**Location:** `_cmd_replay` code (line 1941)

```python
from pyttd.query import get_last_run, get_frame_at_seq, get_line_code
```

`get_frame_at_seq` and `get_line_code` are imported but never used in the function body. This will trigger linting warnings and confuses readers about the function's intended behavior.

**Fix:** Remove unused imports:
```python
from pyttd.query import get_last_run
```

#### MODERATE: No specification for how `pyttd_restore_checkpoint` (C code) returns a Python dict (R6-9)

**Location:** `replay.py` `goto_frame`, `ext/replay.c` stub

The plan shows:
```python
result_json = pyttd_native.restore_checkpoint(target_seq)
return result_json  # parsed dict from pipe protocol
```

The comment says "parsed dict from pipe protocol." The C function `pyttd_restore_checkpoint` reads JSON bytes from the result pipe, but must return a Python `dict` to the caller. This requires parsing JSON in C. Two approaches:

1. **Python's `json.loads()`:** Call from C via `PyImport_ImportModule("json")` + `PyObject_CallMethod(json_mod, "loads", "s", json_buf)`. Returns a Python dict. Simple but requires the `json` module import.

2. **Manual dict construction:** Parse the JSON in C and build a `PyDict_New()` with `PyDict_SetItemString()`. More code, avoids Python import overhead.

3. **Return raw string:** Return the JSON string to Python, let the caller parse. Changes the API contract — `replay.py` would need `json.loads(result_str)`.

Approach 1 is simplest and the overhead is negligible (one-time import). Approach 3 changes the API. The plan should specify which approach is used.

**Fix:** Specify approach 1 in the `pyttd_restore_checkpoint` implementation:
```c
/* After reading result from pipe: */
PyObject *json_mod = PyImport_ImportModule("json");
PyObject *result = PyObject_CallMethod(json_mod, "loads", "s", result_buf);
Py_DECREF(json_mod);
if (!result) return NULL;  /* json.loads() failed — malformed JSON */
return result;  /* Python dict */
```

#### MINOR: `g_locals_buf` (static 64KB buffer) in trace function is not available for child's target serialization (R6-10)

**Location:** `ext/recorder.c` line 311, child target serialization

```c
static char g_locals_buf[MAX_LOCALS_JSON_SIZE];
```

`g_locals_buf` is a static buffer used by the trace function for locals serialization during recording. The child inherits it via COW. When the child reaches the target and needs to serialize locals (via `serialize_locals()`), it writes to `g_locals_buf`, triggering a COW page fault (~16 pages for 64KB). This is acceptable but should be documented.

More importantly, R6-1's `serialize_target_state()` needs a buffer for the complete result JSON (locals + envelope). The result could be up to ~65KB (locals) + ~1KB (envelope) = ~66KB. A second static buffer (`g_result_buf`) is needed, or the function must `malloc`/`free` dynamically.

**Fix:** Add a second static buffer for the target result, or use `g_locals_buf` for locals and a stack-allocated buffer for the envelope (since the envelope overhead is small):
```c
/* In serialize_target_state: */
char *locals_json = serialize_locals(frame, g_locals_buf, sizeof(g_locals_buf), NULL, NULL);
char result_buf[sizeof(g_locals_buf) + 1024];
int len = snprintf(result_buf, sizeof(result_buf), "...", ..., locals_json);
```

#### MINOR: No `#include` for `<poll.h>` or `<sys/wait.h>` specified for Phase 2 C files (R6-11)

**Location:** checkpoint.c, checkpoint_store.c, replay.c

Phase 2 uses:
- `fork()` — `<unistd.h>` (already included in recorder.c but not in checkpoint.c stub)
- `pipe()` — `<unistd.h>`
- `waitpid()` — `<sys/wait.h>` (NOT included anywhere)
- `poll()` — `<poll.h>` (D17 timeout mitigation)
- `signal()` / `SIG_IGN` — `<signal.h>` (NOT included anywhere)
- `htonl()` — `<arpa/inet.h>` or `<netinet/in.h>` (for length-prefix byte order)
- `EINTR`, `EPIPE` — `<errno.h>`

**Fix:** Specify required includes for Phase 2 C files:
```c
/* checkpoint.c */
#include <unistd.h>     /* fork, pipe, close, read, write */
#include <sys/wait.h>   /* waitpid */
#include <signal.h>     /* signal, SIG_IGN, SIGPIPE */
#include <errno.h>      /* EINTR, EPIPE */
#include <poll.h>        /* poll (for D17 timeout) */
#include <arpa/inet.h>  /* htonl, ntohl */
```

---

#### Summary of Sixth-Pass Review Issues

| ID | Severity | Title | Location |
|----|----------|-------|----------|
| R6-1 | Critical | Child target serialization function never specified | checkpoint.c, replay.c |
| R6-2 | Significant | Checkpoint trigger modulo on non-contiguous seqs can skip entirely | recorder.c eval hook |
| R6-3 | Significant | `_warm_fallback` crashes on invalid target_seq (DoesNotExist) | replay.py |
| R6-4 | Significant | checkpoint_store.h API not updated for review requirements | checkpoint_store.h |
| R6-5 | Significant | Checkpoint model has no index on (run_id, sequence_no) | checkpoints.py |
| R6-6 | Significant | Architecture line 111 still says STEP -1 — fix never applied | Architecture section |
| R6-7 | Moderate | atexit._clear() is CPython private API | Child init step 9 |
| R6-8 | Moderate | _cmd_replay has unused imports | cli.py plan code |
| R6-9 | Moderate | pyttd_restore_checkpoint JSON-to-dict conversion unspecified | replay.c |
| R6-10 | Minor | g_locals_buf COW + second buffer for result envelope | recorder.c |
| R6-11 | Minor | Missing #include directives for Phase 2 C files | checkpoint.c, replay.c |

### Phase 2 Seventh-Pass Review: Error Recovery, Cross-Review Staleness, and Fd Hygiene

Seventh-pass review focusing on three areas uncovered by the prior six passes: (1) error recovery paths that were never specified (only success paths), (2) staleness and contradictions between reviews written at different times, and (3) resource hygiene issues in the multi-child architecture. All issues use prefix R7-.

---

#### SIGNIFICANT: `checkpoint_do_fork()` fork failure leaves flush thread paused — deadlock (R7-1)

**Location:** E5 parent-side code (lines 3020-3039), `checkpoint.c`

The E5 pre-fork protocol shows the success path only:
```c
pthread_mutex_unlock(&g_flush_mutex);  /* unlock BEFORE fork */
pid_t pid = fork();
/* Parent: */
PyEval_RestoreThread(saved);
pthread_mutex_lock(&g_flush_mutex);
atomic_store(&g_pause_requested, 0);
pthread_cond_signal(&g_resume_cv);
pthread_mutex_unlock(&g_flush_mutex);
```

If `fork()` returns `-1` (failure — `EAGAIN` from too many processes, `ENOMEM` from insufficient memory), the parent must still resume the flush thread. Without cleanup:
1. `g_pause_requested` remains 1
2. Flush thread is blocked on `g_resume_cv` forever
3. No more frames are flushed to DB for the remainder of the recording
4. Ring buffer fills up, all subsequent events are dropped

This is a **silent data loss bug** — the recording continues (eval hook pushes to ring buffer) but the flush thread never drains it.

**Fix:** Add fork failure handling:
```c
pthread_mutex_unlock(&g_flush_mutex);  /* unlock BEFORE fork */
pid_t pid = fork();
if (pid < 0) {
    /* Fork failed — resume flush thread and re-acquire GIL */
    PyEval_RestoreThread(saved);
    pthread_mutex_lock(&g_flush_mutex);
    atomic_store(&g_pause_requested, 0);
    pthread_cond_signal(&g_resume_cv);
    pthread_mutex_unlock(&g_flush_mutex);
    return -1;  /* caller continues recording without checkpoint */
}
```

Also handle pipe creation failure (the `pipe()` calls before the pre-fork sync can fail with `EMFILE`/`ENFILE`). If pipes fail, skip the pre-fork sync entirely and return -1.

The eval hook must tolerate `checkpoint_do_fork()` returning -1 gracefully — continue recording as if no checkpoint was attempted.

#### SIGNIFICANT: `ringbuf_push()` dereferences NULL `g_rb.events` after child's `ringbuf_destroy()` — no guard (R7-2)

**Location:** `ext/ringbuf.c` lines 112-160, child post-fork init step 7 (line 3304)

After `ringbuf_destroy()` in the child, `g_rb.events = NULL` and `g_rb.initialized = 0`. But `ringbuf_push()` never checks either field:
```c
int ringbuf_push(const FrameEvent *event) {
    uint32_t head = atomic_load_explicit(&g_rb.head, memory_order_relaxed);
    uint32_t tail = atomic_load_explicit(&g_rb.tail, memory_order_acquire);
    // ...
    uint32_t idx = head & g_rb.mask;       /* g_rb.mask is 0 after destroy */
    FrameEvent *slot = &g_rb.events[idx];  /* g_rb.events is NULL → SIGSEGV */
```

The child SHOULD never call `ringbuf_push()` because:
- N8+N9 Solution B checks `g_fast_forward` first → fast-forward path skips `ringbuf_push`
- `g_recording = 0` → normal recording path is unreachable

But this relies on TWO independent flags being correctly set. If either is wrong (implementation bug, race condition), the child crashes with a NULL dereference — an opaque SIGSEGV with no diagnostic message.

**Fix:** Add a guard at the top of `ringbuf_push()`:
```c
int ringbuf_push(const FrameEvent *event) {
    if (!g_rb.initialized) return PYTTD_RINGBUF_ERROR;
    // ... existing code ...
}
```

This is defense-in-depth — the guard should never trigger in correct operation, but prevents crashes from implementation bugs. Apply the same guard to `ringbuf_pop_batch()`, `ringbuf_pool_copy()`, and `ringbuf_fill_percent()`.

#### SIGNIFICANT: Target-reached serialization missing `__return__` and `__exception__` event-type-specific locals (R7-3)

**Location:** R6-1's `serialize_target_state()` (line 4031-4054), `ext/recorder.c` trace function (lines 367-370, 396-401)

During recording, the trace function adds special keys to locals:
- `PyTrace_RETURN` (line 367-370): adds `__return__` key with the return value
- `PyTrace_EXCEPTION` (line 396-401): adds `__exception__` key with the exception value

R6-1's `serialize_target_state()` calls `serialize_locals(frame, locals_buf, locals_buf_size, NULL, NULL)` — always passing `NULL` for `extra_key`/`extra_val`. If the target frame is a `return` event, the cold result is missing `__return__`. If it's an `exception` event, it's missing `__exception__`.

The warm fallback reads `locals_snapshot` from the DB, which DOES include these keys (the trace function serialized them during recording). This creates a **silent inconsistency** between cold and warm locals for the same frame:
- Warm: `{"x": "42", "__return__": "True"}`
- Cold: `{"x": "42"}` ← missing `__return__`

Phase 3's `variablesRequest` handler would show different variables depending on whether the user reached the frame via warm or cold navigation.

**Fix:** The child's target-reached handler must detect the event type and pass the appropriate extra key/value to `serialize_locals()`:

```c
static int serialize_target_state(int result_fd, int event_type, PyObject *trace_arg) {
    PyThreadState *tstate = PyThreadState_Get();
    PyFrameObject *frame = PyThreadState_GetFrame(tstate);
    // ... get code, filename, funcname, line_no ...

    PyObject *extra_key = NULL, *extra_val = NULL;
    if (event_type == PyTrace_RETURN && trace_arg != NULL) {
        extra_key = PyUnicode_FromString("__return__");
        extra_val = trace_arg;
    } else if (event_type == PyTrace_EXCEPTION && trace_arg != NULL) {
        extra_key = PyUnicode_FromString("__exception__");
        if (PyTuple_Check(trace_arg) && PyTuple_GET_SIZE(trace_arg) >= 2)
            extra_val = PyTuple_GET_ITEM(trace_arg, 1);
    }

    const char *locals_json = serialize_locals(
        (PyObject *)frame, g_locals_buf, sizeof(g_locals_buf),
        extra_key, extra_val);
    Py_XDECREF(extra_key);
    Py_DECREF(frame);
    // ... build and write JSON envelope ...
}
```

The `event_type` and `trace_arg` must be passed down from the trace function's fast-forward target-hit path. For `call` and `exception_unwind` events (handled in the eval hook), no extra key is needed (`call` has no return value, `exception_unwind` has no accessible locals per DESIGN.md).

#### SIGNIFICANT: Child end-of-script must enter permanent command loop to prevent Python shutdown (R7-4)

**Location:** C5 (line 2150-2169), C6 (line 2171-2187), checkpoint.c child execution

C5 specifies `checkpoint_wait_for_command()` for when the child reaches the target. C6 specifies error handling when the script ends before the target. But neither addresses the **architectural invariant** that the child must NEVER return control to the Python interpreter's normal exit sequence.

The child's execution flow:
1. Fork → child init → `read(cmd_pipe)` blocks in `checkpoint_do_fork()`
2. RESUME arrives → `checkpoint_do_fork()` returns to eval hook
3. Eval hook: install trace → call `g_original_eval` → fast-forward
4. Target reached (in trace func): serialize, write result, `checkpoint_wait_for_command()`
5. STEP/RESUME: update target, trace function returns 0, execution continues
6. **Script completes**: all frames return, `g_original_eval` returns to eval hook

At step 6, the eval hook processes the return and returns `result` to the interpreter. The interpreter continues executing the parent frame's code. Eventually the top-level module finishes, and **Python begins its shutdown sequence** (`Py_FinalizeEx` → `atexit` handlers → module cleanup → process exit).

E6's `atexit._clear()` mitigates atexit hazards, but the fundamental problem is: the child process exits normally, which is wasteful (cleanup for a disposable snapshot) and potentially dangerous (module `__del__` methods, C extension `Py_FinalizeEx` callbacks).

**Fix:** The child must intercept end-of-script at two points:

**Point A — target reached in trace function, script continues past target:**
Already handled by C5's `checkpoint_wait_for_command()` blocking.

**Point B — script ends during fast-forward (before or after reaching target):**
After `g_original_eval` returns for the checkpoint frame in the eval hook, the child must NOT return the result to the interpreter. Instead:
```c
/* In eval hook, after g_original_eval returns and exception_unwind is processed: */
if (g_fast_forward) {
    /* Script ended during fast-forward — enter permanent command loop */
    if (g_sequence_counter < g_fast_forward_target) {
        /* C6: target unreachable */
        serialize_error_result(g_result_fd, "target_seq_unreachable",
                               g_sequence_counter);
    }
    /* Permanent loop — only DIE exits (via _exit) */
    while (1) {
        int cmd = checkpoint_wait_for_command(g_cmd_fd);
        /* RESUME/STEP: impossible (script completed) → write error */
        serialize_error_result(g_result_fd, "script_completed",
                               g_sequence_counter);
    }
    /* Unreachable — checkpoint_wait_for_command calls _exit(0) on DIE */
}
```

This prevents the child from ever returning to the interpreter loop after the script finishes. The child stays in the command loop until DIE, then calls `_exit(0)`.

**Also applies to STEP:** If a STEP command causes the script to finish (step 5 → 6 transition), the same checkpoint-frame eval hook catches the return and enters the permanent loop.

#### SIGNIFICANT: Consolidated child post-fork init sequence is stale — missing Reviews 5-6 additions (R7-5)

**Location:** Third-pass consolidated sequence (lines 3270-3324)

The third-pass review consolidated all child init requirements from reviews 1-3 into a 10-step sequence (lines 3270-3324). But reviews 5 and 6 added requirements that are NOT reflected in the consolidated sequence:

1. **R5-11:** `g_frame_count = 0` should be added to step 3 (alongside `g_inside_repr = 0`). Currently missing — the child inherits the parent's frame count, which is cosmetic but misleading if `get_recording_stats()` is ever called in the child.

2. **R5-4:** A comment warning against calling `clear_filters()` or `stop_recording()` should be added after step 3. Currently missing — the hazard is documented only in R5-4's text, not in the consolidated code.

3. **R6-2:** `g_last_checkpoint_seq = 0` should be reset in child init (the child inherits the parent's value). While the child doesn't create checkpoints, resetting it prevents confusion if the child's store state is inspected.

An implementer using the consolidated sequence as the definitive reference will produce an incomplete initialization.

**Fix:** Update the consolidated sequence to incorporate all post-Review-3 additions, or add a note: "See R5-4, R5-11, R6-2 for additional child init requirements not reflected here."

Recommended updated step 3:
```c
/* 3. Disable recording state (D16, N8+N9) */
g_recording = 0;
g_flush_thread_created = 0;
g_fast_forward = 0;             /* set to 1 on RESUME */
g_inside_repr = 0;              /* E9 */
g_frame_count = 0;              /* R5-11 */
g_last_checkpoint_seq = 0;      /* R6-2 */
/* NOTE: Do NOT call stop_recording() or clear_filters() — R5-4 */
```

#### SIGNIFICANT: R4-15 "sections A-E preserved verbatim" contradicts N8+N9 Solution B (R7-6)

**Location:** R4-15 (line 3638-3656), N8+N9 Solution B (lines 2876-2895), fast-forward correctness proof (lines 3244-3268)

R4-15 states:
> Sections A-E of the eval hook (recording check, stop request, code extraction, ignore filter, thread check) must be preserved verbatim — they control whether the counter increments at all

N8+N9 Solution B states:
> Check `g_fast_forward` BEFORE `g_recording` ... route to `pyttd_eval_hook_fast_forward()`

These contradict: Section A IS the `g_recording` check (line 431). Solution B explicitly REPLACES Section A with a `g_fast_forward` check and routes to a completely separate function. "Preserved verbatim" cannot be true.

The fast-forward correctness proof (line 3256-3262) clarifies the actual requirement: the **gating conditions** that control whether the counter increments must produce identical decisions in recording and fast-forward. This means:
- Section A: **DIFFERENT** — recording uses `g_recording`, fast-forward uses `g_fast_forward`
- Section B (stop request): **OPTIONAL** — `g_stop_requested` is always 0 in child (C3 cleared it, signals are ignored). Including it is harmless but dead code. Recommend including for structural symmetry.
- Section C (code extraction): **IDENTICAL** — `PyUnstable_InterpreterFrame_GetCode`, `PyUnicode_AsUTF8`
- Section D (ignore filter): **IDENTICAL** — `should_ignore(filename, funcname)`
- Section E (thread check): **IDENTICAL** — `PyThread_get_thread_ident() != g_main_thread_id`

**Fix:** Replace R4-15's "preserved verbatim" with precise language:
> Sections C-E of the eval hook (code extraction, ignore filter, thread check) must be replicated identically in the fast-forward eval hook. Section A is replaced by the `g_fast_forward` check (per N8+N9 Solution B). Section B (stop request) may be included for structural symmetry but is dead code in the child.

Also specify the fast-forward trace function's gating conditions:
- `g_fast_forward` check: **NEW** (replaces `g_recording` check at line 316)
- `PyTrace_CALL` skip (line 319-321): **IDENTICAL** — still skip
- `arg == NULL` skip for `PyTrace_RETURN` (line 356-358): **IDENTICAL** — still skip
- Counter increments for `line`, `return`, `exception`: **IDENTICAL** — still increment
- Serialization/ringbuf/timing: **SKIPPED** — the fast-forward path only increments the counter

#### SIGNIFICANT: Checkpoint children inherit pipe fds from ALL prior checkpoints — fd and kernel buffer leak (R7-7)

**Location:** checkpoint.c fork, checkpoint_store.c, C2 child fd cleanup (line 2106-2118)

Each checkpoint creation calls `pipe()` twice (cmd_pipe, result_pipe), then `fork()`. The child inherits ALL open fds from the parent — including pipe fds from every prior checkpoint still alive in the store.

With `checkpoint_interval=1000` and 32 max checkpoints:
- Checkpoint #1: child inherits 0 extra pipe fds
- Checkpoint #2: child inherits 2 fds (child #1's cmd_fd + result_fd)
- ...
- Checkpoint #32: child inherits 62 fds (from children #1-#31)

Problems:
1. **Fd waste:** Each child holds 2 * (N-1) dangling pipe fds (where N is the checkpoint index). Total across all 32 children: ~992 extra fds. Not critical but wasteful.

2. **Kernel buffer pinning:** The kernel maintains a pipe buffer (~64KB on Linux, 16 pages) for each pipe as long as ANY process holds the fd. When checkpoint #5 is evicted (parent closes its fds, child is killed), the kernel CANNOT free the pipe buffer if children #6-#32 still hold inherited copies of #5's fds. With 32 children, evicted pipes stay allocated until all later children are killed.

3. **Pipe write behavior:** If the parent sends DIE to a child and the child exits, but a later child still holds the child's result_fd (inherited), the parent's `close(result_fd)` releases its reference but the kernel keeps the pipe alive. Not harmful (the parent reads from its own fd copy) but adds to resource pressure.

C2 (line 2106-2118) says "Close unneeded pipe ends" but only lists the current checkpoint's unneeded ends (write end of cmd_pipe, read end of result_pipe). It does NOT mention closing prior checkpoints' fds.

**Fix:** Before each `fork()`, collect all existing checkpoint fds from the store. In the child's init sequence, close them all:

```c
/* In checkpoint_do_fork(), before fork: */
int fds_to_close[MAX_CHECKPOINTS * 2];
int n_fds = checkpoint_store_get_all_fds(fds_to_close);

pid_t pid = fork();
if (pid == 0) {
    /* Child: close all prior checkpoint pipe fds */
    for (int i = 0; i < n_fds; i++) {
        close(fds_to_close[i]);
    }
    /* ... rest of child init ... */
}
```

Add `checkpoint_store_get_all_fds(int *out_fds)` to the `checkpoint_store.h` API:
```c
/* Populate out_fds with all cmd_fd and result_fd values from live entries.
 * Returns the number of fds written. out_fds must have space for MAX_CHECKPOINTS * 2. */
int checkpoint_store_get_all_fds(int *out_fds);
```

This is collected BEFORE fork (from the parent's store) so the child has the list immediately after fork without needing to read shared memory.

---

#### MODERATE: 64-bit byte order conversion for pipe protocol not portable (R7-8)

**Location:** Pipe command protocol (line 1888-1906), R6-1's `serialize_target_state()` (line 4050)

The pipe protocol uses 8-byte big-endian uint64 for target_seq payloads. The parent writes and the child reads these values. The code must convert between host and big-endian byte order.

R6-1 uses `htonl()` for the 4-byte result length — this is standardized in `<arpa/inet.h>`. But for 64-bit values:
- `htobe64()` / `be64toh()`: available on Linux (`<endian.h>`) and macOS (`<libkern/OSByteOrder.h>` as `OSSwapHostToBigInt64`), but NOT POSIX-standardized
- `htonll()` / `ntohll()`: even less portable (BSD-specific)
- No portable function exists in C99/C11 or POSIX for 64-bit byte swap

The current code base uses `<arpa/inet.h>` implicitly (R6-11 adds it for `htonl`). But no header provides `htobe64()` portably.

**Fix:** Define a portable 64-bit byte swap in `platform.h`:
```c
#include <arpa/inet.h>  /* htonl, ntohl */

static inline uint64_t pyttd_htobe64(uint64_t host) {
    uint32_t hi = htonl((uint32_t)(host >> 32));
    uint32_t lo = htonl((uint32_t)(host & 0xFFFFFFFF));
    return ((uint64_t)lo << 32) | hi;
}

static inline uint64_t pyttd_be64toh(uint64_t big) {
    uint32_t hi = ntohl((uint32_t)(big >> 32));
    uint32_t lo = ntohl((uint32_t)(big & 0xFFFFFFFF));
    return ((uint64_t)lo << 32) | hi;
}
```

Use `pyttd_htobe64` / `pyttd_be64toh` in the pipe protocol code instead of system-specific functions.

#### MODERATE: Pre-fork protocol doesn't flush ring buffer — stale DB during Phase 3 live debugging (R7-9)

**Location:** E5 pre-fork protocol (lines 3020-3039), flush_batch() (line 546)

The pre-fork sync pauses the flush thread, but does NOT trigger a final flush before pausing. Events pushed to the ring buffer between the last flush cycle and the pause are not in the DB:

```
Timeline:
  [flush completes] → [eval hook pushes events A, B, C] → [checkpoint trigger] → [pre-fork sync]
  Events A, B, C are in ring buffer but NOT in DB.
  [fork] → [parent resumes flush thread] → [flush thread runs → A, B, C flushed to DB]
```

The delay between fork and the next flush is at most `flush_interval_ms` (10ms). In Phase 2 (CLI-only), this is harmless — warm fallback only happens after recording completes (all events flushed).

But in Phase 3 (server mode with live debugging), the user can navigate during recording. If they request a warm fallback to event B (still in ring buffer, not yet flushed), `ExecutionFrames.get()` returns `DoesNotExist`.

**Fix:** In Phase 3's checkpoint integration, trigger a forced flush before the pre-fork sync:
```c
/* In checkpoint_do_fork(), before pre-fork sync: */
flush_batch();  /* drain ring buffer to DB — GIL is held */
```

This ensures all events up to the checkpoint are in the DB before forking. The overhead is small (one extra `flush_batch()` per checkpoint — typically <1ms). Document this as a Phase 3 requirement, not Phase 2 (Phase 2 CLI doesn't need it).

Alternatively, the Phase 3 session layer can handle `DoesNotExist` gracefully by retrying after a short delay or reading directly from the ring buffer.

---

#### MINOR: R6-1's `snprintf` can truncate result JSON — no truncation check (R7-10)

**Location:** R6-1 `serialize_target_state()` code (lines 4042-4047)

```c
char result[65536 + 1024];
int len = snprintf(result, sizeof(result),
    "{\"status\": \"ok\", \"seq\": %llu, \"file\": \"%s\", \"line\": %d, "
    "\"function_name\": \"%s\", \"call_depth\": %d, \"locals\": %s}",
    ...);
/* Write length-prefixed result to pipe */
uint32_t net_len = htonl((uint32_t)len);
write_all(result_fd, &net_len, 4);
write_all(result_fd, result, len);
```

If `snprintf` truncates (total formatted length > sizeof(result) - 1), `len` is the length that WOULD have been written (per C99 `snprintf` semantics). The code then:
1. Sends `net_len = len` (the untrunacted length) as the 4-byte prefix
2. Writes `len` bytes from `result` — but only `sizeof(result) - 1` bytes are valid

The parent reads `len` bytes, but the last `len - sizeof(result) + 1` bytes are garbage (reading past the buffer — undefined behavior).

Truncation is unlikely for most frames (64KB locals + 1KB envelope ≈ 65KB, buffer is 66KB). But pathological cases (long filenames, function names with many Unicode characters after JSON escaping) could trigger it.

**Fix:** Check for truncation:
```c
int len = snprintf(result, sizeof(result), ...);
if (len < 0 || (size_t)len >= sizeof(result)) {
    /* Truncated — send error instead of invalid JSON */
    const char *err = "{\"status\": \"error\", \"error\": \"result_too_large\"}";
    uint32_t err_len = htonl((uint32_t)strlen(err));
    write_all(result_fd, &err_len, 4);
    write_all(result_fd, err, strlen(err));
    return -1;
}
```

#### MINOR: `checkpoint_store_get_all_fds` missing from R6-4's complete header specification (R7-11)

**Location:** R6-4 complete API (lines 4157-4178)

R6-4 specifies the complete Phase 2 `checkpoint_store.h` API. R7-7's fix introduces `checkpoint_store_get_all_fds()` which is not in R6-4's list. R6-4 should be considered incomplete — it was written before R7-7 identified the fd inheritance issue.

**Fix:** Add to the R6-4 API:
```c
int checkpoint_store_get_all_fds(int *out_fds);  /* R7-7: for child fd cleanup */
```

---

#### Summary of Seventh-Pass Review Issues

| ID | Severity | Title | Location |
|----|----------|-------|----------|
| R7-1 | Significant | Fork failure leaves flush thread paused — deadlock | checkpoint.c, E5 protocol |
| R7-2 | Significant | ringbuf_push NULL deref after child's ringbuf_destroy | ringbuf.c, checkpoint.c |
| R7-3 | Significant | Target serialization missing __return__/__exception__ | serialize_target_state, R6-1 |
| R7-4 | Significant | Child end-of-script must loop to prevent Python shutdown | checkpoint.c eval hook |
| R7-5 | Significant | Consolidated child init sequence stale after R5/R6 | Third-pass line 3270 |
| R7-6 | Significant | R4-15 "verbatim" contradicts N8+N9 Solution B | Fast-forward gating |
| R7-7 | Significant | Children inherit prior checkpoints' pipe fds — fd leak | checkpoint.c, checkpoint_store.c |
| R7-8 | Moderate | 64-bit byte order conversion not portable | pipe protocol, platform.h |
| R7-9 | Moderate | Pre-fork doesn't flush ring buffer — stale DB in Phase 3 | checkpoint.c, flush_batch |
| R7-10 | Minor | R6-1's snprintf truncation produces invalid JSON | serialize_target_state |
| R7-11 | Minor | checkpoint_store_get_all_fds missing from R6-4 API | checkpoint_store.h |

---

## Phase 3: JSON-RPC Server + Debug Adapter (First DAP Connection)

**Goal:** Build the communication bridge. Debug Adapter (TS, inline) spawns the Python backend, connects via TCP for JSON-RPC. First end-to-end: F5 records script, VSCode shows call stack + variables. All standard DAP handlers implemented for forward navigation.

### Python side

**`server.py`:**

The server is the Python backend process. Entry point is `python -m pyttd serve --script <path> --cwd <dir>`.

Startup sequence:
1. Parse CLI args (script path, cwd, checkpoint interval)
2. Bind TCP on `localhost:0` (OS-assigned port) using `socket.AF_INET` (IPv4 — avoids IPv6 ambiguity on dual-stack systems; the adapter connects to `127.0.0.1`)
3. Write `PYTTD_PORT:<port>\n` to stdout (handshake with debug adapter)
4. Redirect stdout/stderr via `os.dup2()` to capture pipes (see stdout/stderr capture section below)
5. Accept single TCP connection from the debug adapter
6. Enter RPC event loop (see Server Concurrency Model in Architecture section)

**Event loop integration:** The RPC event loop uses `selectors.DefaultSelector` to multiplex the TCP socket, stdout/stderr capture pipes, and an internal wakeup pipe. **Do NOT set the socket to non-blocking mode** — `selectors.DefaultSelector` works with blocking sockets (it returns only when I/O is ready, so `recv()` won't block). Keeping the socket in blocking mode avoids `BlockingIOError` from `sendall()` when the kernel send buffer is full (JSON-RPC responses are small, so this is unlikely, but non-blocking `sendall()` would require a write buffer and write-readiness polling). When the selector reports the socket is readable, the loop calls `sock.recv(4096)`, feeds the data to `JsonRpcConnection.feed(data)`, then drains complete messages via `while msg := rpc.try_read_message(): dispatch(msg)`. See `protocol.py` implementation above for the `feed()` / `try_read_message()` API.

For inter-thread communication (recording thread → RPC thread), create an `os.pipe()` pair: the recording thread writes a single byte to the wakeup pipe to signal the RPC thread (e.g., when recording completes). The RPC thread registers the wakeup pipe's read end with the selector alongside the TCP socket and capture pipes. This avoids polling a `queue.Queue` and provides efficient event-driven dispatch. Messages are passed via a `queue.Queue`, but the selector-registered wakeup pipe ensures the RPC thread unblocks immediately when a message is posted.

**`_cmd_serve()` in `cli.py`:** Phase 3 replaces the serve stub with:
```python
def _cmd_serve(args):
    from pyttd.server import PyttdServer
    server = PyttdServer(
        script=args.script,
        is_module=args.module,
        cwd=args.cwd,
        checkpoint_interval=args.checkpoint_interval,
    )
    server.run()  # blocks until disconnect or signal
```

**Recording thread implementation:**
```python
def _recording_thread_main(self):
    """Run in a separate thread. Executes the user script with recording active."""
    error_info = None
    try:
        if self.is_module:
            self.runner.run_module(self.script, self.cwd, self._script_args)
        else:
            self.runner.run_script(self.script, self.cwd, self._script_args)
    except BaseException as e:
        # Catch ALL exceptions including SystemExit and KeyboardInterrupt
        import traceback
        error_info = {"type": type(e).__name__, "message": str(e),
                      "traceback": traceback.format_exc()}
    finally:
        stats = self.recorder.stop()
        # Post message to RPC thread via queue + wakeup pipe
        self._msg_queue.put({"type": "recording_complete", "stats": stats, "error": error_info})
        os.write(self._wakeup_w, b'\x00')  # wake up selector
```

User script exceptions should NOT crash the server — they are expected events that end the recording phase. `SystemExit` (from `sys.exit()` in user code) and `KeyboardInterrupt` (from the C `request_stop()` interrupt mechanism) are both caught. The `recorder.stop()` call in `finally` ensures recording is properly finalized regardless of how the script exited.

**`PyttdServer` class skeleton:**
```python
import os
import sys
import socket
import signal
import selectors
import threading
import queue
import logging
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.runner import Runner
from pyttd.session import Session
from pyttd.protocol import JsonRpcConnection
from pyttd.models.constants import DB_NAME_SUFFIX
from pyttd.models.storage import delete_db_files

class PyttdServer:
    def __init__(self, script: str, is_module: bool = False, cwd: str = '.', checkpoint_interval: int = 1000):
        self.script = script
        self.is_module = is_module
        self.cwd = os.path.abspath(cwd)
        self.config = PyttdConfig(checkpoint_interval=checkpoint_interval)
        self.recorder = Recorder(self.config)
        self.runner = Runner()
        self.session = Session()
        self._sel = selectors.DefaultSelector()
        self._wakeup_r, self._wakeup_w = os.pipe()
        os.set_blocking(self._wakeup_r, False)  # Non-blocking read — selector reports readability
        self._msg_queue = queue.Queue()
        self._recording_thread = None
        self._recording = False
        self._shutdown = False
        # Compute DB path
        if is_module:
            script_name = script.replace('.', '_')
            self._db_path = os.path.join(self.cwd, script_name + DB_NAME_SUFFIX)
        else:
            script_abs = os.path.abspath(script)
            script_name = os.path.splitext(os.path.basename(script_abs))[0]
            self._db_path = os.path.join(os.path.dirname(script_abs) or '.', script_name + DB_NAME_SUFFIX)
        # Launch config overrides (set by `launch` RPC before recording starts)
        self._script_args = []  # set from launch RPC params.args
        # Stdout/stderr capture state
        self._saved_stdout = None
        self._saved_stderr = None
        self._capture_r_stdout = None
        self._capture_r_stderr = None

    def _setup_capture(self):
        """Redirect stdout/stderr to pipes for capture. Must be called AFTER port handshake."""
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()
        self._saved_stdout = os.dup(1)
        self._saved_stderr = os.dup(2)
        os.dup2(w_out, 1)
        os.dup2(w_err, 2)
        os.close(w_out)  # Close original write-end fd (fd 1 is now the write end via dup2)
        os.close(w_err)  # Close original write-end fd (fd 2 is now the write end via dup2)
        self._capture_r_stdout = r_out
        self._capture_r_stderr = r_err
        # Set capture read ends to non-blocking — the selector reports readability,
        # but data could be consumed between select() and read() on some platforms.
        # Non-blocking os.read() returns b'' instead of blocking in that case.
        os.set_blocking(r_out, False)
        os.set_blocking(r_err, False)
        # Fix buffering: Python switches to full buffering when stdout is a pipe
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)

    def _restore_capture(self):
        """Restore original stdout/stderr. Called during shutdown.
        MUST unregister capture fds from selector BEFORE closing them —
        closing an fd still registered in epoll/kqueue causes undefined behavior."""
        if self._capture_r_stdout is not None:
            try: self._sel.unregister(self._capture_r_stdout)
            except (KeyError, ValueError): pass
            os.close(self._capture_r_stdout)
            self._capture_r_stdout = None
        if self._capture_r_stderr is not None:
            try: self._sel.unregister(self._capture_r_stderr)
            except (KeyError, ValueError): pass
            os.close(self._capture_r_stderr)
            self._capture_r_stderr = None
        if self._saved_stdout is not None:
            os.dup2(self._saved_stdout, 1)
            os.close(self._saved_stdout)
            self._saved_stdout = None
        if self._saved_stderr is not None:
            os.dup2(self._saved_stderr, 2)
            os.close(self._saved_stderr)
            self._saved_stderr = None

    def run(self):
        """Main entry point — bind TCP, write port, capture stdout/stderr, enter event loop."""
        # 1. Bind TCP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        # 2. Write port handshake (BEFORE stdout capture)
        sys.stdout.write(f"PYTTD_PORT:{port}\n")
        sys.stdout.flush()
        # 3. Capture stdout/stderr
        self._setup_capture()
        # 4. Install signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        # 5. Accept connection (30s timeout — adapter should connect promptly after reading port)
        sock.settimeout(30)
        try:
            conn, _ = sock.accept()
        except socket.timeout:
            logging.getLogger(__name__).error("Timeout waiting for adapter connection")
            sock.close()
            return
        sock.settimeout(None)
        self._rpc = JsonRpcConnection(conn)
        # 6. Register with selector
        self._sel.register(conn, selectors.EVENT_READ, 'rpc')
        self._sel.register(self._wakeup_r, selectors.EVENT_READ, 'wakeup')
        if self._capture_r_stdout:
            self._sel.register(self._capture_r_stdout, selectors.EVENT_READ, 'stdout')
        if self._capture_r_stderr:
            self._sel.register(self._capture_r_stderr, selectors.EVENT_READ, 'stderr')
        # 7. Event loop
        self._event_loop()
        # 8. Cleanup
        self._restore_capture()
        try: self._sel.unregister(conn)
        except (KeyError, ValueError): pass
        try: self._sel.unregister(self._wakeup_r)
        except (KeyError, ValueError): pass
        self._sel.close()
        conn.close()
        sock.close()
        os.close(self._wakeup_r)
        os.close(self._wakeup_w)

    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM. Interrupt recording if active, then trigger shutdown."""
        if self._recording:
            import pyttd_native
            pyttd_native.request_stop()
        self._shutdown = True
        # Wake the selector so the event loop exits promptly
        try:
            os.write(self._wakeup_w, b'\x01')
        except OSError:
            pass
```

The `_event_loop()` method uses `self._sel.select(timeout=0.5)` during recording (to send periodic progress notifications) and `self._sel.select(timeout=1.0)` during replay (periodic timeout allows checking `self._shutdown` flag set by signal handlers). On each iteration, it processes readable file descriptors: RPC socket data is fed to `self._rpc.feed(data)` then drained via `while msg := self._rpc.try_read_message(): self._dispatch(msg)`. **EOF detection:** after `feed()`, check `self._rpc.is_closed` — if `True`, the adapter disconnected; trigger graceful shutdown (equivalent to receiving a `disconnect` RPC). Wakeup pipe data triggers message queue processing. Capture pipe data triggers output notifications. The `_dispatch(msg)` method routes JSON-RPC requests by method name to handler methods.

**Event loop exit conditions:** The loop exits when `self._shutdown` is `True` (set by signal handler, disconnect RPC, or EOF detection). After exit, `run()` performs cleanup (restore capture, close selector, close connections, close wakeup pipe). If the recording thread is still running at exit, the loop calls `pyttd_native.request_stop()` and joins the recording thread with a 2s timeout before proceeding with cleanup.

JSON-RPC methods handled:

| Method | Phase | Description |
|---|---|---|
| `backend_init` | 3 | Returns server capabilities: `{"version": "0.1.0", "capabilities": [...]}`. Capability strings include `"recording"`, `"warm_navigation"`, and (if `PYTTD_HAS_FORK`) `"cold_navigation"`, `"checkpoints"`. The adapter logs these but does not gate behavior on them in Phase 3 (all navigation is warm). Named `backend_init` (not `initialize`) to avoid confusion with DAP's own `initialize` request. |
| `launch` | 3 | Stores supplemental config from the adapter: `args` (script arguments, default `[]`), `checkpointInterval` (override, optional), and `traceDb` (custom DB path, optional — overrides computed path). The `script` and `cwd` are already known from CLI args; the `launch` RPC provides adapter-side overrides. Does NOT start recording yet. |
| `configuration_done` | 3 | Starts the recording thread — executes user script with C hook active |
| `set_breakpoints` | 3 | Stores breakpoint list for replay navigation |
| `set_exception_breakpoints` | 3 | Stores exception filter settings. `"raised"` filter: `continue_forward` and `reverse_continue` also stop on `frame_event == 'exception'` events. `"uncaught"` filter: stop on `frame_event == 'exception_unwind' AND call_depth == 0`. See query patterns in `session.py` for details. |
| `interrupt` | 3 | Stops recording early (calls `pyttd_native.request_stop()` which sets atomic flag in C) |
| `get_threads` | 3 | Returns `[{id: 1, name: "Main Thread"}]` |
| `get_stack_trace` | 3 | Calls `session.get_stack_at(seq)` |
| `get_scopes` | 3 | Takes `seq` param (from DAP `frameId`). Returns `[{name: "Locals", variablesReference: seq + 1}]` (see `scopesRequest` encoding in DAP handlers). |
| `get_variables` | 3 | Calls `session.get_variables_at(seq, scope)` |
| `evaluate` | 3 | Calls `session.evaluate_at(seq, expression, context)` |
| `continue` | 3 | Calls `session.continue_forward()` (reads breakpoints + exception filters from session state) |
| `next` | 3 | Calls `session.step_over()` |
| `step_in` | 3 | Calls `session.step_into()` |
| `step_out` | 3 | Calls `session.step_out()` |
| `disconnect` | 3 | Graceful shutdown: (1) if recording is still active, call `pyttd_native.request_stop()` and wait for recording thread to finish (with 2s timeout), (2) call `pyttd_native.kill_all_checkpoints()` if checkpoints exist, (3) call `recorder.cleanup()` (closes DB), (4) restore original stdout/stderr via `os.dup2(saved_stdout, 1)` and `os.dup2(saved_stderr, 2)`, close saved fd's and capture pipes, (5) close TCP socket, (6) exit |
| `step_back` | 4 | Calls `session.step_back()` |
| `reverse_continue` | 4 | Calls `session.reverse_continue()` (reads breakpoints + exception filters from session state) |
| `goto_frame` | 4 | Calls `session.goto_frame(seq)` |
| `goto_targets` | 4 | Returns available targets for a file:line |
| `get_timeline_summary` | 5 | Calls `timeline.get_timeline_summary(...)` |
| `get_execution_stats` | 6 | Calls `session.get_execution_stats(filename)` |
| `get_traced_files` | 6 | Returns set of filenames that appear in the trace |
| `get_call_children` | 6 | Calls `session.get_call_children(...)` |

**`protocol.py`:** Content-Length message framing over TCP (matching DAP/LSP wire format):
```
Content-Length: <length>\r\n
\r\n
<JSON-RPC payload>
```
Request ID correlation for matching responses. Supports both request/response and notification patterns.

**Important:** The server uses `selectors.DefaultSelector` for its event loop, so `protocol.py` must support **non-blocking incremental parsing**. The `feed()` + `try_read_message()` pattern allows the selector-based loop to feed data as it arrives and attempt to parse complete messages without blocking:

Implementation:
```python
import json
import socket

class JsonRpcConnection:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = b""
        self._closed = False

    def feed(self, data: bytes):
        """Feed raw bytes received from the socket into the internal buffer.
        Called by the server's selector loop when the socket is readable.
        If data is empty (EOF — remote end closed connection), marks as closed."""
        if not data:
            self._closed = True
            return
        self._buffer += data

    def try_read_message(self) -> dict | None:
        """Attempt to parse one complete Content-Length framed message from the buffer.
        Returns the parsed message dict, or None if not enough data is buffered yet.
        Call in a loop after feed() to drain all complete messages."""
        header_end = self._buffer.find(b"\r\n\r\n")
        if header_end < 0:
            return None
        header = self._buffer[:header_end].decode('ascii')
        # Parse Content-Length from potentially multiple headers (DAP spec allows Content-Type too)
        content_length = None
        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length is None:
            raise ValueError("Missing Content-Length header")
        body_start = header_end + 4
        body_end = body_start + content_length
        if len(self._buffer) < body_end:
            return None  # incomplete body, wait for more data
        body = self._buffer[body_start:body_end]
        self._buffer = self._buffer[body_end:]
        return json.loads(body)

    def send_message(self, msg: dict):
        """Send a Content-Length framed JSON-RPC message.
        Sets _closed on BrokenPipeError/ConnectionResetError (adapter disconnected)."""
        body = json.dumps(msg).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('ascii')
        try:
            self._sock.sendall(header + body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._closed = True

    def send_notification(self, method: str, params: dict):
        """Send a JSON-RPC notification (no id, no response expected)."""
        self.send_message({"jsonrpc": "2.0", "method": method, "params": params})

    def send_response(self, request_id, result: dict):
        """Send a JSON-RPC response."""
        self.send_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def send_error(self, request_id, code: int, message: str):
        """Send a JSON-RPC error response."""
        self.send_message({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    @property
    def is_closed(self) -> bool:
        """True if the remote end closed the connection (EOF on feed) or a send failed."""
        return self._closed
```

**`session.py`:** State manager — holds current `run_id`, `current_frame_seq`, breakpoint list, and the `ReplayController`.

```python
from pyttd.models.frames import ExecutionFrames
from pyttd.replay import ReplayController

class Session:
    def __init__(self):
        self.run_id = None
        self.current_frame_seq = None
        self.state = "idle"  # "idle" | "recording" | "replay"
        self.breakpoints = []      # [{file: str, line: int}, ...]
        self.exception_filters = []  # ["raised", "uncaught"]
        self.current_stack = []    # [{seq, name, file, line, depth}, ...]
        self.first_line_seq = None   # cached in enter_replay()
        self.last_line_seq = None    # cached in enter_replay()
        self.replay_controller = ReplayController()
        self._stack_cache = {}  # seq -> stack_snapshot (checkpoint boundaries)

    def enter_replay(self, run_id, first_line_seq: int):
        """Transition from recording to replay mode."""
        ...

    def set_breakpoints(self, breakpoints: list[dict]):
        """Store breakpoint list (called from set_breakpoints RPC)."""
        ...

    def set_exception_filters(self, filters: list[str]):
        """Store active exception breakpoint filters."""
        ...

    # Navigation methods (return {seq, file, line, function_name, reason})
    def step_into(self) -> dict: ...
    def step_over(self) -> dict: ...
    def step_out(self) -> dict: ...
    def continue_forward(self) -> dict: ...
    # Phase 4 additions:
    def step_back(self) -> dict: ...
    def reverse_continue(self) -> dict: ...
    def goto_frame(self, target_seq: int) -> dict: ...
    def goto_targets(self, filename: str, line: int) -> list[dict]: ...

    # Query methods
    def get_stack_at(self, seq: int) -> list[dict]: ...
    def get_variables_at(self, seq: int, scope: str) -> list[dict]: ...
    def evaluate_at(self, seq: int, expression: str, context: str) -> dict: ...
```

State transitions:
1. **Created** with `run_id=None`, `current_frame_seq=None`, `state="idle"`
2. After recording completes: `state="replay"`, `current_frame_seq` set to the first `line` event's `sequence_no` (NOT seq 0 — seq 0 is the `call` event, which has no locals). The server queries: `ExecutionFrames.select().where(run_id == X, frame_event == 'line').order_by(sequence_no).limit(1).first()`. If no `line` event exists (edge case: empty function body, or script that only calls C functions), fall back to `sequence_no == 0` (the `call` event). The server then sends the `stopped` notification with `seq: first_line_seq`. The adapter transitions to replay mode (sets internal `isReplaying = true` flag) and sends `StoppedEvent('entry', 1)` — this is the one correct use of `reason: "entry"` (debuggee stopped at its entry point). The session also initializes `current_stack` by scanning events from seq 0 to `first_line_seq` (typically just one `call` event at seq 0, producing a single-frame stack).
3. All navigation operates in replay state

**Session initialization flow (on recording complete):**
1. Recording thread calls `recorder.stop()` → updates `Runs` record with `timestamp_end` and `total_frames`
2. Recording thread posts `recording_complete` message (with optional exception info) to the wakeup pipe
3. RPC thread receives wakeup, reads message from the queue
4. RPC thread sets `session.run_id` from recorder, queries DB for first `line` event's seq, calls `session.enter_replay(first_line_seq)`
5. `enter_replay()` sets `state="replay"`, `current_frame_seq=first_line_seq`, computes and caches `last_line_seq` (last `line` event's seq — used as boundary target for forward navigation), builds initial stack
6. RPC thread sends `stopped` notification with `{seq: first_line_seq, reason: "recording_complete", totalFrames: N}`
7. If the recording thread caught an exception from the user script, the RPC thread also sends an `output` notification (category `"stderr"`) with the traceback before the `stopped` notification

Methods:
- `get_stack_at(seq)` — reconstructs call stack by scanning backward from `seq` for `call` events that haven't been matched by a `return` or `exception_unwind` (stack algorithm: walk backward through frames, push `return`/`exception_unwind` events, pop on `call` events — remaining `call` events form the active stack). Returns list of stack frames with `{seq, name, file, line, depth}`. **Stack frame `seq` values:** The top-of-stack frame uses `current_frame_seq` (the `line` event the user is stopped at). For deeper frames (parent callers), `seq` is the most recent `line` event in that frame before the child's `call` event — this is the call site line. Query: for each parent frame's `call` event at depth D, find `ExecutionFrames.select().where(run_id == X, frame_event == 'line', call_depth == D, sequence_no < child_call_seq).order_by(sequence_no.desc()).limit(1)`. This `seq` is used as `frameId` in the DAP `StackFrame` and as the basis for `variablesReference` encoding (`seq + 1`), so clicking a parent frame in the Call Stack panel displays its locals at the point where it called into the child. **Performance optimization:** The naive backward scan is O(seq). For large recordings, this is too slow to call on every step. Two optimizations: (1) **Incremental tracking:** The session maintains a `current_stack` list that is updated incrementally as `current_frame_seq` changes. For **forward** navigation: push on `call` events, pop on `return`/`exception_unwind` events, no change on `line`/`exception`. For **backward** navigation: the push/pop logic is **reversed** — encountering a `call` event going backward means the frame is being exited (pop), encountering a `return`/`exception_unwind` going backward means the frame is being re-entered (push). For single-step navigation (step ±1, step over/in/out), the stack is updated by scanning the few events between old and new seq (usually 1-3 events), so it's effectively O(1). (2) **Checkpoint-boundary cache:** When entering replay mode, build the initial stack at seq=0 (a single `call` event = `[frame_0]`). For `goto_frame` jumps, scan forward from the nearest cached stack rather than backward from target. Cache stacks at checkpoint boundaries when they're computed. Implementation: maintain a `dict[int, list]` mapping `sequence_no -> stack_snapshot` at checkpoint boundaries, seeded with `{0: [frame_0_info]}`. **Population mechanism:** When `goto_frame` resolves via warm navigation (forward scan from a cached stack), the session builds the stack incrementally by processing events from the nearest cached checkpoint to the target. If the scan crosses a checkpoint boundary, the intermediate stack is cached at that boundary for future use. Cold navigation (checkpoint restore + fast-forward) does NOT populate this cache — the fast-forward code in the child process only counts events and does not track stack state. The cache is populated exclusively by the Python-level session code during warm scans.
- `get_variables_at(seq, scope)` — returns locals from recorded frame's `locals_snapshot` JSON. Each variable is `{name, value, type, variablesReference: 0}` (variablesReference=0 means no expandable children — DAP reserves 0 for "no children"). The `value` field is the raw `repr()` string from the snapshot. The `type` field is inferred from the repr: if the value looks like an int/float/bool/None literal, use that type name; otherwise use `"str"` (since all values are repr strings, precise type inference is best-effort). If `locals_snapshot` is `NULL` (e.g., for `call` or `exception_unwind` events reached via `goto_frame`), returns an empty variable list. Navigation methods (`step_over`, `step_into`, `step_back`) always land on `line` events, so this only occurs for direct frame jumps.
- `evaluate_at(seq, expression, context)` — for `hover`/`watch` context: looks up variable name in current frame's `locals_snapshot`, returns its value. For `repl` context: returns informational message. For nested attribute access (`obj.attr`): returns the full object repr (not the attribute — limitation of snapshot approach).
- `step_over()` — find next frame with `frame_event='line'` at same or shallower `call_depth`. This is the DAP `next` operation — it skips over function calls.
- `step_into()` — find next frame with `frame_event='line'` at any depth (i.e., step to the very next executed source line, entering called functions). This is the DAP `stepIn` operation. Note: both `step_over` and `step_into` skip `call`/`return`/`exception` events — they always land on `line` events since those correspond to source lines the user sees.
- `step_out()` — find next `return` or `exception_unwind` event at current depth, then next `line` event at the parent depth. This is the DAP `stepOut` operation. Must check both `return` and `exception_unwind` events because the trace function skips `PyTrace_RETURN` when `arg == NULL` (exception propagation), so only `exception_unwind` records that case. **Exception propagation edge case:** If the exit event is `exception_unwind` and no `line` event exists at `current_depth - 1` before the next `exception_unwind` at that depth (i.e., the exception propagates through the parent frame without executing any source lines), `step_out` should find the next `line` event at ANY shallower depth (use `call_depth < current_depth` instead of `== current_depth - 1`) — this lands on the first handler that catches the exception. If no such `line` event exists, navigate to end of recording. **Top-level edge case:** If `call_depth == 0` (top-level frame), there is no parent depth — navigate to the last `line` event of the recording and return `{"reason": "end"}`.
- `continue_forward()` — reads breakpoints and exception filters from session state (stored via `set_breakpoints` / `set_exception_breakpoints` RPCs). Scans forward through frames for breakpoint match (file + line). Uses the DB index on `(run_id, filename, line_no)`: for each breakpoint, query `ExecutionFrames.select().where(run_id == X, filename == bp.file, line_no == bp.line, sequence_no > current_seq).order_by(sequence_no).limit(1)`, then take the minimum sequence number across all breakpoints. If exception breakpoints are enabled: `"raised"` filter adds a query for `frame_event == 'exception'`; `"uncaught"` filter adds a query for `frame_event == 'exception_unwind' AND call_depth == 0`. Take the minimum across all results (see query patterns below). If no match found ahead, navigate to `last_line_seq` and return `{"reason": "end"}`.

**Navigation Peewee query patterns** (all navigation methods use indexed queries, not full scans):
```python
# step_into: next line event at any depth
ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.sequence_no > current_seq)
).order_by(ExecutionFrames.sequence_no).limit(1)

# step_over: next line event at same or shallower depth
ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.call_depth <= current_depth) &
    (ExecutionFrames.sequence_no > current_seq)
).order_by(ExecutionFrames.sequence_no).limit(1)

# step_out: next return/exception_unwind at current depth, then next line at parent
exit_event = ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event.in_(['return', 'exception_unwind'])) &
    (ExecutionFrames.call_depth == current_depth) &
    (ExecutionFrames.sequence_no > current_seq)
).order_by(ExecutionFrames.sequence_no).first()  # .first() returns None if no result
# If exit_event is None, we're at the end of recording (see boundary handling below)
# then find next line at parent depth:
parent_line = ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.call_depth == current_depth - 1) &
    (ExecutionFrames.sequence_no > exit_event.sequence_no)
).order_by(ExecutionFrames.sequence_no).first()  # .first() returns None if no result
# If parent_line is None and exit was exception_unwind (exception propagating through
# parent without executing a line), widen the search to ANY shallower depth:
if parent_line is None and exit_event and exit_event.frame_event == 'exception_unwind':
    parent_line = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.frame_event == 'line') &
        (ExecutionFrames.call_depth < current_depth) &
        (ExecutionFrames.sequence_no > exit_event.sequence_no)
    ).order_by(ExecutionFrames.sequence_no).first()
# If parent_line is None (exception propagated through all frames, or end of recording),
# return {"reason": "end"} — same as other forward methods at recording boundary

# continue_forward: per-breakpoint queries + optional exception breakpoint query
# For each line breakpoint:
ExecutionFrames.select(ExecutionFrames.sequence_no).where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.filename == bp_file) &
    (ExecutionFrames.line_no == bp_line) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.sequence_no > current_seq)
).order_by(ExecutionFrames.sequence_no).limit(1)
# If "raised" exception breakpoint filter active, also query:
ExecutionFrames.select(ExecutionFrames.sequence_no).where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'exception') &
    (ExecutionFrames.sequence_no > current_seq)
).order_by(ExecutionFrames.sequence_no).limit(1)
# If "uncaught" exception breakpoint filter active, also query:
ExecutionFrames.select(ExecutionFrames.sequence_no).where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'exception_unwind') &
    (ExecutionFrames.call_depth == 0) &
    (ExecutionFrames.sequence_no > current_seq)
).order_by(ExecutionFrames.sequence_no).limit(1)
# Take the minimum sequence_no across all results — that's the next stop point
```
Note: `step_over` and `step_out` queries on `call_depth` use the `(run_id, call_depth, sequence_no)` composite index added in Phase 0 (see `ExecutionFrames.Meta.indexes`). Without this index, SQLite would scan the `(run_id, sequence_no)` index sequentially with a filter on `call_depth`, which degrades for large recordings (> 100K frames).

**Boundary handling:** All navigation methods must handle reaching the end or beginning of the recording:
- Forward methods (`step_over`, `step_into`, `step_out`, `continue_forward`) when no matching frame exists ahead: return `{"reason": "end", "seq": last_line_seq}` where `last_line_seq` is the last `line` event's `sequence_no` (ensures variables are visible — `return`/`exception_unwind` events may have no `locals_snapshot`). Cache `last_line_seq` during `enter_replay()` alongside `first_line_seq`. The adapter sends `StoppedEvent('step')` with `description: "End of recording"` and `text: "End of recording"` (DAP `StoppedEvent` body supports these fields for additional context). Do NOT use `reason: "entry"` for this — DAP reserves `"entry"` for debuggee entry point stops.
- Backward methods (`step_back`, `reverse_continue` — added in Phase 4) at the beginning of recording: land on the first `line` event (NOT seq 0, which is a `call` event with no `locals_snapshot`). Return `{"reason": "start", "seq": first_line_seq}`. The adapter sends `StoppedEvent('step')` with `description: "Beginning of recording"`. Do NOT use `reason: "entry"` — that is reserved for the initial stop after recording completes (see session initialization flow). The session caches `first_line_seq` during `enter_replay()` initialization.
- `step_out` at `call_depth == 0` (top-level frame): there is no parent depth to step to. Navigate to the last `line` event of the recording (not the last frame, which could be `return`/`exception_unwind` with no `locals_snapshot`) and return `{"reason": "end", "seq": last_line_seq}`.
- All navigation responses include: `{"seq": <new_seq>, "file": "...", "line": <n>, "reason": "step"|"breakpoint"|"end"|"start"}`

**Stdout/stderr capture (owned by `server.py`, uses `os.pipe()` + `os.dup2()`):**

The stdout/stderr capture is coordinated by `server.py`, NOT by `runner.py`. `runner.py` remains a simple `runpy` wrapper. The server manages capture because it owns the RPC connection needed to forward output.

Timing is critical — capture must happen AFTER the port handshake line is written but BEFORE recording starts:
1. Server writes `PYTTD_PORT:<port>\n` to original stdout (step 3 of startup sequence)
2. Server creates `os.pipe()` pairs for stdout and stderr
3. Save original fd's: `saved_stdout = os.dup(1)`, `saved_stderr = os.dup(2)`
4. Redirect: `os.dup2(write_end_stdout, 1)`, `os.dup2(write_end_stderr, 2)`
5. Close the **original** write-end file descriptors returned by `os.pipe()` (NOT fd 1/2, which are now the pipe write ends via `dup2` and must remain open for the user script's `print()` calls)
6. Register the read ends of both pipes with the `selectors.DefaultSelector` in the RPC event loop (alongside the TCP socket and the wakeup pipe). When data arrives on a capture pipe, the RPC thread reads it and sends `rpc.send_notification("output", {"category": "stdout"|"stderr", "output": data})`. **Do NOT use separate background reader threads** — the selector-based approach keeps all I/O in a single RPC thread, avoiding thread-safety concerns with the RPC connection.
7. On shutdown: unregister capture pipes from selector, restore original fd's via `os.dup2(saved_stdout, 1)`, close saved fd's and pipe read ends.

This ensures user script output (from `print()`, etc.) is captured and forwarded as JSON-RPC events. `runner.py` doesn't need to know about capture — it just runs the script with redirected fd's already in place.

**Buffering caveat:** After `os.dup2()`, fd 1 points to a pipe. Python's `sys.stdout` (`TextIOWrapper`) detects this and switches to **full buffering** (not line-buffered), which delays output until the buffer fills (~8KB). To ensure prompt forwarding of user `print()` output to the Debug Console, call `sys.stdout.reconfigure(line_buffering=True)` after the `dup2()` redirect (Python 3.7+). Apply the same to `sys.stderr`: `sys.stderr.reconfigure(line_buffering=True)`. Without this, short print statements won't appear in the Debug Console until the script writes enough data to flush the buffer or the script exits.

**Important:** The capture pipes carry the user script's output from the recording thread to the RPC thread via OS-level pipe buffering. The recording thread writes to fd 1/2 (which are pipe write ends after `dup2`). The RPC thread reads from the pipe read ends via the selector. This is safe because OS pipes handle cross-thread reads/writes without explicit locking.

**Progress events during recording:** The server sends periodic JSON-RPC notification events:
```json
{"jsonrpc": "2.0", "method": "progress", "params": {"frameCount": 15000, "elapsedMs": 2340}}
```
Sent every 500ms during recording. Implementation: the RPC event loop uses `selector.select(timeout=0.5)` during recording (not blocking indefinitely). On each timeout with no I/O events, the loop checks if recording is active and sends a progress notification. The frame count is obtained from `pyttd_native.get_recording_stats()` (requires GIL — the RPC thread acquires it briefly via the normal Python calling convention; the recording thread yields the GIL periodically at CPython's switch interval, default 5ms). Alternatively, the `Recorder` object can maintain a `frame_count` attribute updated by the `_on_flush` callback (which already has the GIL and knows the batch size), avoiding an extra GIL acquisition.

The adapter forwards progress to VSCode using DAP's native progress events: `ProgressStartEvent` (on `configurationDone`, with `progressId = 'pyttd-recording'` and `title = 'Recording'`), `ProgressUpdateEvent` (on each progress notification, same `progressId`), and `ProgressEndEvent` (on recording complete, same `progressId`). The adapter stores `private progressId = 'pyttd-recording'` as a constant string for all three events.

### Extension-side pyttd package detection

On `launchRequest`, before spawning the server, the adapter runs `python -c "import pyttd; print(pyttd.__version__)"`. If this fails, it sends an `OutputEvent` with the message: "pyttd is not installed in your Python environment. Install with: pip install pyttd" and fails the launch with a clear error.

### TypeScript side — DAP handlers

`pyttdDebugSession.ts` implements standard DAP handlers:

| DAP Handler | Phase | Behavior |
|---|---|---|
| `initializeRequest` | 3 | Returns capabilities: `supportsConfigurationDoneRequest: true`, `supportsEvaluateForHovers: true`, `supportsProgressReporting: true`. Note: `supportsStepBack`, `supportsGotoTargetsRequest`, `supportsRestartFrame` are NOT advertised yet — added in Phase 4. Do NOT advertise `supportsExceptionInfoRequest` unless an `exceptionInfoRequest` handler is implemented. |
| `launchRequest` | 3 | **Async handler pattern:** `LoggingDebugSession` handlers are synchronous, but launch requires async work (spawn, port read, TCP connect). Do NOT call `sendResponse(response)` immediately — store the response object and call `sendResponse` at the end of the async chain. Implementation: cast `args` to `PyttdLaunchConfig`, call `this.backend.spawn(...).then((port) => this.backend.connect(port)).then(() => { ... this.sendEvent(new InitializedEvent()); this.sendResponse(response); }).catch((err) => { this.sendErrorResponse(response, 1, err.message); this.sendEvent(new TerminatedEvent()); })`. The `.catch()` is critical — without it, spawn failures (bad Python path, missing pyttd) or connect timeouts silently swallow the error and leave the debug session in a broken state. Resolves Python path (see below). Spawns `python -m pyttd serve --script <path> --cwd <dir>` (if launch config has `module` instead of `program`, adds `--module` flag). Reads `PYTTD_PORT:<port>` from child stdout (with 10s timeout). Also listens for stderr data events and logs them as error output. Connects TCP. Sends `backend_init` RPC (NOT `initialize` — avoid confusion with DAP's own `initialize`), then `launch` RPC. Then sends `InitializedEvent` and finally `sendResponse`. **Source path normalization:** The adapter must normalize `program` paths to absolute paths (via `path.resolve(cwd, program)`) before sending to the backend, and must normalize source paths from the backend (which uses `co_filename` — already absolute on CPython) for consistent DAP `Source` objects. On Windows, normalize path separators and drive letter casing. |
| `setBreakpointsRequest` | 3 | Stores breakpoints in adapter state keyed by source file + sends `set_breakpoints` RPC to backend with the merged complete list across all files. DAP sends `setBreakpointsRequest` per source file with the complete list for that file — the adapter must maintain a `Map<string, Breakpoint[]>` and send the flattened union to the backend. Called by VSCode during configuration phase (before `configurationDone`) and whenever the user modifies breakpoints during replay. **Response:** must return `breakpoints` array with one `Breakpoint` per input, each with `verified: true` and `line` matching the requested line (post-mortem debugger — all breakpoints are accepted; verification against actual trace data is deferred to `continue`/`reverse_continue` execution). |
| `setExceptionBreakpointsRequest` | 3 | Configures exception breakpoint filters. The `filters` array contains active filter IDs (as declared in `package.json` `exceptionBreakpointFilters`): `"raised"` = stop on ALL `exception` frame events during continue/reverse-continue; `"uncaught"` = stop only on `exception_unwind` events at `call_depth == 0` (exceptions that propagate out of the top-level frame). Backend stores active filters and applies them in `continue_forward` / `reverse_continue` queries. |
| `configurationDoneRequest` | 3 | Sends `configuration_done` RPC to backend. Backend starts recording thread (executes user script). |
| `threadsRequest` | 3 | Returns `[{id: 1, name: "Main Thread"}]`. Multi-thread: deferred to Phase 7. |
| `stackTraceRequest` | 3 | Sends `get_stack_trace` RPC with `current_frame_seq` from last StoppedEvent |
| `scopesRequest` | 3 | Returns `[{name: "Locals", variablesReference: <encodedRef>}]`. Use `sequence_no + 1` as `variablesReference` (DAP reserves 0 to mean "no variables", so raw seq 0 would incorrectly hide variables). In `stackTraceResponse`, set `frameId = seq` for each stack frame. In `scopesRequest(frameId=seq)`, set `variablesReference = seq + 1`. In `variablesRequest(variablesReference=ref)`, decode as `seq = ref - 1` and query that frame's `locals_snapshot`. This avoids maintaining a separate ID mapping while avoiding the `variablesReference: 0` edge case. |
| `variablesRequest` | 3 | Decodes `variablesReference` as `seq = ref - 1` (see `scopesRequest` encoding), sends `get_variables` RPC with that seq. Returns flat name=value pairs with `variablesReference: 0` (no expandable children). |
| `evaluateRequest` | 3 | `hover`/`watch` context: sends `evaluate` RPC. `repl` context: returns informational message ("Replay mode — expression evaluation not available. Use Variables panel to inspect recorded state."). |
| `continueRequest` | 3 | Sets `response.body = { allThreadsContinued: true }` (required by DAP spec). Sends `continue` RPC. On result, sends `StoppedEvent('breakpoint')` if stopped at a breakpoint, or `StoppedEvent('step')` with `description: "End of recording"` if the end of recording was reached (backend returns `reason: "end"`). Map backend `reason` field: `"breakpoint"` → `StoppedEvent('breakpoint')`, `"exception"` → `StoppedEvent('exception')`, `"end"` → `StoppedEvent('step')` with description, `"step"` → `StoppedEvent('step')`. |
| `nextRequest` | 3 | Sends `next` RPC. Sends `StoppedEvent('step')`. |
| `stepInRequest` | 3 | Sends `step_in` RPC. Sends `StoppedEvent('step')`. |
| `stepOutRequest` | 3 | Sends `step_out` RPC. Sends `StoppedEvent('step')`. |
| `pauseRequest` | 3 | During recording: sends `interrupt` to backend, stops recording early. During replay: no-op. |
| `disconnectRequest` | 3 | Sends `disconnect` RPC and waits for response (with 2s timeout), then kills child process and closes socket. The timeout ensures cleanup even if the backend is hung. |

**Adapter notification dispatch:**
The adapter registers a notification callback via `this.backend.onNotification(...)` during `launchRequest` (after TCP connect). This callback dispatches backend notifications to DAP events:
```typescript
this.backend.onNotification((method: string, params: any) => {
    switch (method) {
        case 'stopped':
            this.currentSeq = params.seq;
            this.isReplaying = true;
            this.sendEvent(new ProgressEndEvent(this.progressId));
            this.sendEvent(new StoppedEvent('entry', 1));
            break;
        case 'output':
            this.sendEvent(new OutputEvent(params.output, params.category));
            break;
        case 'progress':
            this.sendEvent(new ProgressUpdateEvent(this.progressId,
                `Recording: ${params.frameCount} frames`));
            break;
    }
});
```
Navigation RPC responses also update `this.currentSeq` and send the appropriate `StoppedEvent`. The `stopped` notification above is for the initial recording-complete transition; subsequent stops come from navigation RPC responses (not notifications).

**Adapter recording/replay mode tracking:**
The adapter maintains an `isReplaying: boolean` state variable (initially `false`). During recording (`isReplaying === false`): `stackTraceRequest`, `scopesRequest`, `variablesRequest`, `nextRequest`, `stepInRequest`, `stepOutRequest`, `continueRequest` all respond immediately with empty/no-op results (no recorded frames to query yet). Only `pauseRequest` (→ `interrupt`) and `disconnectRequest` are active. The adapter sends `ProgressStartEvent` on `configurationDone` and `ProgressUpdateEvent` on each `progress` notification from the backend. When the backend sends the `stopped` notification, the adapter sets `isReplaying = true`, sends `ProgressEndEvent`, and then sends `StoppedEvent('entry', 1)`. From that point, all navigation/query handlers delegate to the backend. The `current_seq` (from the last `stopped` notification or navigation result) is stored in the adapter for use in `stackTraceRequest` and `scopesRequest`.

**Variable representation (warm path limitation):**
During warm navigation (reading from SQLite), variable values are `repr()` strings captured during recording. Complex objects appear as their `repr()` and **cannot be expanded** into sub-properties. The Variables panel shows flat name=value pairs where each value is a string. `variablesReference` is 0 for all variables (indicating no children). This is a known limitation of the snapshot-based approach.

**Call depth tracking:** During recording, each frame event is tagged with its call depth (an integer incremented on `call` events, decremented on `return` events). This is stored in the `ExecutionFrames` model (added in Phase 0). `step_over` uses this: find the next frame where `depth <= current_depth AND event = 'line'`. This avoids walking the entire call tree.

**Python path discovery:** The adapter checks, in order: (1) `pythonPath` from launch config, (2) `python.defaultInterpreterPath` VS Code setting, (3) `.venv/bin/python` or `venv/bin/python` in the workspace root (common convention for local virtual environments), (4) `python3` on PATH, (5) `python` on PATH. For options 3-5, verify the candidate exists (via `fs.existsSync` for local paths, or by attempting `child_process.execFileSync(candidate, ['--version'])` for PATH candidates) before using it. If no valid Python is found, fail the launch with a clear error message: "Could not find a Python interpreter. Set 'pythonPath' in your launch configuration." If `child_process.spawn` fails with `ENOENT`, catch the `error` event on the child process and reject the spawn promise with a descriptive message.

**`backendConnection.ts`** implementation details:
```typescript
class BackendConnection {
    private process: ChildProcess | null = null;
    private socket: net.Socket | null = null;
    private rpc: JsonRpcConnection | null = null;
    private notificationCallback: ((method: string, params: any) => void) | null = null;

    async spawn(pythonPath: string, args: string[]): Promise<number> {
        // Spawn child process, read PYTTD_PORT:<port> from stdout
        // Returns the port number
        this.process = child_process.spawn(pythonPath, ['-m', 'pyttd', 'serve', ...args]);
        return new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error("Timeout waiting for port")), 10000);
            let stdoutBuffer = '';
            this.process!.stdout!.on('data', (data: Buffer) => {
                // Buffer data and split by lines — a single 'data' event
                // may contain multiple lines or partial lines
                stdoutBuffer += data.toString();
                const lines = stdoutBuffer.split('\n');
                // Keep the last (possibly incomplete) line in the buffer
                stdoutBuffer = lines.pop() || '';
                for (const line of lines) {
                    const match = line.trim().match(/^PYTTD_PORT:(\d+)$/);
                    if (match) {
                        clearTimeout(timeout);
                        resolve(parseInt(match[1]));
                        return;
                    }
                }
            });
            // Monitor stderr for startup errors
            let stderrBuffer = '';
            this.process!.stderr!.on('data', (data: Buffer) => {
                stderrBuffer += data.toString();
            });
            // Reject if process exits before writing port
            this.process!.on('exit', (code) => {
                clearTimeout(timeout);
                reject(new Error(`Backend exited with code ${code}: ${stderrBuffer}`));
            });
            // Reject if spawn fails (e.g., ENOENT — Python not found at given path)
            this.process!.on('error', (err) => {
                clearTimeout(timeout);
                reject(new Error(`Failed to spawn backend: ${err.message}`));
            });
        });
    }

    async connect(port: number): Promise<void> { /* TCP connect, wrap in JsonRpcConnection */ }
    async sendRequest(method: string, params: any): Promise<any> { /* Send request, await response by id */ }
    onNotification(callback: (method: string, params: any) => void): void { /* Register handler */ }
    close(): void { /* Close socket, kill process */ }
}
```

**TypeScript `JsonRpcConnection` class** (used by `BackendConnection`, mirrors Python `protocol.py`): Wraps a `net.Socket` with Content-Length framing. Key implementation details:
- Maintains a `Buffer` accumulator fed by `socket.on('data')`. Must handle partial headers across multiple `data` events (a single `\r\n\r\n` may span two events).
- `read_message()`: Scans for `\r\n\r\n` header terminator, parses `Content-Length` from headers (supports multiple header lines), extracts body when enough bytes are buffered, returns parsed JSON
- `send_message(msg)`: Serializes to JSON, prepends `Content-Length: <N>\r\n\r\n`, writes to socket
- `sendRequest(method, params)`: Assigns incrementing `id`, creates a `Promise` stored in a `Map<number, {resolve, reject}>` keyed by `id`, sends the message. When a response arrives with a matching `id`, resolves/rejects the corresponding promise. **On timeout rejection, remove the entry from the Map** to prevent stale late-arriving responses from resolving a dead promise.
- Incoming message dispatch: responses (have `id` + `result`/`error`) resolve pending promises; notifications (have `method`, no `id`) are dispatched to the registered notification callback
- Timeout: pending requests reject after 5 seconds (configurable via launch config `rpcTimeout`)

### DAP configuration flow (correct order per DAP spec)

1. VSCode sends `initialize` -> adapter responds with capabilities
2. VSCode sends `launch` -> adapter spawns Python backend, connects TCP, sends `backend_init` + `launch` RPCs
3. Adapter sends `InitializedEvent` (tells VSCode it's ready for configuration)
4. VSCode sends `setBreakpoints`, `setExceptionBreakpoints` (configuration phase) -> adapter forwards to backend
5. VSCode sends `configurationDone` -> adapter sends `configuration_done` RPC -> backend starts recording thread
6. Recording completes -> backend sends `stopped` notification -> adapter sends `StoppedEvent('entry', 1)` (stops at first frame)
7. VSCode requests `threads` -> `stackTrace` -> `scopes` -> `variables`

**Important:** In DAP, `launch` is sent BEFORE `InitializedEvent`. The adapter processes `launch` (spawns backend) then sends `InitializedEvent`. VSCode then sends breakpoint configuration. Recording begins only after `configurationDone`.

### JSON-RPC message format

All messages between the debug adapter and Python backend use JSON-RPC 2.0 over Content-Length framed TCP:

**Requests (adapter -> backend):**
```json
{"jsonrpc": "2.0", "id": 1, "method": "backend_init", "params": {}}
{"jsonrpc": "2.0", "id": 2, "method": "launch", "params": {"script": "/path/to/script.py", "cwd": "/path/to", "args": [], "checkpointInterval": 1000}}
{"jsonrpc": "2.0", "id": 3, "method": "set_breakpoints", "params": {"breakpoints": [{"file": "/path/to/script.py", "line": 10}]}}
{"jsonrpc": "2.0", "id": 4, "method": "set_exception_breakpoints", "params": {"filters": ["uncaught"]}}
{"jsonrpc": "2.0", "id": 5, "method": "configuration_done", "params": {}}
{"jsonrpc": "2.0", "id": 6, "method": "get_stack_trace", "params": {"seq": 1500}}
{"jsonrpc": "2.0", "id": 7, "method": "get_variables", "params": {"seq": 1500, "scope": "locals"}}
{"jsonrpc": "2.0", "id": 8, "method": "get_threads", "params": {}}
{"jsonrpc": "2.0", "id": 9, "method": "get_scopes", "params": {"seq": 1500}}
{"jsonrpc": "2.0", "id": 10, "method": "next", "params": {}}
{"jsonrpc": "2.0", "id": 11, "method": "step_in", "params": {}}
{"jsonrpc": "2.0", "id": 12, "method": "step_out", "params": {}}
{"jsonrpc": "2.0", "id": 13, "method": "continue", "params": {}}
{"jsonrpc": "2.0", "id": 14, "method": "interrupt", "params": {}}
{"jsonrpc": "2.0", "id": 15, "method": "evaluate", "params": {"seq": 1500, "expression": "x", "context": "hover"}}
{"jsonrpc": "2.0", "id": 16, "method": "disconnect", "params": {}}
```

**Responses (backend -> adapter):**
```json
{"jsonrpc": "2.0", "id": 1, "result": {"version": "0.1.0", "capabilities": ["recording", "warm_navigation"]}}
{"jsonrpc": "2.0", "id": 6, "result": {"frames": [{"seq": 1500, "name": "foo", "file": "/path/to/script.py", "line": 10, "depth": 2}]}}
{"jsonrpc": "2.0", "id": 7, "result": {"variables": [{"name": "x", "value": "42", "type": "int"}, {"name": "items", "value": "[1, 2, 3]", "type": "list"}]}}
{"jsonrpc": "2.0", "id": 8, "result": {"threads": [{"id": 1, "name": "Main Thread"}]}}
{"jsonrpc": "2.0", "id": 9, "result": {"scopes": [{"name": "Locals", "variablesReference": 1501}]}}
{"jsonrpc": "2.0", "id": 10, "result": {"seq": 1501, "file": "/path/to/script.py", "line": 11, "reason": "step"}}
```

**Notifications (backend -> adapter, no id):**
```json
{"jsonrpc": "2.0", "method": "stopped", "params": {"seq": 25000, "reason": "recording_complete", "totalFrames": 25000}}
{"jsonrpc": "2.0", "method": "output", "params": {"category": "stdout", "output": "Hello, world!\n"}}
{"jsonrpc": "2.0", "method": "progress", "params": {"frameCount": 15000, "elapsedMs": 2340}}
```

### Create

- `pyttd/server.py` — JSON-RPC server (TCP, two-thread model: RPC thread + recording thread). Owns stdout/stderr capture via `os.pipe()` + `os.dup2()` (timed after port handshake, before recording starts). Computes DB path: for script mode, `os.path.splitext(os.path.basename(script))[0] + DB_NAME_SUFFIX` placed in the **script's directory**; for module mode (`--module` flag), `module_name.replace('.', '_') + DB_NAME_SUFFIX` placed in `--cwd` (consistent with `_cmd_record`). The `--cwd` argument sets the working directory for the user script. If the `launch` RPC provides a `traceDb` param (from the launch config's `traceDb` property), override the computed DB path with that value (resolve relative to `cwd`). Deletes existing DB + WAL/SHM files via `storage.delete_db_files()` before recording to ensure fresh schema (avoids stale columns from older phases).
- `pyttd/protocol.py` — Content-Length framing, request/response correlation
- `pyttd/session.py` — navigation state, stack reconstruction, breakpoint matching
- `pyttd/runner.py` — **no changes from Phase 1**. Remains a simple `runpy` wrapper. Does NOT own stdout/stderr capture (that is `server.py`'s responsibility).
- `vscode-pyttd/src/debugAdapter/pyttdDebugSession.ts` — full DAP handler implementations (forward navigation)
- `vscode-pyttd/src/debugAdapter/backendConnection.ts` — full implementation (spawn, port parsing, TCP connect, Content-Length framing, send/receive). Includes a TypeScript `JsonRpcConnection` class that mirrors `protocol.py`: reads `Content-Length: <N>\r\n\r\n<body>` framing from the TCP socket, parses JSON-RPC messages, correlates request IDs to pending promises, and dispatches notifications to registered callbacks. Implementation detail: use Node.js `net.Socket` data events, accumulate into a `Buffer`, parse headers when `\r\n\r\n` is found, extract body when enough bytes are buffered.
- `tests/test_server.py` — start server, send JSON-RPC requests via TCP, verify responses
- `tests/test_session.py` — navigation logic (step forward/over/into/out), stack reconstruction, breakpoint matching

### Update

- **`pyttd/cli.py`** — Update the `main()` dispatch to call `_cmd_serve(args)` instead of the stub print:
  ```python
  elif args.command == 'serve':
      _cmd_serve(args)
  ```

### Verify

1. `.venv/bin/python -m pyttd serve --script samplecode/basic_sample_function.py --cwd .` starts, prints port, accepts JSON-RPC on TCP
2. F5 in Extension Dev Host: script executes, stdout appears in Debug Console
3. After completion: Call Stack and Variables panels show recorded state at first frame
4. Next/Step In/Step Out navigate forward through recorded frames correctly
5. Click Stop -> session terminates cleanly, no orphan Python processes
6. Launch with invalid Python path -> helpful error in Debug Console
7. Launch without pyttd installed -> clear installation instructions shown
8. `.venv/bin/pytest tests/test_server.py tests/test_session.py` pass

---

## Phase 4: Time-Travel (Step Back, Reverse Continue, I/O Hooks)

**Goal:** The core time-travel UX. Step back, reverse continue, and goto-frame all work in VSCode. I/O hooks enable deterministic replay during cold navigation.

### DAP capability update

In `initializeRequest`, add capabilities: `supportsStepBack: true`, `supportsGotoTargetsRequest: true`, `supportsRestartFrame: true`.

### DAP handlers added to `pyttdDebugSession.ts`

| DAP Handler | Behavior |
|---|---|
| `stepBackRequest` | Sends `step_back` RPC. Backend decrements `current_seq` to previous `line` event, reads from SQLite (always warm — no checkpoint needed for ±1 step). Adapter sends `StoppedEvent('step')`. At beginning of recording (when `current_frame_seq == first_line_seq` or no previous `line` event exists), `current_frame_seq` stays unchanged and returns `{"reason": "start", "seq": first_line_seq}` — adapter sends `StoppedEvent('step')` with `description: "Beginning of recording"`. Stack update uses efficient reverse scan (see step_back stack update section below), NOT a full `_build_stack_at` rebuild. |
| `reverseContinueRequest` | Sends `reverse_continue` RPC with current breakpoint list. Backend scans backward via DB index for breakpoint match. Adapter sends `StoppedEvent('breakpoint')` if a breakpoint was hit. If no breakpoint found behind current position, lands on first `line` event and adapter sends `StoppedEvent('step')` with `description: "Beginning of recording"` (do NOT use `reason: "entry"` — reserved for initial stop after recording completes). |
| `gotoTargetsRequest` | Sends `goto_targets` RPC with file and line. Backend returns list of `{seq, function_name}` for matching `line` events (capped at 1000 to avoid multi-MB responses for hot loop lines). Adapter maps each to a `GotoTarget` with `id = seq` (DAP `targetId` is an opaque integer — using `seq` directly avoids maintaining a separate mapping). Adapter stores these in a `Map<number, GotoTarget>` for validation. |
| `gotoRequest` | Extracts `targetId` from the request (which is `seq` — see `gotoTargetsRequest` mapping). Sends `goto_frame` RPC with that `seq`. Backend decides warm vs cold: if `abs(target_seq - current_frame_seq) > 1000` AND a live checkpoint exists nearer to the target than to the current position, use cold (checkpoint restore + fast-forward); otherwise use warm (DB read). Adapter sends `StoppedEvent('goto')`. |
| `restartFrameRequest` | Extracts the `frameId` from the request (which is `seq` — see stack trace encoding). Sends `restart_frame` RPC with `{"seq": frameId}`. The **backend** (not the adapter) does the lookup: finds the `call` event for the frame containing `seq`, then navigates to the first `line` event at that depth within that frame (so variables are visible). Backend query: find the `call` event at the same depth with `sequence_no <= seq`, then find first `line` event where `sequence_no > call_seq AND call_depth == frame_depth` (must be `==`, not `>=` — using `>=` would incorrectly match lines in child calls if the first thing the function does is call another function). Internally delegates to `session.goto_frame()`. This requires a dedicated `restart_frame` RPC — the adapter cannot compute the target seq because it has no DB access. |

### Navigation mode clarification

- **Warm navigation** (always used for step ±1, continue, reverse-continue, and any DB-backed query): Reads frame data directly from SQLite. Sub-millisecond. Variables are `repr()` snapshots — flat, non-expandable. This is the **primary** navigation mode for all operations.
- **Cold navigation** (used only for `goto_frame` jumps to distant frames when live object state reconstruction is desired): Checkpoint restore + fast-forward via pipe-based IPC. 50-300ms. Produces the same `repr()` snapshots but through live re-execution. With I/O hooks active (this phase), non-deterministic function calls produce the same values as during recording.

**Key insight:** `step_back` is always warm. It simply decrements `current_frame_seq` and reads the previous frame from SQLite. No checkpoint needed. Cold navigation is only triggered by explicit `goto_frame` requests (from the timeline scrubber, goto targets, or restart frame) when the target is far from any warm child's position.

### `goto_frame` Session-level implementation

`Session.goto_frame(target_seq)` is the entry point for all frame-jump navigation (goto targets, restart frame, timeline scrubber). Implementation:

1. **Validate target:** Query `ExecutionFrames` to verify `target_seq` exists for this `run_id`. If not found, return `{"error": "frame_not_found", "target_seq": target_seq}`. If the target event is not a `line` event (e.g., user clicked a `call` or `return` in the timeline), find the nearest `line` event: query `frame_event == 'line' AND sequence_no >= target_seq ORDER BY sequence_no LIMIT 1`. If none exists forward, try `sequence_no <= target_seq ORDER BY sequence_no DESC LIMIT 1`. This ensures variables are always visible.

2. **Navigate:** Call `ReplayController.goto_frame(run_id, target_seq)` which tries cold (checkpoint restore + fast-forward) then falls back to warm (SQLite read). The warm/cold decision is handled by `ReplayController` — it calls `pyttd_native.restore_checkpoint(target_seq)` which finds the nearest checkpoint. If no checkpoint exists or `restore_checkpoint` fails, warm fallback reads from SQLite.

3. **Rebuild stack:** Call `_build_stack_at(target_seq)` to reconstruct the call stack. **Performance:** `_build_stack_at` is O(target_seq) which is too slow for large recordings (>50K frames → >50ms). Use the `_stack_cache` to reduce the scan range. The optimized `_build_stack_at` implementation:
```python
def _build_stack_at(self, seq: int) -> list[dict]:
    # Find nearest cached stack <= seq
    cached_seqs = [s for s in self._stack_cache if s <= seq]
    if cached_seqs:
        start_seq = max(cached_seqs)
        stack = [entry.copy() for entry in self._stack_cache[start_seq]]
        # CRITICAL: cached stacks are in DAP order (deepest-first, from the
        # return value of _build_stack_at). The scan loop below uses append/pop
        # which expects call-stack order (shallowest-first). Reverse to convert.
        stack.reverse()
    else:
        start_seq = 0
        stack = []

    # Scan forward from cached position
    events = list(ExecutionFrames.select()
                  .where((ExecutionFrames.run_id == self.run_id) &
                         (ExecutionFrames.sequence_no > start_seq) &
                         (ExecutionFrames.sequence_no <= seq))
                  .order_by(ExecutionFrames.sequence_no))
    for ev in events:
        if ev.frame_event == 'call':
            stack.append(self._frame_to_stack_entry(ev))
        elif ev.frame_event in ('return', 'exception_unwind'):
            if stack:
                stack.pop()
        elif ev.frame_event == 'line':
            if stack and stack[-1]['depth'] == ev.call_depth:
                stack[-1] = self._frame_to_stack_entry(ev)

    # Reverse so top-of-stack (deepest) is first (DAP convention)
    stack.reverse()
    return stack
```
Populate `_stack_cache` entries at checkpoint boundaries: when Phase 4's `goto_frame` is called, after rebuilding the stack, store `self._stack_cache[target_seq] = [entry.copy() for entry in self.current_stack]` if `target_seq` is at a checkpoint boundary (i.e., matches a `Checkpoint.sequence_no`). The cached stack is in DAP order (deepest-first); `_build_stack_at` reverses it before continuing the scan. This gives O(frames_since_last_checkpoint) instead of O(total_frames) for subsequent jumps.

4. **Update state:** Set `current_frame_seq = target_seq`, `current_stack = rebuilt_stack`.

5. **Return:** `{"seq": target_seq, "file": ..., "line": ..., "function_name": ..., "reason": "goto"}`.

```python
def goto_frame(self, target_seq: int) -> dict:
    # 1. Validate target exists
    frame = ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.sequence_no == target_seq))
    if frame is None:
        return {"error": "frame_not_found", "target_seq": target_seq}

    # 2. Snap to nearest line event if not already one
    if frame.frame_event != 'line':
        # Try forward first
        line_fwd = ExecutionFrames.select().where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.frame_event == 'line') &
            (ExecutionFrames.sequence_no >= target_seq)
        ).order_by(ExecutionFrames.sequence_no).first()
        if line_fwd:
            target_seq = line_fwd.sequence_no
        else:
            # Try backward
            line_bwd = ExecutionFrames.select().where(
                (ExecutionFrames.run_id == self.run_id) &
                (ExecutionFrames.frame_event == 'line') &
                (ExecutionFrames.sequence_no <= target_seq)
            ).order_by(ExecutionFrames.sequence_no.desc()).first()
            if line_bwd:
                target_seq = line_bwd.sequence_no
            else:
                return {"error": "no_line_event", "target_seq": target_seq}

    # 3. Navigate (ReplayController handles cold vs warm)
    self.replay_controller.goto_frame(self.run_id, target_seq)

    # 4. Rebuild stack and update state
    self.current_frame_seq = target_seq
    self.current_stack = self._build_stack_at(target_seq)

    # 5. Cache at checkpoint boundaries
    from pyttd.models.checkpoints import Checkpoint
    is_checkpoint = Checkpoint.select().where(
        (Checkpoint.run_id == self.run_id) &
        (Checkpoint.sequence_no == target_seq)
    ).exists()
    if is_checkpoint:
        self._stack_cache[target_seq] = [e.copy() for e in self.current_stack]

    # 6. Return result
    target_frame = ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.sequence_no == target_seq))
    if target_frame:
        return {
            "seq": target_seq,
            "file": target_frame.filename,
            "line": target_frame.line_no,
            "function_name": target_frame.function_name,
            "reason": "goto",
        }
    return {"seq": target_seq, "reason": "goto"}
```

**`restart_frame` Session-level implementation:** `Session.restart_frame(frame_seq)` takes a `frameId` (which is a `seq` from the stack trace). It finds the `call` event that started this frame's invocation, then navigates to the first `line` event within that call:
```python
def restart_frame(self, frame_seq: int) -> dict:
    # Find the frame at this seq to get its call_depth
    frame = ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.sequence_no == frame_seq))
    if frame is None:
        return {"error": "frame_not_found"}
    depth = frame.call_depth
    # Find the call event for this frame: latest call at this depth with seq <= frame_seq
    call_event = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.frame_event == 'call') &
        (ExecutionFrames.call_depth == depth) &
        (ExecutionFrames.sequence_no <= frame_seq)
    ).order_by(ExecutionFrames.sequence_no.desc()).first()
    if call_event is None:
        return {"error": "call_event_not_found"}
    # Find first line event in this call (same depth, after call)
    first_line = ExecutionFrames.select().where(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.frame_event == 'line') &
        (ExecutionFrames.call_depth == depth) &  # == not >= to avoid matching child calls
        (ExecutionFrames.sequence_no > call_event.sequence_no)
    ).order_by(ExecutionFrames.sequence_no).first()
    if first_line is None:
        return {"error": "no_line_in_frame"}
    return self.goto_frame(first_line.sequence_no)
```

### `step_back` stack update optimization

The Phase 3 `_update_stack()` method does a full `_build_stack_at(new_seq)` rebuild for backward navigation, which is O(seq). For `step_back` (going back by one `line` event — typically a gap of 1-5 frame events), this is unnecessarily expensive. Replace the backward path with an efficient reverse scan:

```python
# In _update_stack, backward case (new_seq < old_seq):
if old_seq - new_seq > 100:
    # Large backward jump — full rebuild with cache optimization
    self.current_stack = self._build_stack_at(new_seq)
    return

# Small backward jump — efficient reverse scan
events = list(ExecutionFrames.select()
              .where((ExecutionFrames.run_id == self.run_id) &
                     (ExecutionFrames.sequence_no > new_seq) &
                     (ExecutionFrames.sequence_no <= old_seq))
              .order_by(ExecutionFrames.sequence_no.desc()))  # reverse order
for ev in events:
    if ev.frame_event == 'call':
        # Going backward past a call = exiting the frame (pop)
        if self.current_stack and self.current_stack[0]['depth'] == ev.call_depth:
            self.current_stack.pop(0)
    elif ev.frame_event in ('return', 'exception_unwind'):
        # Going backward past a return = re-entering the frame (push)
        # Find the call event that started this frame to get its info
        call_event = ExecutionFrames.select().where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.frame_event == 'call') &
            (ExecutionFrames.call_depth == ev.call_depth) &
            (ExecutionFrames.sequence_no < ev.sequence_no)
        ).order_by(ExecutionFrames.sequence_no.desc()).first()
        if call_event:
            # Find the most recent line event in this frame before new_seq
            # to get the correct line number for the re-entered frame
            recent_line = ExecutionFrames.select().where(
                (ExecutionFrames.run_id == self.run_id) &
                (ExecutionFrames.frame_event == 'line') &
                (ExecutionFrames.call_depth == ev.call_depth) &
                (ExecutionFrames.sequence_no <= new_seq) &
                (ExecutionFrames.sequence_no >= call_event.sequence_no)
            ).order_by(ExecutionFrames.sequence_no.desc()).first()
            entry = self._frame_to_stack_entry(recent_line or call_event)
            self.current_stack.insert(0, entry)
# Update top-of-stack with the new_seq's frame info
target_frame = ExecutionFrames.get_or_none(
    (ExecutionFrames.run_id == self.run_id) &
    (ExecutionFrames.sequence_no == new_seq))
if target_frame and self.current_stack:
    self.current_stack[0] = self._frame_to_stack_entry(target_frame)
```

This is O(gap_size) for the scan (typically 1-5 events for step_back) plus O(1) indexed queries per call/return event encountered in the gap. For `goto_frame` (large jumps), continue to use `_build_stack_at` with the `_stack_cache` optimization.

**Threshold:** Use the efficient reverse scan when `old_seq - new_seq <= 100` (small backward jump, covers step_back and short reverse-continues). For larger backward jumps, fall back to `_build_stack_at(new_seq)` with the `_stack_cache` optimization, which is more efficient than O(gap_size) individual queries for each call/return in a large gap.

**`step_back` implementation:**
```python
def step_back(self) -> dict:
    if self.current_frame_seq is None or self.current_frame_seq <= self.first_line_seq:
        return self._navigate_to(self.first_line_seq, "start")
    frame = (ExecutionFrames.select()
             .where((ExecutionFrames.run_id == self.run_id) &
                    (ExecutionFrames.frame_event == 'line') &
                    (ExecutionFrames.sequence_no < self.current_frame_seq))
             .order_by(ExecutionFrames.sequence_no.desc())
             .limit(1).first())
    if frame is None:
        return self._navigate_to(self.first_line_seq, "start")
    return self._navigate_to(frame.sequence_no, "step")
```

### Reverse continue

Scans backward through the `(run_id, sequence_no)` index checking `(filename, line_no)` against the breakpoint set. Uses the DB index on `(run_id, filename, line_no)` to accelerate: query for `sequence_no < current AND filename = bp.file AND line_no = bp.line ORDER BY sequence_no DESC LIMIT 1` for each breakpoint, then take the maximum. This is O(breakpoints) indexed queries, not O(frames).

If exception breakpoints are enabled (via `set_exception_breakpoints`), the reverse-continue also queries backward for exception events — same logic as `continue_forward`: `"raised"` filter matches `frame_event == 'exception'`, `"uncaught"` filter matches `frame_event == 'exception_unwind' AND call_depth == 0`. Take the maximum `sequence_no` across all breakpoint and exception queries. This mirrors the forward logic symmetrically.

Note: breakpoints added during replay (after recording) are handled by the DB query — no special pre-recording index needed.

**`reverse_continue` implementation:**
```python
def reverse_continue(self) -> dict:
    candidates = []
    # Line breakpoints — one indexed query per breakpoint, take max
    for bp in self.breakpoints:
        hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
               .where((ExecutionFrames.run_id == self.run_id) &
                      (ExecutionFrames.filename == bp['file']) &
                      (ExecutionFrames.line_no == bp['line']) &
                      (ExecutionFrames.frame_event == 'line') &
                      (ExecutionFrames.sequence_no < self.current_frame_seq))
               .order_by(ExecutionFrames.sequence_no.desc())
               .limit(1).first())
        if hit:
            candidates.append((hit.sequence_no, "breakpoint"))
    # Exception breakpoints
    if "raised" in self.exception_filters:
        hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
               .where((ExecutionFrames.run_id == self.run_id) &
                      (ExecutionFrames.frame_event == 'exception') &
                      (ExecutionFrames.sequence_no < self.current_frame_seq))
               .order_by(ExecutionFrames.sequence_no.desc())
               .limit(1).first())
        if hit:
            candidates.append((hit.sequence_no, "exception"))
    if "uncaught" in self.exception_filters:
        hit = (ExecutionFrames.select(ExecutionFrames.sequence_no)
               .where((ExecutionFrames.run_id == self.run_id) &
                      (ExecutionFrames.frame_event == 'exception_unwind') &
                      (ExecutionFrames.call_depth == 0) &
                      (ExecutionFrames.sequence_no < self.current_frame_seq))
               .order_by(ExecutionFrames.sequence_no.desc())
               .limit(1).first())
        if hit:
            candidates.append((hit.sequence_no, "exception"))
    if not candidates:
        return self._navigate_to(self.first_line_seq, "start")
    # Take the nearest hit behind (maximum seq since all are < current)
    best_seq, reason = max(candidates, key=lambda x: x[0])
    return self._navigate_to(best_seq, reason)
```

### Reverse navigation Peewee query patterns

Add these methods to `session.py` (mirrors the forward navigation queries from Phase 3):
```python
# step_back: previous line event at any depth
ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.sequence_no < current_seq)
).order_by(ExecutionFrames.sequence_no.desc()).limit(1)

# reverse_continue: previous breakpoint match (one query per breakpoint, take max seq)
ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.filename == bp_file) &
    (ExecutionFrames.line_no == bp_line) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.sequence_no < current_seq)
).order_by(ExecutionFrames.sequence_no.desc()).limit(1)
# If "raised" exception breakpoint filter active, also query:
ExecutionFrames.select(ExecutionFrames.sequence_no).where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'exception') &
    (ExecutionFrames.sequence_no < current_seq)
).order_by(ExecutionFrames.sequence_no.desc()).limit(1)
# If "uncaught" exception breakpoint filter active, also query:
ExecutionFrames.select(ExecutionFrames.sequence_no).where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'exception_unwind') &
    (ExecutionFrames.call_depth == 0) &
    (ExecutionFrames.sequence_no < current_seq)
).order_by(ExecutionFrames.sequence_no.desc()).limit(1)
# Take the maximum sequence_no across all results — that's the previous stop point

# goto_targets: sequence numbers matching a file:line (capped at 1000 results to avoid
# multi-MB JSON responses for hot loop lines that execute millions of times)
ExecutionFrames.select(ExecutionFrames.sequence_no, ExecutionFrames.function_name).where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.filename == target_file) &
    (ExecutionFrames.line_no == target_line) &
    (ExecutionFrames.frame_event == 'line')
).order_by(ExecutionFrames.sequence_no).limit(1000)
```

**`goto_targets` implementation:**
```python
def goto_targets(self, filename: str, line: int) -> list[dict]:
    results = list(ExecutionFrames.select(
        ExecutionFrames.sequence_no, ExecutionFrames.function_name
    ).where(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.filename == filename) &
        (ExecutionFrames.line_no == line) &
        (ExecutionFrames.frame_event == 'line')
    ).order_by(ExecutionFrames.sequence_no).limit(1000).dicts())
    return [{"seq": r["sequence_no"], "function_name": r["function_name"]} for r in results]
```

### JSON-RPC messages added

**Requests (adapter -> backend):**
```json
{"jsonrpc": "2.0", "id": 17, "method": "step_back", "params": {}}
{"jsonrpc": "2.0", "id": 18, "method": "reverse_continue", "params": {}}
{"jsonrpc": "2.0", "id": 19, "method": "goto_frame", "params": {"seq": 500}}
{"jsonrpc": "2.0", "id": 20, "method": "goto_targets", "params": {"file": "...", "line": 10}}
{"jsonrpc": "2.0", "id": 21, "method": "restart_frame", "params": {"seq": 1500}}
```

**Responses (backend -> adapter):**
```json
{"jsonrpc": "2.0", "id": 17, "result": {"seq": 99, "file": "/path/script.py", "line": 5, "function_name": "foo", "reason": "step"}}
{"jsonrpc": "2.0", "id": 18, "result": {"seq": 50, "file": "/path/script.py", "line": 10, "function_name": "bar", "reason": "breakpoint"}}
{"jsonrpc": "2.0", "id": 19, "result": {"seq": 500, "file": "/path/script.py", "line": 15, "function_name": "baz", "reason": "goto"}}
{"jsonrpc": "2.0", "id": 20, "result": {"targets": [{"seq": 100, "function_name": "foo"}, {"seq": 500, "function_name": "foo"}]}}
{"jsonrpc": "2.0", "id": 21, "result": {"seq": 101, "file": "/path/script.py", "line": 6, "function_name": "foo", "reason": "goto"}}
```

Note: `continue` and `reverse_continue` do NOT pass breakpoints in params — both read from session state (set via `set_breakpoints` and `set_exception_breakpoints` RPCs). This keeps the navigation API stateless from the adapter's perspective; the adapter sends breakpoint updates once, and all navigation commands use the stored set.

### I/O hooks (`iohook.c`)

During recording, intercept non-deterministic functions by **replacing module attributes** at the C level using `PyObject_SetAttrString()`:
- Save original: `orig_time_time = PyObject_GetAttrString(time_module, "time")`
- Replace: `PyObject_SetAttrString(time_module, "time", hooked_time_time_pyfunc)`
- The hook calls the original with the same arguments, logs the return value as an `IOEvent`, and returns it

**Hook creation and calling convention:** Each hooked function is a static C function wrapped as a Python callable via `PyCFunction_New()` with a `PyMethodDef`. All hooks use `METH_VARARGS | METH_KEYWORDS` to handle any argument signature uniformly. The hook forwards arguments to the original via `PyObject_Call(orig_func, args, kwargs)`:

```c
// Example: hooked_time_time (no args), hooked_random_randint (2 args)
// Both use the same universal forwarding pattern:
// NOTE: With METH_VARARGS | METH_KEYWORDS, CPython always passes a tuple for
// args (empty if no positional args) and NULL or dict for kwargs. args is
// NEVER NULL — do not use `args ? args : PyTuple_New(0)` which would leak.
static PyObject *hooked_time_time(PyObject *self, PyObject *args, PyObject *kwargs) {
    if (g_io_replay_mode) return io_replay_next("time.time");
    PyObject *result = PyObject_Call(g_orig_time_time, args, kwargs);
    if (result == NULL) return NULL;  // propagate exception — do NOT log IOEvent
    io_log_event("time.time", result);  // synchronous IOEvent insert
    return result;
}

static PyObject *hooked_random_randint(PyObject *self, PyObject *args, PyObject *kwargs) {
    if (g_io_replay_mode) return io_replay_next("random.randint");
    PyObject *result = PyObject_Call(g_orig_random_randint, args, kwargs);
    if (result == NULL) return NULL;  // propagate exception
    io_log_event("random.randint", result);
    return result;
}
```

**Error handling in hooks:** If the original function raises an exception (returns NULL with `PyErr_Occurred()`), the hook MUST propagate the exception without logging an IOEvent. This ensures the exception propagation path during fast-forward replay matches the recording. The hooked function returns NULL and the caller sees the same exception as if the hook weren't installed.

**Pre-existing bug to fix:** In `recorder.c`, `pyttd_eval_hook_fast_forward()` (line ~729) installs `pyttd_trace_func` (the normal serializing trace function) instead of `pyttd_trace_func_fast_forward`. During fast-forward in checkpoint children, the ring buffer is destroyed, so the normal trace function wastes time serializing locals and pushing to a destroyed buffer (which silently drops via the `!g_rb.initialized` guard). This is functionally harmless but causes unnecessary overhead during fast-forward. More critically, with I/O hooks in Phase 4, the normal trace function's serialization may interact poorly with replay mode. Fix: change line ~729-730 to install `pyttd_trace_func_fast_forward` instead of `pyttd_trace_func`. The same fix applies to the condition check on line ~748 (restore path).

**Integration with `start_recording` / `stop_recording`:** The existing `pyttd_install_io_hooks` and `pyttd_remove_io_hooks` stubs in `pyttd_native.c` become **internal C functions** (remove `static` qualifier but do NOT register as Python-facing methods — remove them from the `PyttdMethods` table). They are called automatically:
- `install_io_hooks_internal(io_flush_callback, io_replay_loader)` is called at the end of `pyttd_start_recording()` (after ring buffer init, before recording begins), only if `io_flush_callback != NULL`. It `Py_INCREF`s both callbacks, imports the target modules (`time`, `random`, `os`) via `PyImport_ImportModule()`, saves originals with `PyObject_GetAttrString`, and replaces with `PyObject_SetAttrString`. If a module import fails (shouldn't happen for stdlib), skip that module's hooks gracefully (log warning, continue with other hooks).
- `remove_io_hooks_internal()` is called at the beginning of `pyttd_stop_recording()` (before stopping the flush thread). It restores original module attributes via `PyObject_SetAttrString`, then `Py_XDECREF`s the saved originals and both callbacks (`g_io_flush_callback`, `g_io_replay_loader`), setting all to NULL. Only restores hooks that were actually installed (check `g_orig_* != NULL` before restoring). Also `Py_XDECREF`s `g_io_replay_list` if non-NULL.

The `io_flush_callback` and `io_replay_loader` are stored as `static PyObject *` globals in `iohook.c` (with `Py_INCREF`/`Py_XDECREF` lifecycle management), NOT in `recorder.c`. `recorder.c` passes them through to `iohook.c` during install. `iohook.c` must `#include "recorder.h"` to access `g_sequence_counter` (for stamping `sequence_no` on IOEvents) — it is already declared `extern` in `recorder.h`.

**IOEvent storage mechanism:** The I/O hooks are C functions installed as module attributes. When a hooked function is called by the user's script, the hook has the GIL (called from Python code). It stores the IOEvent by calling the Python callback (`io_flush_callback`) with a dict: `{"sequence_no": g_sequence_counter - 1, "io_sequence": g_io_sequence++, "function_name": "time.time", "return_value": serialized_bytes}`. Note: `g_sequence_counter - 1` gives the seq of the most recent frame event (the `line` event whose bytecode triggered the I/O call). Using `g_sequence_counter` (without - 1) would stamp the *next* seq to be assigned, which doesn't correspond to any frame event. The callback inserts into the `IOEvent` table via Peewee. Unlike frame events (which go through the ring buffer for async flush), IOEvents are written synchronously because they're infrequent and must be committed before any checkpoint that follows.

**IOEvent and checkpoint ordering:** IOEvents MUST be committed to the DB before any checkpoint that follows. Since `io_log_event` does a synchronous Peewee insert with WAL mode (which auto-commits), and `checkpoint_do_fork` pauses the flush thread and forks after, all IOEvents from before the checkpoint are guaranteed to be in the DB at fork time. The child inherits the WAL state, so its `_load_io_events_for_replay` query (which runs after fork, from the inherited DB connection) will see all relevant IOEvents.

**`io_sequence` tracking:** A static `uint64_t g_io_sequence` counter in `iohook.c`, reset to 0 in `install_io_hooks_internal()`. It increments monotonically with each IOEvent logged (not reset per frame). This provides a global ordering for IOEvents within a run, and combined with `sequence_no` in the compound unique index `(run_id, sequence_no, io_sequence)`, uniquely identifies each IOEvent. During replay, the pre-loaded list is ordered by `(sequence_no, io_sequence)`, so the `g_io_replay_cursor` advances through events in the exact recording order.

**Note on PEP 523 interaction:** The hooked C functions (`PyCFunction` objects) do NOT create Python frames — they are invisible to the PEP 523 eval hook and the C-level trace function. Calling a hooked function does not increment `g_sequence_counter` or generate any frame events. The `sequence_no` stamped on the IOEvent is the seq of the most recent frame event (the `line` event whose bytecode evaluation triggered the call). This is correct because the I/O event is logically associated with the source line that caused it.

Hooks are installed at the start of recording (in `start_recording()`) and removed at the end (in `stop_recording()`).

**Limitation:** If user code captures a function reference before hooks are installed (e.g., `t = time.time` at module level), the captured reference bypasses the hook. This is documented as a known limitation.

Target functions: `time.time`, `time.monotonic`, `time.perf_counter`, `random.random`, `random.randint`, `os.urandom`.

**File I/O hooks (optional, deferred to Phase 7 if too complex):** Hook `builtins.open` to return wrapper file objects that intercept `read()`/`readline()`/`readlines()` and log results. This requires wrapping the file object protocol (iteration, context manager, `seek`, `tell`, `close`, etc.) which is substantially more complex than the scalar function hooks above. For Phase 4, **prioritize the scalar hooks** (`time.*`, `random.*`, `os.urandom`). If time permits, hook `os.read()` as a simpler alternative to full `open()` wrapping. Cold navigation of code that reads files will produce non-deterministic results without these hooks — this is acceptable and should be documented as a known limitation until file I/O hooks are implemented.

**Replay mode (inside a resumed checkpoint child):** When a checkpoint child wakes for fast-forward, the I/O hooks must switch from recording mode to replay mode. The mechanism:

1. Before the child's fast-forward begins, the parent sends the `RESUME` command. The child's checkpoint wake-up code (in `checkpoint_child_command_loop` in `checkpoint.c`) re-acquires the GIL and, before calling `recorder_set_fast_forward()`, calls `iohook_enter_replay_mode(checkpoint_seq)`. This function: (a) sets `g_io_replay_mode = 1`, (b) calls the stored `g_io_replay_loader` Python callback with `(checkpoint_seq,)` as args — the callback is a bound method on the `Recorder` instance (which is inherited via fork), so it has access to `self._run.run_id` without the C code needing to know the run_id. The callback returns a Python list of dicts ordered by `(sequence_no, io_sequence)`. (c) stores the returned list as `g_io_replay_list` (with `Py_INCREF`) and sets `g_io_replay_cursor = 0`.

2. In replay mode, each hooked function (e.g., `hooked_time_time`) checks `g_io_replay_mode`. If set, instead of calling the original function and logging, it calls `io_replay_next(function_name)` which reads the next `IOEvent` from `g_io_replay_list` at `g_io_replay_cursor`. It verifies the `function_name` matches (mismatch indicates non-determinism — log a warning and fall back to calling the original). It deserializes the `return_value` using the type-specific format (raw IEEE 754 double for floats, length-prefixed for bytes, etc.), advances `g_io_replay_cursor`, and returns the deserialized value. If the cursor exceeds the list length (more I/O calls during fast-forward than were recorded — should not happen for deterministic code), the hook falls back to calling the original function and logs a warning via `PyErr_WriteUnraisable`.

3. The pre-loaded list approach avoids per-call DB queries during fast-forward (which would be slow for tight loops calling `time.time()`). The list is ordered by `(sequence_no, io_sequence)` matching the recording order.

**Checkpoint child I/O hook state:** The forked child inherits the parent's module state, including the hooked functions (module attributes still point to the C hook functions). This is correct — during fast-forward, the hooks fire and check `g_io_replay_mode` to return pre-loaded values instead of calling the originals. The `g_io_replay_mode` flag is per-process (static in `iohook.c`), so the parent's `g_io_replay_mode = 0` is not affected by the child setting it to 1 (separate address spaces after fork). **Important:** `checkpoint_child_init()` must call `iohook_reset_child_state()` to ensure `g_io_replay_mode = 0`, `g_io_replay_list = NULL`, and `g_io_replay_cursor = 0` in the child before it enters the command loop. The replay mode is enabled later, only when a RESUME command arrives. Without this reset, if a child is forked from a parent that itself was a child in replay mode (not currently possible but defensive), stale replay state would cause incorrect behavior. Add `iohook_reset_child_state()` to `iohook.h` exports.

This is what makes cold navigation deterministic — the re-executed user code between the checkpoint frame and the target frame receives the same non-deterministic values as during the original recording.

**I/O value serialization:** Values are serialized type-specifically for faithful replay:
- `float` (time, random): `struct.pack('d', value)` — 8 bytes, exact IEEE 754 round-trip
- `int` (randint): `value.to_bytes()` with length prefix
- `bytes` (urandom, file reads): stored directly with length prefix
- `str` (file readline): UTF-8 encoded with length prefix

This avoids the lossy `repr()` -> `eval()` round-trip and the security concerns of `pickle`.

### I/O event model (`pyttd/models/io_events.py`)

```python
from peewee import AutoField, CharField, BigIntegerField, BlobField, ForeignKeyField
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs

class IOEvent(_BaseModel):
    io_event_id = AutoField()           # auto-increment integer PK
    run_id = ForeignKeyField(Runs, backref='io_events', field='run_id')
    sequence_no = BigIntegerField()     # frame seq when this I/O occurred
    io_sequence = BigIntegerField()     # ordering within same frame
    function_name = CharField()         # e.g., "time.time", "random.random"
    return_value = BlobField()          # type-specific serialized return value

    class Meta:
        indexes = (
            (('run_id', 'sequence_no', 'io_sequence'), True),
        )
```

### Schema initialization update

Update `recorder.py`'s `start()` method to include `IOEvent` in `initialize_schema`:
```python
from pyttd.models.io_events import IOEvent
storage.initialize_schema([Runs, ExecutionFrames, Checkpoint, IOEvent])
```

### I/O value serialization note

The serialization formats described above use Python notation (`struct.pack('d', value)`, `value.to_bytes()`) for clarity. In the actual C implementation (`iohook.c`), floats are stored as raw 8-byte IEEE 754 doubles (via `memcpy` or union cast), integers use variable-length encoding with a length prefix, and bytes/strings use length-prefixed binary.

### Create

- `ext/iohook.c/h` — full implementation (module attribute replacement, logging, replay mode, type-specific serialization). Exports: `install_io_hooks_internal(PyObject *io_flush_callback, PyObject *io_replay_loader)`, `remove_io_hooks_internal()`, `iohook_enter_replay_mode(uint64_t checkpoint_seq)`, `iohook_reset_child_state()`. Static globals: `g_io_replay_mode`, `g_io_replay_list`, `g_io_replay_cursor`, `g_io_sequence`, `g_io_flush_callback`, `g_io_replay_loader`, and `g_orig_*` saved originals for each hooked function.
- `pyttd/models/io_events.py` — IOEvent Peewee model
- `tests/test_iohook.py` — record script with `time.time()` and `random.random()`, verify same values on cold replay
- `tests/test_reverse_nav.py` — step_back boundary tests (reaches start, stays at first_line_seq), reverse_continue with breakpoints, reverse_continue with exception filters, goto_frame warm path, goto_targets query

### Update

- **`pyttd/session.py`** — Replace Phase 3 stubs with implementations: `step_back` (previous line event query + efficient reverse stack update), `reverse_continue` (backward breakpoint + exception filter queries, take max seq), `goto_frame` (validate target, snap to nearest line event, delegate to ReplayController, rebuild stack with `_stack_cache` optimization), `goto_targets` (file:line query capped at 1000). Add `restart_frame(frame_seq)` method (find call event, then first line in that call, delegate to `goto_frame`). Update `_update_stack` backward path to use efficient reverse scan for small gaps (see step_back stack update section). Add `"start"` reason to `_navigate_to` for backward boundary.
- **`vscode-pyttd/src/debugAdapter/pyttdDebugSession.ts`** — Add DAP handlers: `stepBackRequest`, `reverseContinueRequest`, `gotoTargetsRequest`, `gotoRequest`, `restartFrameRequest`. Update `initializeRequest` to advertise new capabilities: `supportsStepBack: true`, `supportsGotoTargetsRequest: true`, `supportsRestartFrame: true`. Add `sendStoppedForReason` mapping for `"start"` reason (→ `StoppedEvent('step')` with `description: "Beginning of recording"`). Add `"goto"` reason mapping (→ `StoppedEvent('goto')`).
- **`pyttd/server.py`** — Add RPC handlers for `step_back`, `reverse_continue`, `goto_frame`, `goto_targets`, `restart_frame` to the `_dispatch` handler map and corresponding `_handle_*` methods. Note: `_handle_goto_targets` must wrap the return value from `session.goto_targets()` (which returns `list[dict]`) as `{"targets": result}` to match the JSON-RPC response format. All handlers must check `self.session.state != "replay"` and return `{"error": "not_in_replay"}` like the existing navigation handlers.
- **`ext/recorder.c/h`** — Update `pyttd_start_recording()` to accept two additional keyword arguments: `io_flush_callback` (for recording-mode IOEvent storage) and `io_replay_loader` (for replay-mode IOEvent pre-loading). Both are passed through to `iohook.c` via `install_io_hooks_internal()`. Update `pyttd_stop_recording()` to call `remove_io_hooks_internal()` before flushing. Update the `kwlist` array and `PyArg_ParseTupleAndKeywords` format string accordingly. **Bug fix:** In `pyttd_eval_hook_fast_forward()`, change line ~729-730 to install `pyttd_trace_func_fast_forward` instead of `pyttd_trace_func`, and update the restore condition on line ~748 similarly. This avoids wasted serialization during fast-forward and prevents interaction issues with I/O hooks in replay mode.
- **`ext/checkpoint.c`** — Two changes: (1) In `checkpoint_child_init` (line ~233), after disabling recording state and before releasing GIL, call `iohook_reset_child_state()` to clear `g_io_replay_mode`, `g_io_replay_list`, and `g_io_replay_cursor` (defensive — ensures child starts in recording-hook mode, not replay mode). (2) In `checkpoint_child_command_loop` (line ~206, the RESUME handler), after re-acquiring the GIL (`PyEval_RestoreThread`) and before calling `recorder_set_fast_forward()` at line ~213, call `iohook_enter_replay_mode(recorder_get_sequence_counter())` to pre-load IOEvents and enable replay mode in the child. Use `recorder_get_sequence_counter()` (the child's current position, which equals the checkpoint's `sequence_no`) rather than the RESUME target. This requires `#include "iohook.h"`. The child's `g_sequence_counter` was inherited from the parent at fork time and never reset, so it accurately reflects the checkpoint position.
- **`ext/pyttd_native.c`** — Remove `install_io_hooks` and `remove_io_hooks` from the `PyttdMethods` table (they are now internal C functions, not Python-facing). Update the `start_recording` docstring to document the new `io_flush_callback` and `io_replay_loader` kwargs.
- **`pyttd/recorder.py`** — Update `start()` to include `IOEvent` in `initialize_schema` and pass both I/O callbacks to `start_recording()`:
  ```python
  def _on_io_event(self, event: dict):
      """Called synchronously by C I/O hooks (with GIL held) to insert a single IOEvent."""
      event['run_id'] = self._run.run_id
      IOEvent.create(**event)

  def _load_io_events_for_replay(self, after_seq: int) -> list[dict]:
      """Called by checkpoint child to pre-load IOEvents for deterministic fast-forward.
      The child process inherits this bound method via fork, so self._run.run_id
      is available without the C code needing to pass the run_id.
      Returns list of {function_name, return_value} dicts ordered by (sequence_no, io_sequence)."""
      return list(IOEvent.select(IOEvent.function_name, IOEvent.return_value)
          .where((IOEvent.run_id == self._run.run_id) & (IOEvent.sequence_no > after_seq))
          .order_by(IOEvent.sequence_no, IOEvent.io_sequence)
          .dicts())
  ```
  Then pass `io_flush_callback=self._on_io_event` and `io_replay_loader=self._load_io_events_for_replay` in the `start_recording()` call. Note: `_load_io_events_for_replay` takes only `after_seq` (NOT `run_id_bytes`) — it accesses `self._run.run_id` via the bound method. The C code calls it with `PyObject_CallFunction(g_io_replay_loader, "K", checkpoint_seq)`.
- **`pyttd/models/__init__.py`** — Add `from pyttd.models.io_events import IOEvent` to exports
- **`tests/conftest.py`** — Update `db_setup` fixture to include `IOEvent` in schema initialization:
  ```python
  from pyttd.models.io_events import IOEvent
  storage.initialize_schema([Runs, ExecutionFrames, Checkpoint, IOEvent])
  ```

### Verify

1. Step Back repeatedly from end -> each step shows previous line with correct variables
2. Step Back at beginning of recording -> stays at `first_line_seq`, returns `reason: "start"`
3. Set breakpoint on a line, click Reverse Continue -> stops at that line
4. Add a NEW breakpoint during replay, Reverse Continue -> stops at it (DB query approach)
5. Reverse Continue with no breakpoints behind -> lands on `first_line_seq`, returns `reason: "start"`
6. Reverse Continue with exception breakpoint ("raised") -> stops at exception event
7. Forward stepping (Next, Step In, Step Out) still works correctly
8. Goto frame via command palette -> jumps to arbitrary frame, stack updates correctly
9. Goto frame to a non-line event (call/return) -> snaps to nearest line event
10. Restart Frame on a nested call -> lands on first line in that function
11. Restart Frame on top-level call -> lands on first line in main script
12. Goto targets for a hot loop line -> returns at most 1000 results
13. Record a script with `time.time()` calls, cold-navigate -> verify same time values
14. Record a script with `random.random()` calls, cold-navigate -> verify same random values
15. Record a script with `random.randint(a, b)` calls -> verify arguments forwarded correctly and return values recorded
16. I/O hook does not fire for exception path (e.g., `time.time()` called but raises — exception propagates, no IOEvent logged)
17. Fast-forward uses `pyttd_trace_func_fast_forward` (not the serializing trace function) — verify by checking no ring buffer push during fast-forward
18. Performance: warm step (forward and back) < 10ms, cold jump < 300ms
19. `.venv/bin/pytest tests/test_iohook.py tests/test_reverse_nav.py tests/test_session.py tests/test_server.py` all pass
20. All 40+ existing tests still pass (no regressions from `recorder.c` and `checkpoint.c` changes)

---

## Phase 5: Timeline Scrubber Webview

**Goal:** A visual timeline panel in the Debug sidebar. Drag or click to any frame. Shows call depth bars, exception markers, breakpoint markers, current position.

### Timeline data model (`pyttd/models/timeline.py`)

```python
from peewee import fn, SQL
from pyttd.models.frames import ExecutionFrames


def get_timeline_summary(run_id, start_seq, end_seq, bucket_count=500,
                         breakpoints=None) -> list[dict]:
    """Return downsampled timeline data for rendering.

    Each bucket: {startSeq, endSeq, maxCallDepth, hasException,
                  hasBreakpoint, dominantFunction}

    Uses SQL GROUP BY on computed bucket index from ExecutionFrames.
    breakpoints is a list of {file: str, line: int} dicts from session state.
    """
    total_range = end_seq - start_seq
    if total_range <= 0 or bucket_count <= 0:
        return []
    bucket_size = max(1, total_range // bucket_count)

    # Compute bucket index as (sequence_no - start_seq) / bucket_size
    bucket_expr = SQL(
        '(("sequence_no" - ?) / ?)', start_seq, bucket_size
    )

    # Query ALL event types (not just 'line') so exception events are counted.
    # dominantFunction: SQLite picks one row's function_name per group for
    # the bare (non-aggregated) column — the choice is indeterminate per the
    # SQL standard, but in practice SQLite returns the first row by insertion
    # order. Since events are inserted in sequence_no order, this gives the
    # earliest event's function in each bucket. An acceptable approximation
    # for a display-only label (exact mode requires a correlated subquery).
    rows = (ExecutionFrames.select(
                fn.MIN(ExecutionFrames.sequence_no).alias('start_seq'),
                fn.MAX(ExecutionFrames.sequence_no).alias('end_seq'),
                fn.MAX(ExecutionFrames.call_depth).alias('max_depth'),
                fn.SUM(SQL("CASE WHEN frame_event IN ('exception', 'exception_unwind') "
                           "THEN 1 ELSE 0 END")).alias('exc_count'),
                ExecutionFrames.function_name,
            )
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.sequence_no >= start_seq) &
                   (ExecutionFrames.sequence_no <= end_seq))
            .group_by(bucket_expr)
            .order_by(bucket_expr)
            .dicts())

    # Build breakpoint lookup set for O(1) matching
    bp_set = set()
    if breakpoints:
        bp_set = {(bp['file'], bp['line']) for bp in breakpoints
                  if 'file' in bp and 'line' in bp}

    buckets = []
    for row in rows:
        has_bp = False
        if bp_set:
            # Check if any line event in this bucket's range matches a breakpoint.
            # Only query if breakpoints exist (avoids per-bucket queries otherwise).
            # Build OR conditions for each breakpoint (filename, line_no) pair.
            from functools import reduce
            import operator
            bp_conditions = [
                ((ExecutionFrames.filename == f) & (ExecutionFrames.line_no == l))
                for f, l in bp_set
            ]
            has_bp = ExecutionFrames.select().where(
                (ExecutionFrames.run_id == run_id) &
                (ExecutionFrames.sequence_no >= row['start_seq']) &
                (ExecutionFrames.sequence_no <= row['end_seq']) &
                (ExecutionFrames.frame_event == 'line') &
                reduce(operator.or_, bp_conditions)
            ).exists()

        buckets.append({
            'startSeq': row['start_seq'],
            'endSeq': row['end_seq'],
            'maxCallDepth': row['max_depth'] or 0,
            'hasException': (row['exc_count'] or 0) > 0,
            'hasBreakpoint': has_bp,
            'dominantFunction': row['function_name'] or '',
        })

    return buckets
```

This is a query module, not a Peewee model. It aggregates data from `ExecutionFrames` using SQL `GROUP BY` on computed bucket indices. **`hasBreakpoint`** is determined by cross-referencing with the session's breakpoint list (passed from the server handler, not stored in DB). **`dominantFunction`** uses the function name from a representative event in each bucket — SQLite's choice of row for the non-aggregated `function_name` column is indeterminate per the SQL standard (in practice, typically the first row by insertion order). Computing the exact mode (most frequent) would require a correlated subquery per bucket, which is expensive for 500 buckets. The representative function is an acceptable display-only heuristic. **Note:** The `hasBreakpoint` check uses per-bucket queries, which adds overhead proportional to `bucket_count × |breakpoints|`. For typical usage (500 buckets, <20 breakpoints), this is acceptable. If profiling shows it's too slow, optimize with a single pre-query that returns all (filename, line_no, sequence_no) triples matching any breakpoint in the full range, then bucket-assign in Python.

**Alternative `hasBreakpoint` strategy (simpler, no per-bucket query):** Instead of per-bucket DB queries, do a single query for all `line` events in the full range matching any breakpoint `(filename, line_no)` pair, then assign each match to its bucket by `(sequence_no - start_seq) // bucket_size`. This is O(1) DB query + O(matches) Python work:

```python
if bp_set:
    # Single query: find all breakpoint-matching seqs in range
    conditions = [
        ((ExecutionFrames.filename == f) & (ExecutionFrames.line_no == l))
        for f, l in bp_set
    ]
    from functools import reduce
    import operator
    bp_hits = set(
        row.sequence_no for row in
        ExecutionFrames.select(ExecutionFrames.sequence_no)
        .where((ExecutionFrames.run_id == run_id) &
               (ExecutionFrames.sequence_no >= start_seq) &
               (ExecutionFrames.sequence_no <= end_seq) &
               (ExecutionFrames.frame_event == 'line') &
               reduce(operator.or_, conditions))
    )
    # Mark buckets that contain any hit
    bp_buckets = {(seq - start_seq) // bucket_size for seq in bp_hits}
```

Use this approach in the actual implementation (the first code block above shows the per-bucket approach for clarity of intent).

### Implementation

`WebviewViewProvider` creates a `<canvas>`-based timeline. Register in `package.json` under `contributes.views` in the `debug` view container:

```jsonc
"contributes": {
  "views": {
    "debug": [{
      "type": "webview",
      "id": "pyttd.timeline",
      "name": "Timeline",
      "when": "debugType == 'pyttd'"
    }]
  }
}
```

Python backend sends downsampled timeline data (~500 buckets) with `{startSeq, endSeq, maxCallDepth, hasException, hasBreakpoint, dominantFunction}`. Canvas renders vertical bars (height = call depth), red for exceptions, blue for breakpoint regions, yellow vertical line for current position.

**Theming:** The webview uses VSCode's CSS custom properties (`--vscode-editor-background`, `--vscode-editor-foreground`, `--vscode-charts-*`) so it matches the user's dark/light/custom theme.

**Interaction:**
- Mouse click on canvas -> compute target `seq` from X position -> post `{type: 'scrub', seq}` message to extension -> extension calls `vscode.debug.activeDebugSession?.customRequest('goto', { threadId: 1, targetId: seq })` (standard DAP `goto` request — `targetId` IS `seq` by the Phase 4 convention). The adapter's existing `gotoRequest` handler processes this.
- Mouse drag on canvas -> visual cursor updates immediately on canvas (no round-trip), actual `goto` request fires only on mouseup (or throttled to max one request per 150ms during drag via `requestAnimationFrame` + pending-request guard). This prevents flooding the RPC channel during fast drags.
- Keyboard (only when timeline webview has focus): Left/Right arrow = post step_back/step_forward message to extension (extension calls `customRequest('stepBack')`/`customRequest('next')`), Home = goto frame 0, End = goto last frame, PageUp/PageDown = jump by 10% of total frames (computed client-side, sent as `goto` request).
- Mousewheel on canvas = zoom in/out on timeline. Zoom re-requests buckets from the backend at higher resolution for the visible range (`get_timeline_summary` with narrower `startSeq`/`endSeq`). Cache the previous bucket set client-side (keyed by `startSeq:endSeq:bucketCount`) to avoid round-trip lag during fast zoom. Evict cache entries older than 3 zoom levels.

**Communication:** Custom DAP events via `sendEvent()` with the `Event` base class from `@vscode/debugadapter`:
```typescript
// In pyttdDebugSession.ts:
this.sendEvent(new Event('pyttd/timelineData', { buckets: [...], totalFrames: 25000 }));
this.sendEvent(new Event('pyttd/positionChanged', { seq: 1500, file: '...', line: 10 }));
```

**Timeline data flow (initial):**
1. Recording completes → server sends `stopped` notification with `{seq, reason, totalFrames}`
2. Adapter's `handleNotification` receives `stopped` → stores `totalFrames` in `this.totalFrames`
3. Adapter immediately sends `get_timeline_summary` RPC: `{startSeq: 0, endSeq: totalFrames, bucketCount: 500}`
4. On response, adapter emits `pyttd/timelineData` custom event: `{buckets: [...], totalFrames, startSeq: 0, endSeq: totalFrames}`
5. Extension receives via `debug.onDidReceiveDebugSessionCustomEvent`, relays to webview via `postMessage`

**Timeline data flow (zoom):**
1. Webview posts `{type: 'zoom', startSeq, endSeq}` to extension
2. Extension calls `customRequest('get_timeline_summary', {startSeq, endSeq, bucketCount: 500})`
3. Adapter sends RPC, receives response, emits `pyttd/timelineData` with updated range
4. Extension relays to webview

**Position change flow:**
- `pyttd/positionChanged` is emitted from `sendStoppedForReason` (which all navigation handlers call). This ensures every navigation action — step, continue, goto, scrub — updates the timeline cursor without modifying each handler individually.
- The event carries `{seq, file, line}` from the navigation result. `sendStoppedForReason` receives these values (see Update section for the signature change).

**Breakpoint change refresh:** When breakpoints change during replay (`setBreakPointsRequest`), the timeline's `hasBreakpoint` markers become stale. After forwarding breakpoints to the backend, the adapter re-requests timeline data for the current visible range to refresh breakpoint markers. Add to `setBreakPointsRequest`:
```typescript
// After forwarding to backend, refresh timeline breakpoint markers
if (this.isReplaying) {
    this.backend.sendRequest('get_timeline_summary', {
        startSeq: this.timelineStartSeq ?? 0,
        endSeq: this.timelineEndSeq ?? this.totalFrames,
        bucketCount: 500,
    }).then((result: any) => {
        this.sendEvent(new Event('pyttd/timelineData', {
            buckets: result.buckets,
            totalFrames: this.totalFrames,
            startSeq: this.timelineStartSeq ?? 0,
            endSeq: this.timelineEndSeq ?? this.totalFrames,
        }));
    }).catch(() => {});
}
```
This requires tracking the current visible range in `private timelineStartSeq: number | null = null;` and `private timelineEndSeq: number | null = null;`, updated whenever timeline data is sent.

Extension main (`extension.ts`) listens for these custom events via `debug.onDidReceiveDebugSessionCustomEvent` and relays to webview via `postMessage`.

### JSON-RPC messages added

Request:
```json
{"jsonrpc": "2.0", "id": 21, "method": "get_timeline_summary", "params": {"startSeq": 0, "endSeq": 25000, "bucketCount": 500}}
```

Response:
```json
{"jsonrpc": "2.0", "id": 21, "result": {"buckets": [
  {"startSeq": 0, "endSeq": 49, "maxCallDepth": 3, "hasException": false, "hasBreakpoint": true, "dominantFunction": "main"},
  {"startSeq": 50, "endSeq": 99, "maxCallDepth": 5, "hasException": true, "hasBreakpoint": false, "dominantFunction": "process_data"}
]}}
```

Note: `run_id` is not in the request params — the server handler uses `self.session.run_id` (same pattern as all other replay-mode handlers).

### Create

- `pyttd/models/timeline.py` — timeline summary query function (SQL bucket aggregation over ExecutionFrames)
- `vscode-pyttd/src/views/timelineScrubberProvider.ts` — `WebviewViewProvider` implementation (creates `<canvas>` webview, handles `postMessage` from webview, holds reference to webview panel for incoming event relay)
- `vscode-pyttd/src/views/timelineScrubber.html` — timeline webview HTML (canvas element, script/style includes). **Must include a Content Security Policy** `<meta>` tag — without CSP, VSCode silently blocks inline scripts and the webview renders blank. Use nonce-based policy: the provider generates a random nonce per webview resolve, injects it into the HTML template via `${nonce}`, and the CSP allows `script-src 'nonce-${nonce}'`. External resources use `${webview.cspSource}` as the allowed source
- `vscode-pyttd/src/views/timelineScrubber.js` — canvas rendering (bar chart with depth-scaled heights, color coding), interaction handlers (click, drag with throttle, mousewheel zoom, keyboard), `window.addEventListener('message')` for incoming data/position updates from extension
- `vscode-pyttd/src/views/timelineScrubber.css` — themed styles using VSCode CSS custom properties
- `tests/test_timeline.py` — test `get_timeline_summary` with known frame data: bucket boundaries, exception detection, breakpoint matching, dominant function, empty ranges, single-event buckets, zoom (sub-range queries)

### Update

- **`vscode-pyttd/package.json`** — Add `views` section under `contributes` (alongside existing `debuggers`):
  ```jsonc
  "views": {
    "debug": [{
      "type": "webview",
      "id": "pyttd.timeline",
      "name": "Timeline",
      "when": "debugType == 'pyttd'"
    }]
  }
  ```

- **`pyttd/server.py`** — Add `get_timeline_summary` to the dispatch table and implement `_handle_get_timeline_summary`:
  ```python
  def _handle_get_timeline_summary(self, params: dict) -> dict:
      if self.session.state != "replay":
          return {"error": "not_in_replay"}
      from pyttd.models.timeline import get_timeline_summary
      start_seq = params.get("startSeq", 0)
      end_seq = params.get("endSeq", self.session.last_line_seq or 0)
      bucket_count = params.get("bucketCount", 500)
      buckets = get_timeline_summary(
          self.session.run_id, start_seq, end_seq, bucket_count,
          breakpoints=self.session.breakpoints)
      return {"buckets": buckets}
  ```
  Note: passes `self.session.breakpoints` to the query function so `hasBreakpoint` can be computed. The response does NOT include `totalFrames` — the adapter already caches this value from the `stopped` notification and attaches it to the `pyttd/timelineData` event itself (avoids the confusing distinction between total recording frames and the queried range size).

- **`vscode-pyttd/src/debugAdapter/pyttdDebugSession.ts`** — Five changes:
  1. Add `Event` to the import from `@vscode/debugadapter` (currently not imported — only the specific event subclasses like `StoppedEvent` are). Add new fields: `private totalFrames: number = 0;`, `private timelineStartSeq: number | null = null;`, `private timelineEndSeq: number | null = null;`.
  2. Update `handleNotification` for `stopped` case: store `params.totalFrames` in `this.totalFrames`, then request timeline data:
     ```typescript
     case 'stopped':
         this.currentSeq = params.seq;
         this.totalFrames = params.totalFrames || 0;
         this.isReplaying = true;
         this.sendEvent(new ProgressEndEvent(this.progressId));
         this.sendEvent(new StoppedEvent('entry', 1));
         // Send initial position to timeline
         this.sendEvent(new Event('pyttd/positionChanged', {
             seq: params.seq,
         }));
         // Request initial timeline data
         this.timelineStartSeq = 0;
         this.timelineEndSeq = this.totalFrames;
         this.backend.sendRequest('get_timeline_summary', {
             startSeq: 0, endSeq: this.totalFrames, bucketCount: 500
         }).then((result: any) => {
             this.sendEvent(new Event('pyttd/timelineData', {
                 buckets: result.buckets,
                 totalFrames: this.totalFrames,
                 startSeq: 0,
                 endSeq: this.totalFrames,
             }));
         }).catch(() => {}); // Timeline is non-critical — don't break debug session
         break;
     ```
  3. Update `sendStoppedForReason` signature to accept navigation result and emit `pyttd/positionChanged`:
     ```typescript
     private sendStoppedForReason(reason: string, navResult?: { seq: number; file?: string; line?: number }): void {
         // ... existing switch/case logic unchanged ...
         // After sending StoppedEvent, also send position update for timeline
         if (navResult) {
             this.sendEvent(new Event('pyttd/positionChanged', {
                 seq: navResult.seq,
                 file: navResult.file,
                 line: navResult.line,
             }));
         }
     }
     ```
     Update all callers to pass the navigation result: `this.sendStoppedForReason(result.reason, result)`. Also add a custom request handler for `get_timeline_summary` (for zoom requests from the webview):
     ```typescript
     protected customRequest(command: string, response: DebugProtocol.Response, args: any): void {
         if (command === 'get_timeline_summary') {
             this.backend.sendRequest('get_timeline_summary', args)
                 .then((result: any) => {
                     this.timelineStartSeq = args.startSeq;
                     this.timelineEndSeq = args.endSeq;
                     this.sendEvent(new Event('pyttd/timelineData', {
                         buckets: result.buckets,
                         totalFrames: this.totalFrames,
                         startSeq: args.startSeq,
                         endSeq: args.endSeq,
                     }));
                     this.sendResponse(response);
                 })
                 .catch((err: Error) => {
                     this.sendErrorResponse(response, 1, err.message);
                 });
         } else {
             super.customRequest(command, response, args);
         }
     }
     ```
  4. Update `setBreakPointsRequest` to refresh timeline breakpoint markers after forwarding to backend (see "Breakpoint change refresh" in the Communication section above). This re-requests `get_timeline_summary` for the current visible range so `hasBreakpoint` markers update immediately when the user adds/removes breakpoints during replay.

- **`vscode-pyttd/src/extension.ts`** — Replace the Phase 5 comment with actual registration:
  ```typescript
  // Register timeline webview provider
  const timelineProvider = new TimelineScrubberProvider(context.extensionUri);
  context.subscriptions.push(
      vscode.window.registerWebviewViewProvider('pyttd.timeline', timelineProvider)
  );

  // Relay custom debug events to timeline webview
  context.subscriptions.push(
      vscode.debug.onDidReceiveDebugSessionCustomEvent((e) => {
          if (e.session.type !== 'pyttd') return;
          if (e.event === 'pyttd/timelineData' || e.event === 'pyttd/positionChanged') {
              timelineProvider.postMessage({ type: e.event, data: e.body });
          }
      })
  );
  ```
  Import `TimelineScrubberProvider` from `./views/timelineScrubberProvider`.

### Verify

1. Timeline panel appears in Debug sidebar when a pyttd session is active
2. Timeline panel is hidden when no pyttd debug session is active (`when` clause)
3. Drag scrubber -> editor cursor, variables, and stack all update
4. Click on timeline -> jumps to clicked frame position
5. Step back/forward in debug toolbar -> timeline cursor moves in sync
6. Keyboard navigation works (arrow keys, Home/End, PageUp/PageDown) when timeline has focus
7. Exception markers visible as red bars at correct positions
8. Breakpoint markers visible at correct positions (re-verified after changing breakpoints)
9. Zoom in/out shows higher/lower resolution (bucket count stays at 500 but range narrows)
10. Smooth interaction at 60fps for recordings with 100k+ frames (canvas repaints only, goto throttled)
11. Renders correctly in both dark and light themes
12. `.venv/bin/pytest tests/test_timeline.py` passes (bucket boundaries, exception detection, breakpoint matching, empty ranges)

---

## Phase 6: CodeLens, Inline Values, Call History Tree

**Goal:** Rich editor integration — execution stats above functions, variable values inline during navigation, and a collapsible call tree in the Debug sidebar.

### Backend: session.py methods

Three new methods on `Session`, plus corresponding server handlers. All methods require `self.state == "replay"`.

#### `get_traced_files()`

Returns the set of filenames that appear in the recording. Used by the CodeLens provider to avoid querying files that aren't in the trace.

```python
def get_traced_files(self) -> list[str]:
    rows = (ExecutionFrames.select(ExecutionFrames.filename)
            .where(ExecutionFrames.run_id == self.run_id)
            .distinct())
    return [row.filename for row in rows]
```

Uses the existing `(run_id, filename, line_no)` index. Typically returns <50 files — no performance concern.

#### `get_execution_stats(filename)`

Returns per-function execution statistics for a single file. Single `GROUP BY` query — no per-function round-trips.

```python
def get_execution_stats(self, filename: str) -> list[dict]:
    from peewee import fn, SQL
    rows = list(ExecutionFrames.select(
        ExecutionFrames.function_name,
        fn.SUM(SQL("CASE WHEN frame_event = 'call' THEN 1 ELSE 0 END")).alias('call_count'),
        fn.SUM(SQL("CASE WHEN frame_event = 'exception_unwind' "
                   "THEN 1 ELSE 0 END")).alias('exception_count'),
        fn.MIN(SQL("CASE WHEN frame_event = 'call' THEN sequence_no END")).alias('first_call_seq'),
        fn.MIN(SQL("CASE WHEN frame_event = 'call' THEN line_no END")).alias('def_line'),
    ).where(
        (ExecutionFrames.run_id == self.run_id) &
        (ExecutionFrames.filename == filename)
    ).group_by(ExecutionFrames.function_name).dicts())

    return [{
        'functionName': r['function_name'],
        'callCount': r['call_count'] or 0,
        'exceptionCount': r['exception_count'] or 0,
        'firstCallSeq': r['first_call_seq'],
        'defLine': r['def_line'],
    } for r in rows if r['call_count']]
```

**Path matching:** No `os.path.realpath()` normalization — match the filename as stored in the DB. The recorder stores whatever `PyCode_GetFilename()` returns (typically the absolute path passed to `runpy.run_path()`). The CodeLens provider must send filenames that match the DB values. The `get_traced_files()` response provides the canonical filenames from the DB; the CodeLens provider should compare `document.uri.fsPath` against that set, and pass the matching DB filename (not the fsPath) to `get_execution_stats()`. This avoids symlink/case mismatches.

**Exception counting:** Only counts `exception_unwind` events (frame exited via exception propagation), NOT `exception` events (exception raised within frame, may be caught). A single propagated exception generates both event types — counting both would inflate the number 2x. `exception_unwind` count equals "number of calls that ended abnormally", which is the useful metric for CodeLens display.

`defLine` is the line number from the first `call` event — CPython sets `f_lineno` to `co_firstlineno` (the `def` line) at frame entry. Used by the CodeLens provider to position annotations without fragile regex matching. `firstCallSeq` is used for the "jump to first execution" click action. Functions with `call_count == 0` are excluded (defensive — shouldn't happen in normal recordings but could if recording started mid-execution).

**Known limitation:** The query groups by `function_name` alone (`co_qualname`). Qualified names handle most cases (e.g., `A.process` vs `B.process`), but multiple lambdas or comprehensions in the same file share qualnames like `<lambda>` or `<listcomp>` and will be merged into a single CodeLens entry. The CodeLens will show combined stats and navigate to the first occurrence. Acceptable for Phase 6 — a future improvement could group by `(function_name, def_line)` using a subquery.

#### `get_call_children(parent_call_seq, parent_return_seq)`

Returns direct child calls within a parent call's scope. Uses a single query to fetch all `call`/`return`/`exception_unwind` events at the target depth within the parent's sequence range, then pairs them sequentially in Python. O(1) DB queries + O(n) Python work.

```python
def get_call_children(self, parent_call_seq=None, parent_return_seq=None) -> list[dict]:
    if parent_call_seq is None:
        # Root level: get all depth-0 call/return pairs
        target_depth = 0
        range_filter = (ExecutionFrames.sequence_no >= 0)
    else:
        parent = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.sequence_no == parent_call_seq))
        if not parent:
            return []
        target_depth = parent.call_depth + 1
        if parent_return_seq is not None:
            range_filter = (
                (ExecutionFrames.sequence_no > parent_call_seq) &
                (ExecutionFrames.sequence_no < parent_return_seq))
        else:
            # Incomplete parent call — no upper bound
            range_filter = (ExecutionFrames.sequence_no > parent_call_seq)

    events = list(ExecutionFrames.select()
        .where(
            (ExecutionFrames.run_id == self.run_id) &
            (ExecutionFrames.frame_event.in_(['call', 'return', 'exception_unwind'])) &
            (ExecutionFrames.call_depth == target_depth) &
            range_filter)
        .order_by(ExecutionFrames.sequence_no))

    # Pair sequential call/return events
    results = []
    i = 0
    while i < len(events):
        ev = events[i]
        if ev.frame_event != 'call':
            i += 1
            continue
        return_ev = None
        if (i + 1 < len(events) and
                events[i + 1].frame_event in ('return', 'exception_unwind')):
            return_ev = events[i + 1]
            i += 2
        else:
            i += 1
        results.append({
            'callSeq': ev.sequence_no,
            'returnSeq': return_ev.sequence_no if return_ev else None,
            'functionName': ev.function_name,
            'filename': ev.filename,
            'line': ev.line_no,
            'depth': ev.call_depth,
            'hasException': (return_ev.frame_event == 'exception_unwind'
                             if return_ev else False),
            'isComplete': return_ev is not None,
        })
    return results
```

**Pairing correctness:** At a given depth within a parent's range, events at exactly that depth form strict `call → return` pairs (nested events at deeper levels are filtered out by `call_depth == target_depth`). The only exception is an incomplete recording where the last `call` has no matching `return`. Uses the `(run_id, call_depth, sequence_no)` index.

**Recursive functions:** Handled naturally — each recursive call is at a deeper depth, so each tree level shows one invocation. Expanding a node reveals child calls at `parent_depth + 1`.

**Root-level query:** When `parent_call_seq` is `None`, the method returns all depth-0 calls. For a typical script, this is a single `<module>` entry whose children are the top-level function calls.

### Server dispatch (server.py)

Add three handlers to the dispatch table (after `get_timeline_summary`, before `disconnect`):

```python
"get_traced_files": self._handle_get_traced_files,
"get_execution_stats": self._handle_get_execution_stats,
"get_call_children": self._handle_get_call_children,
```

Handlers:

```python
def _handle_get_traced_files(self, params: dict) -> dict:
    if self.session.state != "replay":
        return {"error": "not_in_replay"}
    return {"files": self.session.get_traced_files()}

def _handle_get_execution_stats(self, params: dict) -> dict:
    if self.session.state != "replay":
        return {"error": "not_in_replay"}
    filename = params.get("filename", "")
    return {"stats": self.session.get_execution_stats(filename)}

def _handle_get_call_children(self, params: dict) -> dict:
    if self.session.state != "replay":
        return {"error": "not_in_replay"}
    parent_call_seq = params.get("parentCallSeq")
    parent_return_seq = params.get("parentReturnSeq")
    return {"children": self.session.get_call_children(parent_call_seq, parent_return_seq)}
```

### CodeLens Provider (`codeLensProvider.ts`)

Shows "TTD: 47 calls | 3 exceptions" above each traced function definition. Click navigates to the first execution of that function via `goto_frame` (which snaps the `call` event to its first `line` event automatically).

**Activation guard:** Only activates during a pyttd debug session (`activeDebugSession?.type === 'pyttd'`). On session start, queries `get_traced_files` and caches the result as a `Map<string, string>` keyed by `fsPath` → DB filename. Only provides CodeLens for documents whose `fsPath` matches a traced file.

**Path matching:** `get_traced_files` returns DB-stored filenames (from `PyCode_GetFilename()`). The CodeLens provider builds a lookup map: for each DB filename, normalize to filesystem path and map it to the original DB filename. When `provideCodeLenses` is called, look up `document.uri.fsPath` in the map. If found, call `get_execution_stats` with the DB filename (not the fsPath) to ensure exact match against the DB. This avoids symlink/case normalization mismatches.

**Refresh triggers:** `onDidChangeCodeLenses` fires when debug session starts (calls `get_traced_files` to populate cache) and when debug session ends (clears cache).

**CodeLens positioning:** Uses `defLine` from `get_execution_stats` response instead of regex-matching `def` statements. Avoids fragile text parsing. The CodeLens `Range` is positioned at `(defLine - 1, 0)` (0-indexed from the 1-indexed DB value).

**Click command:** Registers `pyttd.gotoFirstExecution` command in `extension.ts`. Handler calls `session.customRequest('goto_frame', { target_seq: firstCallSeq })`. Since `firstCallSeq` is a `call` event, `goto_frame` handles the `call` → `line` snap automatically (same mechanism as `restart_frame`).

**Registration:** `vscode.languages.registerCodeLensProvider({ language: 'python' }, provider)` in `extension.ts`. No `package.json` changes needed — CodeLens is registered programmatically, not declaratively. The `contributes.languages` contribution point is for defining new language grammars, NOT for CodeLens registration.

### Inline Values Provider (`inlineValuesProvider.ts`)

Shows variable values inline in the editor during time-travel navigation. Reuses the existing `get_variables` RPC — no new backend method needed.

**Flow:**
1. VSCode calls `provideInlineValues(document, viewPort, context)` on every stop event
2. Provider calls `session.customRequest('get_variables', { seq: context.frameId })` — `frameId` is the sequence number (pyttd convention from `stackTraceRequest`)
3. Receives `{variables: [{name, value, type}]}`
4. Scans visible lines (`viewPort.start.line` to `viewPort.end.line`) for variable name occurrences using word-boundary regex (`\bvarName\b`)
5. Returns `InlineValueText(range, `${name} = ${value}`)` for each match

**Cancellation:** VSCode passes a `CancellationToken` and cancels stale requests during rapid navigation. No manual debouncing needed — VSCode handles this natively.

**No new RPC needed:** The existing `get_variables` handler returns all locals at a sequence number. Filtering to visible lines happens in the extension via simple string matching against document text. The backend doesn't have access to the editor viewport, so client-side filtering is the correct design.

**Registration:** `vscode.languages.registerInlineValuesProvider({ language: 'python' }, provider)` in `extension.ts`.

### Call History Tree (`callHistoryProvider.ts`)

`TreeDataProvider` in the Debug sidebar. Collapsible tree built from `call`/`return` event pairs via `get_call_children`. Each node shows function name, filename:line, and sequence range. Click navigates to that call's first line event (via `goto_frame` which auto-snaps `call` → `line`).

Register in `package.json` (add to existing `contributes.views.debug` array alongside the timeline webview):

```jsonc
"contributes": {
  "views": {
    "debug": [
      { /* existing pyttd.timeline entry */ },
      {
        "id": "pyttd.callHistory",
        "name": "Call History",
        "when": "debugType == 'pyttd'"
      }
    ]
  }
}
```

**`TreeItem` per call node:**
- `label`: `functionName` (e.g., `process_data`)
- `description`: `basename(filename):line` (e.g., `script.py:42`)
- `tooltip`: `functionName at filename:line (seq callSeq–returnSeq)`
- `iconPath`: `ThemeIcon('symbol-function')` for normal calls, `ThemeIcon('error')` for calls with `hasException`
- `collapsibleState`: `Collapsed` if `isComplete` (children available to expand), `None` if incomplete (no expand arrow)
- `command`: `pyttd.gotoCallFrame` — calls `customRequest('goto_frame', { target_seq: callSeq })`

**`getChildren(element?)`** calls `customRequest('get_call_children', params)`:
- Root (no element): `{}` → server returns depth-0 calls
- Expand node: `{ parentCallSeq: element.callSeq, parentReturnSeq: element.returnSeq }` → server returns direct child calls within that scope

**Lazy loading:** Children are fetched on expand (single backend query per expand). No upfront tree construction.

**Incomplete recordings:** Calls without a matching return (recording stopped mid-execution) have `returnSeq: null` and `isComplete: false`. Shown with `collapsibleState: None` (no expand arrow) and an "(incomplete)" label suffix. If `parentReturnSeq` is `null` in `get_call_children`, the backend uses no upper bound.

**Refresh:** `onDidChangeTreeData` fires once when entering replay mode (triggered by first `pyttd/timelineData` event from the debug adapter) and on session termination (to clear the tree). Does NOT fire on every navigation step.

**Registration:** `vscode.window.registerTreeDataProvider('pyttd.callHistory', provider)` in `extension.ts`. Click command: register `pyttd.gotoCallFrame` (same mechanism as CodeLens click — calls `goto_frame`).

### Debug Adapter changes (pyttdDebugSession.ts)

Expand `customRequest` to handle Phase 6 RPCs. Two categories: navigation handlers (need state updates + stopped events) and query pass-throughs (simple forwarding):

```typescript
protected customRequest(command: string, response: DebugProtocol.Response, args: any): void {
    if (command === 'get_timeline_summary') {
        // ... existing handler (stores timelineStartSeq/EndSeq, emits event) ...
    } else if (command === 'goto_frame') {
        // Navigation handler — NOT a simple pass-through.
        // Must update currentSeq and emit stopped event (same as gotoRequest).
        // CodeLens and Call History commands use this path.
        if (!this.isReplaying) {
            this.sendResponse(response);
            return;
        }
        this.backend.sendRequest('goto_frame', args || {})
            .then((result: any) => {
                this.currentSeq = result.seq;
                this.sendResponse(response);
                this.sendStoppedForReason(result.reason || 'goto', result);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    } else if (['get_execution_stats', 'get_traced_files',
                'get_call_children', 'get_variables'].includes(command)) {
        // Query pass-throughs — no state modification, just forward and return.
        this.backend.sendRequest(command, args || {})
            .then((result: any) => {
                response.body = result;
                this.sendResponse(response);
            })
            .catch((err: Error) => {
                this.sendErrorResponse(response, 1, err.message);
            });
    } else {
        super.customRequest(command, response, args);
    }
}
```

**Why `goto_frame` needs special handling:** The existing `gotoRequest` DAP handler (standard goto) calls `goto_frame` on the backend and updates `currentSeq` + emits `StoppedEvent` + `pyttd/positionChanged`. The CodeLens and Call History commands use `session.customRequest('goto_frame', ...)` from the extension host, which routes through `customRequest` — NOT through `gotoRequest`. Without this handler, `goto_frame` falls through to `super.customRequest()` and does nothing.

**Why `goto_frame` duplicates `gotoRequest` logic:** The two entry points serve different callers — `gotoRequest` is triggered by VSCode's native goto mechanism (via `gotoTargets` → click), `customRequest('goto_frame')` is triggered by extension-side commands (CodeLens click, Call History click). Both need the same state updates. A helper method could deduplicate, but the duplication is small (4 lines) and the explicit handling is clearer.

Note: `get_variables` is added to the pass-through list so the `InlineValuesProvider` can call it via `customRequest`. The existing `variablesRequest` DAP handler uses a different mechanism (variablesReference encoding → decodes to seq → sends `get_variables({seq})` to backend). The `customRequest` path sends `{seq}` directly — same backend parameter format, different DAP entry point.

### Extension registration (extension.ts)

Replace the Phase 6 comment with actual registration. Import all three providers:

```typescript
import { PyttdCodeLensProvider } from './providers/codeLensProvider';
import { PyttdInlineValuesProvider } from './providers/inlineValuesProvider';
import { PyttdCallHistoryProvider } from './providers/callHistoryProvider';

// Inside activate():

const codeLensProvider = new PyttdCodeLensProvider();
const callHistoryProvider = new PyttdCallHistoryProvider();
let callHistoryRefreshed = false;

context.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ language: 'python' }, codeLensProvider),
    vscode.languages.registerInlineValuesProvider({ language: 'python' }, new PyttdInlineValuesProvider()),
    vscode.window.registerTreeDataProvider('pyttd.callHistory', callHistoryProvider),
    vscode.commands.registerCommand('pyttd.gotoFirstExecution', (seq: number) => {
        vscode.debug.activeDebugSession?.customRequest('goto_frame', { target_seq: seq });
    }),
    vscode.commands.registerCommand('pyttd.gotoCallFrame', (seq: number) => {
        vscode.debug.activeDebugSession?.customRequest('goto_frame', { target_seq: seq });
    }),
);

// Debug session lifecycle — single handler per event type (no duplicate listeners)
context.subscriptions.push(
    vscode.debug.onDidStartDebugSession((session) => {
        if (session.type === 'pyttd') {
            codeLensProvider.refresh();
        }
    }),
    vscode.debug.onDidTerminateDebugSession((session) => {
        if (session.type === 'pyttd') {
            codeLensProvider.refresh();
            callHistoryProvider.refresh();
            callHistoryRefreshed = false;
        }
    }),
);
```

Update the existing `onDidReceiveDebugSessionCustomEvent` handler (lines 21-28 of current `extension.ts`) to also refresh the call history tree on first timeline data. **Replace** the existing handler — do NOT add a second `onDidReceiveDebugSessionCustomEvent` listener:

```typescript
// Replace existing custom event relay (lines 21-28) with:
context.subscriptions.push(
    vscode.debug.onDidReceiveDebugSessionCustomEvent((e) => {
        if (e.session.type !== 'pyttd') return;
        if (e.event === 'pyttd/timelineData' || e.event === 'pyttd/positionChanged') {
            timelineProvider.postMessage({ type: e.event, data: e.body });
        }
        // Refresh call history once when entering replay mode
        if (e.event === 'pyttd/timelineData' && !callHistoryRefreshed) {
            callHistoryRefreshed = true;
            callHistoryProvider.refresh();
        }
    }),
);
```

**Important:** The `callHistoryRefreshed` flag and its reset in `onDidTerminateDebugSession` are consolidated into a single scope. The previous version of the plan had two separate `onDidTerminateDebugSession` listeners — one for provider refresh and one for flag reset — which is redundant. A single handler handles both.

### JSON-RPC messages added

Request/response pairs (`run_id` is not in request params — server uses `self.session.run_id`, same pattern as all other replay-mode handlers):

```json
{"jsonrpc": "2.0", "id": 22, "method": "get_traced_files", "params": {}}
→ {"jsonrpc": "2.0", "id": 22, "result": {"files": ["/path/to/script.py", "/path/to/module.py"]}}

{"jsonrpc": "2.0", "id": 23, "method": "get_execution_stats", "params": {"filename": "/path/to/script.py"}}
→ {"jsonrpc": "2.0", "id": 23, "result": {"stats": [
    {"functionName": "main", "callCount": 1, "exceptionCount": 0, "firstCallSeq": 0, "defLine": 5},
    {"functionName": "process", "callCount": 47, "exceptionCount": 3, "firstCallSeq": 10, "defLine": 15}
  ]}}

{"jsonrpc": "2.0", "id": 24, "method": "get_call_children", "params": {}}
→ {"jsonrpc": "2.0", "id": 24, "result": {"children": [
    {"callSeq": 0, "returnSeq": 25000, "functionName": "<module>", "filename": "/path/to/script.py",
     "line": 1, "depth": 0, "hasException": false, "isComplete": true}
  ]}}

{"jsonrpc": "2.0", "id": 25, "method": "get_call_children", "params": {"parentCallSeq": 0, "parentReturnSeq": 25000}}
→ {"jsonrpc": "2.0", "id": 25, "result": {"children": [
    {"callSeq": 10, "returnSeq": 500, "functionName": "process", "filename": "/path/to/script.py",
     "line": 15, "depth": 1, "hasException": false, "isComplete": true},
    {"callSeq": 501, "returnSeq": null, "functionName": "cleanup", "filename": "/path/to/script.py",
     "line": 30, "depth": 1, "hasException": false, "isComplete": false}
  ]}}
```

### Create

- `vscode-pyttd/src/providers/codeLensProvider.ts` — CodeLens provider with traced-files cache, `get_execution_stats` queries, `defLine`-based positioning
- `vscode-pyttd/src/providers/inlineValuesProvider.ts` — InlineValues provider using existing `get_variables` + visible-line text scanning
- `vscode-pyttd/src/providers/callHistoryProvider.ts` — TreeDataProvider with lazy `get_call_children` loading, exception icons, incomplete markers
- `tests/test_phase6.py` — Backend tests for `get_traced_files`, `get_execution_stats`, `get_call_children` with known frame data: traced file filtering, call counts, exception counts, root calls, nested calls, recursive calls, incomplete recordings

### Update

- **`package.json`** — Add Call History tree view to existing `contributes.views.debug` array (alongside timeline). No `contributes.languages` entry (CodeLens is registered programmatically)
- **`extension.ts`** — Register all three providers, two commands (`pyttd.gotoFirstExecution`, `pyttd.gotoCallFrame`), and lifecycle listeners for refresh
- **`pyttdDebugSession.ts`** — Expand `customRequest` handler: add `goto_frame` as a navigation handler (state update + stopped event), and forward `get_execution_stats`, `get_traced_files`, `get_call_children`, and `get_variables` as query pass-throughs
- **`session.py`** — Add `get_traced_files()`, `get_execution_stats()`, `get_call_children()` methods
- **`server.py`** — Add dispatch handlers for all three new RPCs (after `get_timeline_summary`, before `disconnect`)

### Verify

1. CodeLens annotations appear above functions in traced files with correct call/exception counts
2. CodeLens does NOT appear in files not in the trace
3. Click CodeLens → navigates to first execution of that function (lands on first `line` event)
4. Inline values visible next to variable names during debug, update on step
5. Inline values disappear when debug session ends
6. Call History tree appears in Debug sidebar during pyttd sessions
7. Call History tree is expandable, shows correct nesting for nested function calls
8. Click Call History node → navigates to that frame's first line event
9. Exception calls shown with error icon in Call History
10. Incomplete calls (interrupted recording) shown with "(incomplete)" indicator, not expandable
11. Recursive function calls properly nested in Call History tree
12. Click CodeLens/Call History navigates correctly (editor opens file, stops at expected line, timeline cursor updates)
13. Multiple lambdas in same file: CodeLens merges them (known limitation, verify no crash)
14. `.venv/bin/pytest tests/test_phase6.py` passes

---

## Phase 7: Polish, Performance, Packaging

**Goal:** Production quality. VSIX for marketplace, wheel for PyPI.

### Implementation order

Phase 7 has many independent work streams. Recommended sequencing:

1. **CLI improvements + error handling** (quick wins, no C changes)
2. **Benchmarking** (establishes baselines before optimization)
3. **Multi-thread recording** (largest C change, highest risk — optional for v0.2.0)
4. **pyproject.toml / packaging metadata** (needed before wheels)
5. **CI setup** (GitHub Actions — needed before publishing)
6. **PyPI wheel** (depends on CI)
7. **VSIX packaging** (depends on CI for Node.js)
8. **VSCode extension tests** (can parallelize with 5-7)
9. **Documentation** (last — reflects final state)

Streams 1-2 and 8 can be done in any order. Stream 3 is optional for an initial release — a v0.2.0 can ship with single-thread recording and add multi-thread in v0.3.0.

### Performance targets

| Metric | Target | Measured (Phase 6) | Status |
|---|---|---|---|
| Recording overhead (I/O-bound) | < 2x | — | Benchmark needed |
| Recording overhead (compute-bound) | < 5-10x | — | Benchmark needed |
| Ring buffer flush | < 5ms per 1000 frames | — | Benchmark needed |
| Step back (warm) | < 10ms | ~1.1ms | Verified — well under target |
| Step forward (warm) | < 10ms | ~0.5ms | Verified — well under target |
| Jump to frame (cold) | < 300ms | ~5.8ms | Verified — well under target |
| Timeline scrub | < 16ms per update | < 1ms | Verified — 60fps target met |
| DB size per frame | < 500 bytes | — | Benchmark needed |
| Checkpoint memory | < 50MB each (CoW) | — | Only dirty pages count after fork; measure with `/proc/smaps` on Linux |
| Peak recording RSS | — | — | Benchmark needed — measure Python process RSS during recording to establish baseline |

**Benchmarking tasks:** Create a repeatable benchmark harness that measures each of the unmeasured targets above. Use `samplecode/func_test_stress.py` as the baseline workload (1877 frames, recursive calls, exceptions, I/O hooks). Include a compute-bound microbenchmark (tight loop with many function calls) and an I/O-bound benchmark (script with sleeps/file reads) for the two overhead categories. Report results in a `BENCHMARKS.md` file.

### Work

**Error handling:**
- Backend crash detection: `BackendConnection.spawn()` already handles exit code via `process.on('exit')` and propagates stderr during startup. **Remaining work:** after the spawn promise resolves (port received), register a _second_ `process.on('exit')` handler that fires during the active debug session to emit an `OutputEvent` with `category: 'important'` showing the exit code and captured stderr. Currently a mid-session backend crash silently drops the connection
- RPC timeout: already implemented at 5s default in `backendConnection.ts` (`JsonRpcConnection` constructor) with configurable `rpcTimeout` passed from launch config (`pyttdDebugSession.ts`). **Done — no further work needed**
- User scripts with syntax errors: already handled — `runpy` raises `SyntaxError`, caught by `runner.run_script()`, propagated to server. **Verify:** error event includes full traceback (test with malformed script in functional tests)
- User scripts that call `os.fork()`: document as unsupported. Add `PYTTD_RECORDING=1` environment variable — set it in `start_recording()` (C level, via `setenv()`) and clear it in `stop_recording()`. User scripts can check `os.environ.get('PYTTD_RECORDING')` to detect recording mode
- Orphan process cleanup: `BackendConnection.close()` already calls `process.kill()`. **Remaining work:** register a cleanup handler in the extension's `activate()` function (e.g., via `vscode.Disposable`) to kill any surviving backend processes on extension host shutdown (covers unexpected crashes where `disconnectRequest` never fires). Track active backend PIDs in a module-level set
- DB write errors: if the script's directory is read-only, fall back to `tempfile.mkdtemp()` and warn via `OutputEvent`. Currently fails with Peewee `OperationalError`. Implementation: wrap `storage.connect_to_db(path)` call in `recorder.py` with try/except, retry with temp dir, and return the actual DB path so the server can report it to the adapter
- **Protocol robustness:** `protocol.py` already has: (1) non-ASCII header fix (advances buffer on `UnicodeDecodeError`, sets `_closed`), (2) 10MB `Content-Length` limit (already stricter than needed). **Remaining work:** add a header accumulation limit — if `_buffer` grows beyond 1MB without a complete `\r\n\r\n` header terminator, discard and set `_closed`. This prevents memory exhaustion from a slow trickle of bytes without header terminators. Add fuzz tests for these edge cases

**Multi-thread recording:**
- The PEP 523 frame eval hook is per-interpreter and automatically covers all threads
- Currently `recorder.c` filters non-main-thread frames via `g_main_thread_id` checks in the eval hook and fast-forward hook (early return for non-main threads). Phase 7 removes this filter
- **C global changes required:**
  - `g_sequence_counter`: change from plain `uint64_t` to `_Atomic uint64_t`, use `atomic_fetch_add_explicit(..., memory_order_relaxed)` for thread-safe increment. Currently safe only because non-main threads are filtered out
  - `g_call_depth`: change from plain `int` to `_Thread_local int` (per-thread call depth tracking). Each thread's depth starts at -1 (set on first frame entry for that thread)
  - `g_inside_repr`: change from plain `int` to `_Thread_local int` (per-thread reentrancy guard)
  - `g_stop_requested`: already `_Atomic int` — no change needed
  - `g_recording`: already `_Atomic int` — no change needed
- **FrameEvent struct:** add `unsigned long thread_id` field to `FrameEvent` in `frame_event.h`. Stamp via `PyThread_get_thread_ident()` in the eval hook
- **ExecutionFrames model:** add `thread_id = IntegerField(default=0)` to `ExecutionFrames`. Add index on `(run_id, thread_id, sequence_no)`. Update `initialize_schema()` call. **Migration note:** existing `.pyttd.db` files won't have this column — the recorder already deletes and recreates the DB on each recording, so no migration needed. Warm replay of old recordings will fail until re-recorded
- **Flush callback:** update the flush thread's dict-building code in `flush_batch()` to include `'thread_id'` key (matching the new model field). Update `recorder.py._on_flush()` to pass it through in batch inserts
- **Ring buffer upgrade from SPSC to MPSC:** recommended approach is per-thread SPSC buffers (`_Thread_local RingBuffer*` pointer) with flush thread draining all buffers round-robin. Each per-thread buffer needs its own string pool pair to avoid cross-thread pool corruption. Implementation:
  - Add `_Thread_local RingBuffer *g_thread_rb` in `ringbuf.c`
  - On first frame event per thread, allocate a new `RingBuffer` (smaller capacity, e.g. 8192 per thread) and register it in a global linked list protected by a mutex (flush thread iterates the list)
  - Flush thread round-robins: for each registered buffer, drain available events, swap pool, flush batch
  - Alternative: single MPSC buffer with CAS-based `write_pos` advance, but this introduces contention on the hot path. Per-thread buffers are preferred
- **Thread cleanup:** when a thread exits, its `_Thread_local` buffer must be drained and freed. Use `pthread_key_create()` with a destructor callback (portable POSIX API). Do NOT use `threading._register_atexit` — it's a private Python API subject to change. The destructor must: drain remaining events, free the buffer, and remove it from the global list
- `threadsRequest` returns actual thread list from recorded data (`SELECT DISTINCT thread_id FROM execution_frames WHERE run_id = ?`)
- **Navigation thread semantics:**
  - `step_into` / `step_back`: operate on the global sequence (interleaved thread events) — user sees the next/previous event across all threads. This is correct for understanding program behavior
  - `step_over` / `step_out`: must filter to the **current thread** — stepping over a call in thread A should not land on a line event in thread B. Filter by `thread_id == current_thread_id` in addition to existing `call_depth` checks
  - `continue_forward` / `reverse_continue`: breakpoints match across all threads (any thread hitting a breakpoint stops). The `thread_id` of the stopped frame determines which thread is "focused" in the DAP `StoppedEvent`
  - Add `--thread N` filter option for per-thread stepping in CLI replay mode
- Checkpoint correctness: all non-main threads must be quiesced before `fork()`. **Recommended: option (b)** — require single-threaded state at fork time. Document that checkpoint-based cold navigation is only reliable for single-threaded programs. In practice: if non-main threads are active at checkpoint time, skip the checkpoint (log a warning) rather than risk undefined behavior. Defer full multi-thread checkpoint support
- `stop_recording` cleanup: must iterate per-thread buffers (via the global linked list) and drain all remaining events. The `pthread_key` destructor handles thread-exit cleanup, but `stop_recording` must handle the case where threads are still alive at stop time

**Generator/async frames:** Recorded as regular frames with appropriate event types. Generator function names are captured via `PyCode_GetCode()` → `co_qualname` (e.g., `outer.<locals>.inner`), which covers generator functions. No special coroutine visualization in v1 — async `await` appears as regular call/return pairs. Document that `asyncio` event loop internals are filtered by `should_ignore()` (stdlib), but user-defined coroutines are recorded normally.

**CLI improvements:**
- Add `--version` flag: `parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')` — import `__version__` from `pyttd`
- Add `--db` flag to `serve` subcommand: allows replaying an existing `.pyttd.db` without re-recording. Server skips recording phase and goes directly to replay mode. Implementation: `serve_parser.add_argument('--db', type=str)`. In server startup, if `--db` is provided, skip `launch`/`configuration_done` recording flow and immediately call `enter_replay()`. Mutually exclusive with `--script`
- Add `--verbose` / `-v` flag: set up `logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)` in `main()`. Replace scattered `print()` calls with `logging.debug()` / `logging.info()`
- Validate script path exists before starting server: in `_cmd_serve`, check `os.path.isfile(args.script)` before spawning. Return clear error message with the attempted path

**VSCode extension testing:**
- Set up Mocha test framework (`npm test`) with `@vscode/test-electron`
- Test DAP message handling with mock backend (fake TCP server returning canned JSON-RPC responses)
- Test `BackendConnection` spawn/connect/close lifecycle (mock subprocess)
- Test timeline scrubber rendering with mock data (verify canvas draw calls)
- Test CodeLens provider with mock debug session
- Test Call History tree provider expand/collapse
- Minimum coverage: all DAP request handlers, notification dispatch, custom event routing

**VSIX packaging:**
- `vsce package` (or `@vscode/vsce`)
- Marketplace metadata: `publisher` already set to `"pyttd"`, `categories` already set to `["Debuggers"]`. **Add:** `icon` (128x128 PNG), `repository` URL, `galleryBanner` color, README with screenshots
- Extension should work without Python extension installed (standalone debug adapter — `findPythonPath` already has venv/PATH fallback)
- `engines.vscode` already set to `^1.85.0` — verify this is still a reasonable minimum (it provides the `WebviewView` API needed for the timeline sidebar)
- Add `extensionPack` (not `extensionDependencies`) for `ms-python.python` as a soft recommendation (for Python path auto-discovery, not required for functionality)
- Pre-publish validation: verify `activationEvents` (`onDebugResolve:pyttd`), `contributes.debuggers`, `contributes.views` are correct. Run `vsce ls` to check included files, ensure no `node_modules` bloat or `.pyttd.db` test artifacts are bundled
- Include `CHANGELOG.md` in VSIX (add to `.vscodeignore` allowlist)

**PyPI wheel:**
- Build matrix for Python 3.12/3.13/3.14 (and 3.15 when stable — `PyUnstable_InterpreterState_GetEvalFrameFunc` rename already handled via `#ifdef PY_VERSION_HEX >= 0x030F0000` in `recorder.c`)
- Platform wheels for Linux x86_64/aarch64 and macOS x86_64/arm64
- Source distribution (sdist) for Windows and other platforms (compiles from source). **Ensure `MANIFEST.in` includes `ext/*.h` header files** — without this, sdist installs fail because `setup.py` specifies `include_dirs=["ext"]` but only `.c` files are auto-included by setuptools
- `cibuildwheel` for CI wheel builds. Configuration in `pyproject.toml` under `[tool.cibuildwheel]`: set `CIBW_BUILD = "cp312-* cp313-* cp314-*"`, `CIBW_TEST_COMMAND = "pytest {project}/tests/ -v"`, `CIBW_TEST_REQUIRES = "pytest"`. Skip `musllinux` initially (musl + fork + checkpointing may have edge cases)
- Complete `pyproject.toml` metadata (currently missing):
  - `project.urls`: `Homepage`, `Repository`, `Bug Tracker`, `Documentation`
  - `project.classifiers`: `Development Status :: 3 - Alpha`, `License :: OSI Approved :: MIT License`, `Programming Language :: Python :: 3.12`, `Programming Language :: Python :: 3.13`, `Topic :: Software Development :: Debuggers`, `Operating System :: POSIX :: Linux`, `Operating System :: MacOS`
  - `project.readme = "README.md"`
- Add `py.typed` marker file in `pyttd/` for PEP 561 type checking support (empty file)
- Version: update from `0.1.0` to `0.2.0` for first public release (reserve `1.0.0` for multi-thread + polished state). Keep `__version__` in `pyttd/__init__.py` and `version` in `pyproject.toml` in sync
- Entry point: `[project.scripts] pyttd = "pyttd.cli:main"` already present — verify with `pip install` from sdist on a clean venv
- Smoke test: on a clean venv, `pip install` the built wheel, then run `pyttd record samplecode/stress_test.py` and `pyttd query --last-run --frames` to verify end-to-end. Check that `import pyttd_native` works (catches missing shared library linking issues)

**CI (GitHub Actions):**
- Test matrix: Python 3.12/3.13/3.14, Linux x86_64 + macOS arm64
- Windows in test matrix for record-only mode (no checkpoint tests — gate with `@pytest.mark.skipif(not hasattr(pyttd_native, 'restore_checkpoint'), reason="no fork support")` or equivalent `PYTTD_HAS_FORK` check)
- ASAN build on Linux: `CFLAGS="-fsanitize=address" LDFLAGS="-fsanitize=address" pip install -e .` then `pytest tests/ -v`. Not on macOS (SIP blocks `DYLD_INSERT_LIBRARIES` required for ASAN with dlopen'd extensions). Consider also running UBSan (`-fsanitize=undefined`) which works on both platforms
- Run full test suite: 129+ unit tests via `pytest tests/ -v`
- Run functional tests: `samplecode/func_test_server.py`, `samplecode/func_test_stress.py`, `samplecode/func_test_deep.py`, `samplecode/func_test_leaks.py`
- `npm test` for extension (requires Node.js 18+ in CI)
- `vsce package` validation (no publish, just verify it builds)
- Artifact upload: wheels + VSIX per build
- Branch protection: require CI pass before merge
- **Dependency caching:** cache `.venv/` (keyed on `pyproject.toml` hash) and `node_modules/` (keyed on `package-lock.json` hash) for faster CI runs

**Release process:**
- Git tag `v0.2.0` on release commit
- Update `__version__` in `pyttd/__init__.py` and `version` in `pyproject.toml` (keep in sync)
- Update `version` in `vscode-pyttd/package.json` to match
- Update DESIGN.md to reflect Phase 7 completion
- `CHANGELOG.md` entry for the release

**Documentation:**
- `README.md` — Already exists (100 lines). Expand with PyPI install instructions (`pip install pyttd`), VSCode marketplace link, and a quick-start GIF/screenshot
- `ARCHITECTURE.md` — Three-layer architecture overview, key design decisions (extracted from DESIGN.md's architectural sections)
- `docs/known-limitations.md` — Expanded from the list below, with workarounds where applicable
- `docs/cli-reference.md` — All subcommands (`record`, `query`, `replay`, `serve`), flags, examples
- `docs/vscode-usage.md` — Launch config setup, timeline scrubber usage, CodeLens interaction, Call History panel
- API reference: Document JSON-RPC methods exposed by `serve` for third-party tool integration. Extract method list from `server.py` dispatch table: `backend_init`, `launch`, `configuration_done`, `set_breakpoints`, `set_exception_breakpoints`, `interrupt`, `get_threads`, `get_stack_trace`, `get_scopes`, `get_variables`, `evaluate`, `continue`, `next`, `step_in`, `step_out`, `step_back`, `reverse_continue`, `goto_frame`, `goto_targets`, `restart_frame`, `get_timeline_summary`, `get_traced_files`, `get_execution_stats`, `get_call_children`, `disconnect`

### Known limitations to document

1. Variables in replay mode are `repr()` snapshots (max 256 chars per value), not live expandable objects — cannot drill into nested structures
2. Debug Console REPL evaluation is disabled in replay mode (returns guidance message). Hover and Watch evaluate against recorded `repr()` snapshots via string lookup, not live `eval()` — cannot evaluate expressions like `len(x)` or `x + y`
3. C extension internal state is opaque — third-party C extension object `repr()` may not be informative
4. Windows: record + warm browse only, no checkpoint-based cold navigation (no `fork()`)
5. Multi-threaded programs: only the main thread is recorded in v0.2.0 (multi-thread recording is a Phase 7 work item). Even with multi-thread recording, checkpoint correctness requires single-threaded state at fork time — cold navigation skips checkpoints when non-main threads are active
6. Async/await: recorded as regular frames, no coroutine-specific visualization or `asyncio` task grouping
7. Relative imports: scripts using `from . import` must be launched via `module` mode (`--module`), not `program` mode
8. Per-variable repr truncation: individual local variable repr strings are truncated at 256 characters (not total locals size). Complex objects show `repr(obj)[:256]...`. Per-frame locals buffer is 64KB — frames with extremely many locals may have some variables omitted
9. User scripts that call `os.fork()` directly may conflict with checkpoint manager (two fork-based systems fighting over child processes). Check `os.environ.get('PYTTD_RECORDING')` to detect recording mode
10. Output capture: uses `os.dup2` to redirect stdout/stderr — scripts that close/redirect their own file descriptors may interfere. Scripts that write to `/dev/tty` bypass capture
11. I/O hooks: only 6 functions hooked (`time.time`, `time.monotonic`, `time.perf_counter`, `random.random`, `random.randint`, `os.urandom`). Function references captured before recording starts (e.g., `t = time.time` at module level) bypass the hooks. File I/O (`open`, `os.read`) is NOT hooked — cold replay of file-reading code produces non-deterministic results
12. `PyEval_SetTrace` overflow: CPython's internal events counter can overflow after ~65K trace installations. Fixed in pyttd via redundant-install check, but may still affect very long recordings if running on an older CPython build where `_Py_TracingPossible` handling differs
13. macOS fork safety: fork-based checkpointing works if the process is single-threaded at fork time. macOS `libdispatch` or AppKit imports may create background threads that make fork unsafe — avoid importing `objc`, `AppKit`, or `Foundation` in recorded scripts
14. Source code dependency: `linecache.getline()` reads source at replay time, not record time. If source files change between recording and replay, line numbers will not match displayed source. **Workaround:** re-record after source changes
15. Recording overhead: varies significantly by workload. I/O-bound programs see < 2x slowdown; compute-bound programs with many short function calls may see 5-10x. The per-frame eval hook cost is fixed regardless of what the function does, so fast functions amplify the relative overhead

---

## Sequencing & Parallelism

```
Phase 0 (Foundation)
  |-- Phase 1 (C Recorder)     --\
  |     \-- Phase 2 (Checkpoints) >-- Phase 4 (Time-Travel + I/O Hooks)
  \-- Phase 3 (Server + DAP)   --/     |-- Phase 5 (Timeline)  --\
                                       \-- Phase 6 (CodeLens+) --/-- Phase 7 (Polish)
```

- **Phase 0** is a prerequisite for all other phases.
- **Phases 1 & 3** can be partially parallelized: the Debug Adapter can be built and tested against a mock Python backend (hardcoded JSON responses) while the C extension is developed. Phase 3 depends on Phase 1 for the actual recording integration, but the DAP handler scaffolding and JSON-RPC protocol can be built independently.
- **Phase 2** depends on Phase 1 (needs the recorder and ring buffer for checkpoint integration).
- **Phase 4** depends on both Phase 2 (checkpoint infrastructure for cold navigation) and Phase 3 (DAP handlers to expose time-travel UI). I/O hooks in Phase 4 retroactively make Phase 2's cold navigation deterministic for non-deterministic code.
- **Phases 5 & 6** can be fully parallelized — they are independent UI features that both depend on the Phase 4 navigation infrastructure.
- **Phase 7** depends on all previous phases.

### Implementation order for sequential (non-parallel) development

If implementing sequentially with a single developer, the recommended order is:

**Phase 0 -> Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6 -> Phase 7**

This builds the system bottom-up: C foundation first, then Python layer, then TypeScript layer, then polish.

## Estimated Code (updated after Phase 6)

| Component | Language | Estimated | Actual |
|---|---|---|---|
| C extension (ext/) | C | ~1,800 | ~3,200 |
| Python backend (pyttd/) | Python | ~1,500 | ~1,900 |
| Debug Adapter + Extension | TypeScript + HTML/JS/CSS | ~2,200 | ~1,700 |
| Tests | Python | ~900 | ~2,500 |
| Config/Build/Docs | Various | ~500 | ~500 |
| **Total** | | **~6,900** | **~9,800** |

## Critical Files

See DESIGN.md `## Critical Files` section for the authoritative, up-to-date list of all implemented files with detailed descriptions. The DESIGN.md file is maintained as implementation progresses and reflects the actual state of each file.
