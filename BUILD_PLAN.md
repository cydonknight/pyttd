# pyttd Build Plan: C Extension + VSCode Time-Travel Debugger

## Context

pyttd is a partially-started Python time-travel debugger. The current code (~300 lines) uses `sys.settrace` from Python and prints frame data. The goal is to evolve this into a true replay debugger with a C extension backend and a full VSCode IDE integration using the Debug Adapter Protocol (DAP). C extension only (no pure-Python fallback). Full visual debugger experience.

### Prerequisites

- **Python >= 3.12** installed (required for `PyUnstable_InterpreterFrame_*` C API accessors)
- **C compiler** (gcc or clang on Unix, MSVC on Windows)
- **Node.js >= 18** and npm (for VSCode extension development)
- **VSCode** (for testing the extension; CLI works standalone)
- The repository is **not yet a git repository** — Phase 0 initializes it

### Virtual Environment

All Python commands use a project-local virtual environment at `.venv/`. Phase 0 creates it. The existing `venv/` directory (created earlier with stale dependencies) is gitignored but not deleted.

```bash
# Created in Phase 0:
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"    # Compiles C extension + installs peewee, pytest, rich

# All subsequent commands use the .venv prefix:
.venv/bin/python -m pyttd record script.py
.venv/bin/pytest tests/
.venv/bin/pip install -e .           # Recompile after C changes
```

**Convention:** Never use bare `pip` or `python` — always `.venv/bin/pip` and `.venv/bin/python` to avoid polluting the system Python environment.

### Existing Code Inventory

The current repository contains:
- `pyttd/main.py` — `@ttdbg` decorator using `sys.settrace`, `DBG` stub class
- `pyttd/models/` — Peewee models (`base.py`, `frames.py`, `runs.py`, `storage.py`, `constants.py`) with known bugs
- `pyttd/models/frames.old.py` — superseded by Peewee version, to delete
- `pyttd/models/__init__.py` — exports `_BaseModel`, `ExecutionFrames`, `Runs`
- `pyttd/tracing/` — `trace.py` (SimpleTrace class), `trace_func.py` (function_decorator_trace), `enums.py`, `constants.py`
- `pyttd/tracing/__init__.py` — empty
- `pyttd/performance/` — `clock.py`, `performance.py` (**missing `__init__.py`** — must be created)
- `pyttd/__init__.py` — empty (needs `__version__`)
- `pyttd/runs.py` — empty file, to delete
- `samplecode/` — `basic_sample_function.py` (has test functions BUT imports `from pyttd.main import ttdbg` which breaks after Phase 0's `storage.py` rewrite — must clean up in Phase 0), `edge_case_samples.py` (empty), `threads_sample.py` (empty)
- `samplecode/__init__.py` — incorrect (not a package), to delete
- `requirements.txt` — lists `rich`, `sql30` (both replaced), to delete
- `__init__.py` at repo root — incorrect, to delete
- `TTdebug.db` — leftover test artifact, to delete
- `TODO` — old task list superseded by this plan, to delete
- `.vscode/settings.json` — docstring format setting (keep)

### Known Bugs in Existing Code

- `pyttd/models/frames.py`: `frame_id = UUIDField(default=uuid4())` — evaluated once at import AND incompatible with `insert_many`. Replace with `frame_id = AutoField()` (auto-increment integer, see Phase 0 Modify section)
- `pyttd/models/runs.py`: `default=uuid4()` evaluates once at import — should be `default=uuid4` (callable)
- Both files: `default=int(datetime.now().timestamp())` freezes at import — should be `default=lambda: datetime.now().timestamp()`
- `pyttd/models/constants.py`: `DEBUG_DB_NAME = "TTdebug.db"` hardcoded — replace with `DB_NAME_SUFFIX = ".pyttd.db"`
- `pyttd/models/constants.py`: `synchronous = 0` risks corruption — change to `synchronous = 1` (WAL + NORMAL is safe)
- `pyttd/models/storage.py`: hardcoded DB path from `DEBUG_DB_NAME` — make dynamic via `db.init()`
- `pyttd/models/base.py`: `database = db_conn.db` binds at import — must use Peewee's deferred database pattern

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
11. `disconnect` DAP request -> adapter sends `shutdown` RPC -> server kills all checkpoint children, closes socket, exits

**Replay mode vs. live debugging:** This is a **post-mortem / record-replay debugger**, not a live debugger. The script runs to completion (or interruption) before interactive debugging begins. Breakpoints set before recording mark positions for forward-continue and reverse-continue to stop at during replay. Live breakpoints that pause execution mid-recording are a future enhancement.

### Server Concurrency Model

The Python backend uses **two threads** during recording:

1. **RPC thread** (main thread): Runs a `select()`-based event loop on the TCP socket. Handles incoming JSON-RPC requests (`interrupt`, `set_breakpoints`, etc.) and sends outgoing notifications (`progress`, `output`). During recording, most RPC requests are deferred until replay mode — only `interrupt` and `set_breakpoints` are processed immediately.

2. **Recording thread**: Spawned when `configuration_done` RPC is received. Calls `runner.run_script()` which executes the user script with the C frame eval hook active. When the script completes (or is interrupted), this thread calls `recorder.stop()`, then posts a `recording_complete` message to the main thread's event queue via the wakeup pipe.

**Interrupt mechanism:** The `interrupt` RPC handler calls `pyttd_native.request_stop()` which sets an atomic `g_stop_requested` flag in `recorder.c`. The frame eval hook checks this flag at the top of each invocation. When set, the hook raises `KeyboardInterrupt` via `PyErr_SetNone(PyExc_KeyboardInterrupt)` and returns `NULL`, which unwinds the script back to the recording thread's `runner.run_script()` call. The recording thread catches the exception, calls `recorder.stop()`, and signals recording complete. This avoids the Python `threading.Event` approach which would require the C eval hook to periodically re-acquire the GIL to check a Python object.

The C flush thread (a third thread, managed by the C extension) wakes periodically to batch-insert frames from the ring buffer into SQLite. It acquires the GIL only during the Python-side `batch_insert` call.

After recording completes, the recording thread exits and the RPC thread handles all replay navigation in a single-threaded loop.

**Signal handling:** The server registers `signal.signal(signal.SIGINT, handler)` and `signal.signal(signal.SIGTERM, handler)` in the main thread. The handler calls `pyttd_native.request_stop()` (if recording is active) to interrupt the user script via the C atomic flag, then sets a `shutdown_event` that triggers graceful cleanup: wait for recording thread to finish, kill checkpoint children, close socket, exit. The recording thread catches the resulting `KeyboardInterrupt`, calls `recorder.stop()`, and signals completion via the wakeup pipe — same path as the `interrupt` RPC handler.

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

## What Stays vs. Changes

| Current File | Action | Phase |
|---|---|---|
| `models/base.py` | **Evolve** (deferred database pattern) | 0 |
| `models/storage.py` | **Evolve** (dynamic DB path, batch insert, remove rich) | 0 |
| `models/constants.py` | **Evolve** (update DB naming convention, fix synchronous) | 0 |
| `models/frames.py` | **Fix & evolve** (fix defaults, add fields, add indexes) | 0 |
| `models/runs.py` | **Fix** (fix defaults, add fields) | 0 |
| `models/__init__.py` | **Keep** | — |
| `tracing/enums.py` | **Keep** | — |
| `tracing/constants.py` | **Keep** (pass ignore patterns to C extension) | — |
| `tracing/__init__.py` | **Keep** | — |
| `tracing/trace_func.py` | **Delete** (replaced by C extension in Phase 1) | 1 |
| `tracing/trace.py` | **Delete** (replaced by C extension in Phase 1) | 1 |
| `main.py` | **Evolve** (calls into C extension instead of settrace) | 1 |
| `performance/clock.py` | **Keep** | — |
| `performance/performance.py` | **Keep** | — |
| `models/frames.old.py` | **Delete** | 0 |
| `pyttd/runs.py` (empty) | **Delete** | 0 |
| `samplecode/__init__.py` | **Delete** (not a package) | 0 |
| `requirements.txt` | **Delete** (replaced by pyproject.toml) | 0 |
| `__init__.py` (repo root) | **Delete** (not a package) | 0 |
| `TTdebug.db` | **Delete** (test artifact) | 0 |
| `TODO` | **Delete** (superseded by BUILD_PLAN.md) | 0 |
| `.vscode/settings.json` | **Keep** (editor settings) | — |

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
    performance/                 # Existing, kept
      __init__.py                # NEW: required for package imports
      clock.py, performance.py
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

## Phase 0: Foundation Cleanup & Scaffolding

**Goal:** Fix bugs, initialize git, set up monorepo, scaffold C extension stubs + VSCode extension skeleton. Pressing F5 in Extension Development Host shows "pyttd" in the debug type dropdown.

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

## Phase 1: C Extension Recorder + Ring Buffer

**Goal:** Replace Python settrace with C-level frame eval hook. Ring buffer captures frames, background flush thread writes to SQLite via Peewee.

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

### ExecutionFrames schema update

Add `checkpoint_id` field to `ExecutionFrames`:
```python
# In pyttd/models/frames.py, add to the model:
checkpoint_id = IntegerField(null=True)  # set only on frames where a checkpoint was taken
```

This field is `null` for most frames. It's only populated for the specific frame at which a `fork()` occurred, linking to the `Checkpoint` model. **Population mechanism:** After `Checkpoint.create()` returns the auto-assigned `checkpoint_id`, `recorder.py`'s checkpoint callback issues an UPDATE: `ExecutionFrames.update(checkpoint_id=cp.checkpoint_id).where(ExecutionFrames.run_id == run_id, ExecutionFrames.sequence_no == seq).execute()`. This update runs in the main recording thread (the checkpoint callback is called from the eval hook, which has the GIL). Note: the frame row for this sequence_no may not be flushed to DB yet (still in ring buffer). In that case, the UPDATE matches zero rows — this is acceptable because the `Checkpoint` table itself records the `sequence_no`, which is the primary lookup key for `find_nearest_checkpoint`. The `checkpoint_id` field in `ExecutionFrames` is a convenience denormalization, not a critical dependency.

**Schema migration note:** Since Phase 0 already created the `ExecutionFrames` table without `checkpoint_id`, and Phase 2 adds this column, the implementer must either: (a) drop and recreate the table (acceptable since the DB is deleted before each recording via `delete_db_files()`), or (b) use `playhouse.migrate` to add the column. Option (a) is preferred — the `delete_db_files()` call in `_cmd_record` / `server.py` already ensures a fresh DB on each recording, and `create_tables(safe=True)` creates the table with the new column from scratch. The only risk is leftover DB files from Phase 1 testing that don't have the new column — `delete_db_files()` handles this.

### Key C components

**`checkpoint.c`:**
1. **Pre-fork:** Set atomic `pause_requested` flag on flush thread. Wait for `pause_ack` condition variable (flush thread checks flag at top of each iteration, signals ack, blocks on `resume_cv`). **Critical:** The flush thread must acknowledge the pause AFTER releasing the GIL and all Python runtime mutexes (i.e., after `PyGILState_Release()` in its flush iteration). If the flush thread holds the GIL or any interpreter mutex when `fork()` is called, the child process inherits a locked mutex and deadlocks in `PyOS_AfterFork_Child()`. Timeout after 1 second — if flush thread is stuck, log warning and skip this checkpoint.
2. **Fork:** Call `PyEval_SaveThread()` to release GIL, then `fork()`.
3. **Child:** Call `PyOS_AfterFork_Child()` to reinitialize GIL and thread state (child now holds the GIL). Close unneeded pipe ends (write end of cmd_pipe, read end of result_pipe). Release GIL via `saved_tstate = PyEval_SaveThread()`, storing the `PyThreadState*` in a C static variable accessible to the checkpoint wake-up code. Then block on `read(cmd_pipe)`. When a command arrives, re-acquire GIL via `PyEval_RestoreThread(saved_tstate)`, process command (fast-forward, serialize state, write to result_pipe), release GIL again via `saved_tstate = PyEval_SaveThread()`, and block on the next command. The child does NOT have a flush thread — threads don't survive `fork()`. It doesn't need one since it's a frozen snapshot, not actively recording.
4. **Parent:** Call `PyEval_RestoreThread()` to re-acquire GIL. Record `(child_pid, cmd_pipe_fd, result_pipe_fd, sequence_no)` in checkpoint store. Signal `resume_cv` to wake the flush thread.
5. Max 32 live checkpoints with exponential thinning eviction (see Architecture section).

**Checkpoint store:** Two complementary data structures:
- **C-level `checkpoint_store.c`**: Array of `CheckpointEntry` structs holding runtime state: `{child_pid, cmd_pipe_fd, result_pipe_fd, sequence_no, is_alive}`. This is the source of truth for live checkpoint management (sending commands to children, killing them). Lives only in process memory.
- **Python-level `checkpoints.py`**: Peewee model persisted to the DB. Records `{checkpoint_id, run_id, sequence_no}` for replay queries like "find nearest checkpoint <= target_seq." The `child_pid` field is for diagnostics only (null after the session ends).

**`replay.c`:** `replay_to_frame(target_seq)` queries the C checkpoint store for the nearest checkpoint ≤ target_seq. Writes `(RESUME, target_seq)` to the child's command pipe. Child wakes from `read(cmd_pipe)`, re-acquires GIL via `PyEval_RestoreThread(saved_tstate)` (`PyOS_AfterFork_Child` was already called once right after fork — subsequent wake-ups use `PyEval_RestoreThread`), enters fast-forward mode. At `target_seq`, serializes full frame state as JSON, writes to result pipe in chunks (handles states > 64KB pipe buffer limit by using a length-prefixed protocol: write 4-byte big-endian length, then payload). Parent reads length, then payload, and returns the JSON to Python.

**Execution flow detail:** The checkpoint is created inside the frame eval hook, specifically **after recording the `call` event (step 5) and before calling the original eval function (step 7)**. The eval hook checks `sequence_no % checkpoint_interval == 0` and calls the checkpoint creation function at that point. After `fork()`:
- The child blocks on `read(cmd_pipe)`.
- When `RESUME` arrives, the child's checkpoint function returns, unwinding back into the frame eval hook, which continues at step 6 (install trace) → step 7 (call original eval).
- The interpreter resumes executing the user's script from the checkpointed frame, with the eval hook now in **fast-forward mode** (a static `int g_fast_forward` flag + `uint64_t g_fast_forward_target` set before returning from the checkpoint function).
- **Critical:** In fast-forward mode, the eval hook MUST still install the trace function via `PyEval_SetTrace` (step 6 still executes). Without the trace function installed, `line`/`return`/`exception` events would not be generated by CPython's eval loop, and only `call` and `exception_unwind` events (from the eval hook) would be counted — causing the fast-forward sequence counter to diverge from the recording's sequence numbers. Both the eval hook and trace function increment the sequence counter for every event (call, line, return, exception, exception_unwind) — **the same events that increment `g_sequence_counter` during normal recording** — but do NOT serialize locals, do NOT write to the ring buffer, and do NOT create checkpoints. The trace function checks `g_fast_forward` and skips serialization/ringbuf writes while still incrementing the counter.
- The same ignore filter logic (step 3) must apply identically in fast-forward mode — ignored frames must be skipped with the same criteria, or the event count will diverge from the original recording.
- At `target_seq`, the hook/trace function serializes full frame state as JSON, writes it to the result pipe, clears the fast-forward flag, and blocks again on `read(cmd_pipe)`. The `read()` call should release the GIL first (`PyEval_SaveThread()`) since the child is single-threaded but proper GIL release is still good practice for signal handling.
- The user's code actually re-executes between the checkpoint frame and the target frame. Non-deterministic functions (`time.time()`, `random.random()`, etc.) may produce different values — this is **expected and acceptable** until Phase 4 adds I/O hooks for deterministic replay. Cold navigation in Phase 2 correctly reconstructs call stack and frame metadata but variable values from non-deterministic calls may differ from the original recording. **Warning:** If non-deterministic values cause the re-executed code to take a different branch (e.g., `if random.random() > 0.5:`), the fast-forward sequence counter will diverge and the target frame may not correspond to the original recording. This is fixed in Phase 4 with I/O hooks.

**macOS safety:** The checkpoint creation function in `checkpoint.c` must check for active threads before forking. Since `threading.active_count()` is a Python API, this is implemented by calling `PyObject_CallNoArgs()` on the `threading.active_count` function from C (cached during `start_recording`). If the count > 1 (the flush thread doesn't count — it's a C thread, not a Python thread, but verify via testing), log a warning via `PyErr_WarnEx(PyExc_RuntimeWarning, ...)` and skip the checkpoint. On Python 3.12+, the fork itself would also emit `DeprecationWarning` — suppress it since we've already warned.

**Windows:** Stubs return `PYTTD_ERR_NO_FORK`. All cold navigation requests fall through to warm-only mode.

### Pipe command protocol

Commands sent from parent to checkpoint child via `cmd_pipe`. Each command is a fixed-size binary message:

```
Command format: [1-byte opcode] [8-byte uint64 payload, big-endian]

Opcodes:
  0x01 = RESUME   payload = target_seq (fast-forward to this sequence number from checkpoint origin)
  0x02 = STEP     payload = delta as uint64 (forward only: advance delta events from current position)
  0xFF = DIE      payload = ignored (child exits immediately)
```

Result sent from child to parent via `result_pipe`. Uses a length-prefixed protocol to handle payloads > 64KB (OS pipe buffer limit):

```
Result format: [4-byte big-endian length N] [N bytes of JSON payload]
```

The parent must read in a loop, handling `EINTR` and partial reads (`read()` may return fewer bytes than requested). For payloads > 64KB, the child's `write()` may block if the pipe buffer is full — this is safe as long as the parent is concurrently reading. The JSON payload contains the same fields as a navigation response: `{"seq": <uint64>, "file": "...", "line": <int>, "function_name": "...", "call_depth": <int>, "locals": {...}}`.

### Checkpoint lifecycle and warm child management

**Checkpoint consumption:** When a checkpoint child receives `RESUME(target_seq)`, it fast-forwards from its checkpoint origin to `target_seq`. After fast-forwarding, the child's process state reflects `target_seq`, NOT its original checkpoint position — the original checkpoint state is irreversibly consumed. The C-level `checkpoint_store` must track the child's current position (updated after each `RESUME` or `STEP`) separately from its original `sequence_no`. When `checkpoint_store_find_nearest(target_seq)` is called, it must only consider checkpoints whose **current position** is ≤ `target_seq` (a child that has fast-forwarded past `target_seq` cannot serve it).

**Warm child:** The most recently resumed child stays alive in a "warm" state. The parent sends `STEP(+N)` to the warm child for forward incremental cold navigation (child advances N more events, re-serializes state, writes to result pipe). `STEP` only supports forward movement — a checkpoint child cannot step backward because its prior process state is gone after fast-forward.

**Backward cold navigation:** Since `step_back` and `reverse_continue` are always warm (read from SQLite), backward cold navigation only occurs via `goto_frame` to a position before the warm child's current position. In this case, the parent must find a different checkpoint whose current position is ≤ the target. The warm child may be kept alive (for potential future forward jumps) or killed via `DIE` if the checkpoint slot is needed for eviction. If no unconsumed checkpoint covers the target, the system falls back to warm-only navigation (SQLite read with `repr()` snapshots).

The child exits when: (a) the user jumps outside this child's forward-reachable window, (b) the session ends (`DIE` command), or (c) the parent needs to evict it for a new checkpoint.

### Checkpoint index model (`pyttd/models/checkpoints.py`)

```python
from peewee import AutoField, BigIntegerField, IntegerField, ForeignKeyField
from pyttd.models.base import _BaseModel
from pyttd.models.runs import Runs

class Checkpoint(_BaseModel):
    checkpoint_id = AutoField()  # auto-increment primary key
    run_id = ForeignKeyField(Runs, backref='checkpoints', field='run_id')
    sequence_no = BigIntegerField()      # frame seq at which this checkpoint was taken
    child_pid = IntegerField(null=True)  # null after child is killed or session ends
    is_alive = IntegerField(default=1)   # 0 = evicted/killed
```

**Checkpoint creation flow:** The C eval hook detects `sequence_no % checkpoint_interval == 0` and calls `pyttd_create_checkpoint()`. In Phase 2, `pyttd_create_checkpoint` is updated to accept a Python callback (`checkpoint_callback`). After a successful `fork()`, the parent calls the callback with `(child_pid, sequence_no)`, and the Python-level `recorder.py` inserts the `Checkpoint` row via Peewee (which auto-assigns `checkpoint_id`). The C-level `checkpoint_store` tracks the same data in its array (keyed by array index, not `checkpoint_id`) for runtime IPC management.

### CLI replay subcommand

Replace the Phase 1 stub in `cli.py`:
```python
def _cmd_replay(args):
    from pyttd.replay import ReplayController
    from pyttd.query import get_last_run, get_frame_at_seq, get_line_code
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

    run = get_last_run(db_path)
    controller = ReplayController()
    result = controller.goto_frame(run.run_id, args.goto_frame)
    print(f"Frame {args.goto_frame}: {result}")
    storage.close_db()
```

**Note:** The CLI `replay` command always uses warm-only navigation (SQLite reads) since checkpoint children don't survive the recording process exit. Cold navigation (checkpoint restore) is only available in the server mode (Phase 3+) where the recording and replay occur in the same process within a single session.

### Schema initialization update

Update `recorder.py`'s `start()` method to include the `Checkpoint` model in `initialize_schema`:
```python
from pyttd.models.checkpoints import Checkpoint
storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])
```

### C signature update

Update `pyttd_start_recording()` to accept two additional keyword arguments: `checkpoint_callback` and `checkpoint_interval`. The `checkpoint_interval` (int, default 1000) is stored as a C static and used by the eval hook to decide when to fork (`sequence_no % checkpoint_interval == 0`). The `checkpoint_callback` is invoked from the eval hook (with GIL held) after a successful `fork()` in the parent process. Signature: `checkpoint_callback(child_pid: int, sequence_no: int)`. The Python-level `recorder.py` uses this callback to insert the `Checkpoint` row and update the `ExecutionFrames` row. Update `recorder.py` accordingly:
```python
def _on_checkpoint(self, child_pid: int, sequence_no: int):
    """Called by C eval hook (with GIL held) after successful fork()."""
    from pyttd.models.checkpoints import Checkpoint
    Checkpoint.create(run_id=self._run.run_id, sequence_no=sequence_no, child_pid=child_pid)
```
Update the `start_recording()` call in `recorder.py.start()` to pass both new parameters:
```python
pyttd_native.start_recording(
    flush_callback=self._on_flush,
    buffer_size=self.config.ring_buffer_size,
    flush_interval_ms=self.config.flush_interval_ms,
    checkpoint_callback=self._on_checkpoint,
    checkpoint_interval=self.config.checkpoint_interval,
)
```

### Create

- `ext/checkpoint.c/h` — full implementation (fork, pipe IPC, pre-fork sync with condition variables)
- `ext/checkpoint_store.c/h` — full implementation (C-level checkpoint array, exponential thinning eviction, `find_nearest()`)
- `ext/replay.c/h` — full implementation (find nearest checkpoint, resume child, fast-forward, length-prefixed pipe protocol)
- `pyttd/models/checkpoints.py` — Peewee model for checkpoint persistence
- `pyttd/replay.py` — Python wrapper around C checkpoint/replay:
  ```python
  import pyttd_native
  from pyttd.models.checkpoints import Checkpoint
  from pyttd.models.frames import ExecutionFrames
  from pyttd.models import storage

  class ReplayController:
      def goto_frame(self, run_id, target_seq) -> dict:
          """Cold navigation: restore checkpoint, fast-forward, return frame state.
          Falls back to warm-only navigation (SQLite read) if no checkpoint available
          or if all checkpoints near the target have been consumed past it."""
          cp = self.get_nearest_checkpoint(run_id, target_seq)
          if cp is None or not cp.is_alive:
              return self._warm_fallback(run_id, target_seq)
          # Cold: C-level checkpoint_store_find_nearest checks current_position
          # (not just original sequence_no) to find a child that can serve this target.
          # If all nearby checkpoints have been consumed past target_seq, raises ReplayError.
          try:
              result_json = pyttd_native.restore_checkpoint(target_seq)
              return result_json  # parsed dict from pipe protocol
          except Exception:
              return self._warm_fallback(run_id, target_seq)

      def _warm_fallback(self, run_id, target_seq) -> dict:
          """Read frame data directly from SQLite (repr snapshots only)."""
          frame = ExecutionFrames.get(
              (ExecutionFrames.run_id == run_id) &
              (ExecutionFrames.sequence_no == target_seq))
          return {"seq": target_seq, "file": frame.filename, "line": frame.line_no,
                  "function_name": frame.function_name, "call_depth": frame.call_depth,
                  "locals": frame.locals_snapshot, "warm_only": True}

      def get_nearest_checkpoint(self, run_id, target_seq) -> Checkpoint | None:
          """Query DB for nearest checkpoint <= target_seq."""
          return (Checkpoint.select()
              .where((Checkpoint.run_id == run_id) &
                     (Checkpoint.sequence_no <= target_seq) &
                     (Checkpoint.is_alive == 1))
              .order_by(Checkpoint.sequence_no.desc())
              .first())

      def kill_all(self):
          """Send DIE to all live checkpoint children."""
          pyttd_native.kill_all_checkpoints()
          Checkpoint.update(is_alive=0, child_pid=None).execute()
  ```
- `tests/test_checkpoint.py` — fork creates child, child responds via pipe, eviction works
- `tests/test_replay.py` — verifies locals at frame N during replay match recording

### Update

- **`pyttd/cli.py`** — Update the `main()` dispatch to call `_cmd_replay(args)` instead of the stub print:
  ```python
  elif args.command == 'replay':
      _cmd_replay(args)
  ```
- **`tests/conftest.py`** — Update `db_setup` fixture to include `Checkpoint` in schema initialization:
  ```python
  from pyttd.models.checkpoints import Checkpoint
  storage.initialize_schema([Runs, ExecutionFrames, Checkpoint])
  ```

### Verify

1. `.venv/bin/python -m pyttd record --checkpoint-interval 500 samplecode/basic_sample_function.py` creates checkpoints (DB in `samplecode/basic_sample_function.pyttd.db`)
2. `.venv/bin/python -m pyttd replay --last-run --goto-frame 750 --db samplecode/basic_sample_function.pyttd.db` restores from checkpoint at 500, fast-forwards to 750, prints correct locals
3. `.venv/bin/python -m pyttd replay --last-run --goto-frame 50 --db samplecode/basic_sample_function.pyttd.db` works even after many checkpoints are evicted (uses exponential thinning)
4. `.venv/bin/pytest tests/test_replay.py` — verifies locals at frame N during replay match locals at frame N during recording (for deterministic code — no `time.time()` etc.)
5. On macOS: verify warning is logged when threads are active, checkpoint is skipped gracefully
6. On Windows: verify cold navigation returns a clear error, warm navigation still works

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
            self.runner.run_module(self.script, self.cwd)
        else:
            self.runner.run_script(self.script, self.cwd)
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
        # Fix buffering: Python switches to full buffering when stdout is a pipe
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)

    def _restore_capture(self):
        """Restore original stdout/stderr. Called during shutdown."""
        if self._saved_stdout is not None:
            os.dup2(self._saved_stdout, 1)
            os.close(self._saved_stdout)
            self._saved_stdout = None
        if self._saved_stderr is not None:
            os.dup2(self._saved_stderr, 2)
            os.close(self._saved_stderr)
            self._saved_stderr = None
        if self._capture_r_stdout is not None:
            os.close(self._capture_r_stdout)
            self._capture_r_stdout = None
        if self._capture_r_stderr is not None:
            os.close(self._capture_r_stderr)
            self._capture_r_stderr = None

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
        sock.close()

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

The `_event_loop()` method uses `self._sel.select(timeout=0.5)` during recording (to send periodic progress notifications) and `self._sel.select()` (blocking) during replay. On each iteration, it processes readable file descriptors: RPC socket data is fed to `self._rpc.feed(data)` then drained via `while msg := self._rpc.try_read_message(): self._dispatch(msg)`. **EOF detection:** after `feed()`, check `self._rpc._closed` — if `True`, the adapter disconnected; trigger graceful shutdown (equivalent to receiving a `disconnect` RPC). Wakeup pipe data triggers message queue processing. Capture pipe data triggers output notifications. The `_dispatch(msg)` method routes JSON-RPC requests by method name to handler methods.

JSON-RPC methods handled:

| Method | Phase | Description |
|---|---|---|
| `backend_init` | 3 | Returns server capabilities (version, supported features). Named `backend_init` (not `initialize`) to avoid confusion with DAP's own `initialize` request. |
| `launch` | 3 | Stores config (script, args, checkpoint interval). Does NOT start recording yet. |
| `configuration_done` | 3 | Starts the recording thread — executes user script with C hook active |
| `set_breakpoints` | 3 | Stores breakpoint list for replay navigation |
| `set_exception_breakpoints` | 3 | Stores exception filter settings. `"raised"` filter: `continue_forward` and `reverse_continue` also stop on `frame_event == 'exception'` events. `"uncaught"` filter: stop on `frame_event == 'exception_unwind' AND call_depth == 0`. See query patterns in `session.py` for details. |
| `interrupt` | 3 | Stops recording early (calls `pyttd_native.request_stop()` which sets atomic flag in C) |
| `get_threads` | 3 | Returns `[{id: 1, name: "Main Thread"}]` |
| `get_stack_trace` | 3 | Calls `session.get_stack_at(seq)` |
| `get_scopes` | 3 | Returns `[{name: "Locals", variablesReference: 1}]` |
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
        self.first_line_seq = None
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
5. `enter_replay()` sets `state="replay"`, `current_frame_seq=first_line_seq`, builds initial stack
6. RPC thread sends `stopped` notification with `{seq: first_line_seq, reason: "recording_complete", totalFrames: N}`
7. If the recording thread caught an exception from the user script, the RPC thread also sends an `output` notification (category `"stderr"`) with the traceback before the `stopped` notification

Methods:
- `get_stack_at(seq)` — reconstructs call stack by scanning backward from `seq` for `call` events that haven't been matched by a `return` or `exception_unwind` (stack algorithm: walk backward through frames, push `return`/`exception_unwind` events, pop on `call` events — remaining `call` events form the active stack). Returns list of stack frames with `{seq, name, file, line, depth}`. **Stack frame `seq` values:** The top-of-stack frame uses `current_frame_seq` (the `line` event the user is stopped at). For deeper frames (parent callers), `seq` is the most recent `line` event in that frame before the child's `call` event — this is the call site line. Query: for each parent frame's `call` event at depth D, find `ExecutionFrames.select().where(run_id == X, frame_event == 'line', call_depth == D, sequence_no < child_call_seq).order_by(sequence_no.desc()).limit(1)`. This `seq` is used as `frameId` in the DAP `StackFrame` and as the basis for `variablesReference` encoding (`seq + 1`), so clicking a parent frame in the Call Stack panel displays its locals at the point where it called into the child. **Performance optimization:** The naive backward scan is O(seq). For large recordings, this is too slow to call on every step. Two optimizations: (1) **Incremental tracking:** The session maintains a `current_stack` list that is updated incrementally as `current_frame_seq` changes. For **forward** navigation: push on `call` events, pop on `return`/`exception_unwind` events, no change on `line`/`exception`. For **backward** navigation: the push/pop logic is **reversed** — encountering a `call` event going backward means the frame is being exited (pop), encountering a `return`/`exception_unwind` going backward means the frame is being re-entered (push). For single-step navigation (step ±1, step over/in/out), the stack is updated by scanning the few events between old and new seq (usually 1-3 events), so it's effectively O(1). (2) **Checkpoint-boundary cache:** When entering replay mode, build the initial stack at seq=0 (a single `call` event = `[frame_0]`). For `goto_frame` jumps, scan forward from the nearest cached stack rather than backward from target. Cache stacks at checkpoint boundaries when they're computed. Implementation: maintain a `dict[int, list]` mapping `sequence_no -> stack_snapshot` at checkpoint boundaries, seeded with `{0: [frame_0_info]}`. **Population mechanism:** When `goto_frame` resolves via warm navigation (forward scan from a cached stack), the session builds the stack incrementally by processing events from the nearest cached checkpoint to the target. If the scan crosses a checkpoint boundary, the intermediate stack is cached at that boundary for future use. Cold navigation (checkpoint restore + fast-forward) does NOT populate this cache — the fast-forward code in the child process only counts events and does not track stack state. The cache is populated exclusively by the Python-level session code during warm scans.
- `get_variables_at(seq, scope)` — returns locals from recorded frame's `locals_snapshot` JSON. Each variable is `{name, value, type, variablesReference: 0}` (variablesReference=0 means no expandable children). If `locals_snapshot` is `NULL` (e.g., for `call` or `exception_unwind` events reached via `goto_frame`), returns an empty variable list. Navigation methods (`step_over`, `step_into`, `step_back`) always land on `line` events, so this only occurs for direct frame jumps.
- `evaluate_at(seq, expression, context)` — for `hover`/`watch` context: looks up variable name in current frame's `locals_snapshot`, returns its value. For `repl` context: returns informational message. For nested attribute access (`obj.attr`): returns the full object repr (not the attribute — limitation of snapshot approach).
- `step_over()` — find next frame with `frame_event='line'` at same or shallower `call_depth`. This is the DAP `next` operation — it skips over function calls.
- `step_into()` — find next frame with `frame_event='line'` at any depth (i.e., step to the very next executed source line, entering called functions). This is the DAP `stepIn` operation. Note: both `step_over` and `step_into` skip `call`/`return`/`exception` events — they always land on `line` events since those correspond to source lines the user sees.
- `step_out()` — find next `return` or `exception_unwind` event at current depth, then next `line` event at the parent depth. This is the DAP `stepOut` operation. Must check both `return` and `exception_unwind` events because the trace function skips `PyTrace_RETURN` when `arg == NULL` (exception propagation), so only `exception_unwind` records that case. **Edge case:** If `call_depth == 0` (top-level frame), there is no parent depth — navigate to the last frame of the recording and return `{"reason": "end"}`.
- `continue_forward()` — reads breakpoints and exception filters from session state (stored via `set_breakpoints` / `set_exception_breakpoints` RPCs). Scans forward through frames for breakpoint match (file + line). Uses the DB index on `(run_id, filename, line_no)`: for each breakpoint, query `ExecutionFrames.select().where(run_id == X, filename == bp.file, line_no == bp.line, sequence_no > current_seq).order_by(sequence_no).limit(1)`, then take the minimum sequence number across all breakpoints. If exception breakpoints are enabled: `"raised"` filter adds a query for `frame_event == 'exception'`; `"uncaught"` filter adds a query for `frame_event == 'exception_unwind' AND call_depth == 0`. Take the minimum across all results (see query patterns below). If no match found ahead, navigate to the last frame and return `{"reason": "end"}`.

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
# then find next line at parent_depth = current_depth - 1:
parent_line = ExecutionFrames.select().where(
    (ExecutionFrames.run_id == run_id) &
    (ExecutionFrames.frame_event == 'line') &
    (ExecutionFrames.call_depth == current_depth - 1) &
    (ExecutionFrames.sequence_no > exit_event.sequence_no)
).order_by(ExecutionFrames.sequence_no).first()  # .first() returns None if no result
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
- Forward methods (`step_over`, `step_into`, `step_out`, `continue_forward`) at the last frame: return `{"reason": "end", "seq": current_seq}` indicating the end of recording was reached. The adapter sends `StoppedEvent('step')` with `description: "End of recording"` and `text: "End of recording"` (DAP `StoppedEvent` body supports these fields for additional context). Do NOT use `reason: "entry"` for this — DAP reserves `"entry"` for debuggee entry point stops.
- Backward methods (`step_back`, `reverse_continue` — added in Phase 4) at the beginning of recording: land on the first `line` event (NOT seq 0, which is a `call` event with no `locals_snapshot`). Return `{"reason": "start", "seq": first_line_seq}`. The adapter sends `StoppedEvent('step')` with `description: "Beginning of recording"`. Do NOT use `reason: "entry"` — that is reserved for the initial stop after recording completes (see session initialization flow). The session caches `first_line_seq` during `enter_replay()` initialization.
- `step_out` at `call_depth == 0` (top-level frame): there is no parent depth to step to. Navigate to the last frame of the recording and return `{"reason": "end", "seq": last_seq}`.
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

The adapter forwards progress to VSCode using DAP's native progress events: `ProgressStartEvent` (on `configurationDone`), `ProgressUpdateEvent` (on each progress notification), and `ProgressEndEvent` (on recording complete).

### Extension-side pyttd package detection

On `launchRequest`, before spawning the server, the adapter runs `python -c "import pyttd; print(pyttd.__version__)"`. If this fails, it sends an `OutputEvent` with the message: "pyttd is not installed in your Python environment. Install with: pip install pyttd" and fails the launch with a clear error.

### TypeScript side — DAP handlers

`pyttdDebugSession.ts` implements standard DAP handlers:

| DAP Handler | Phase | Behavior |
|---|---|---|
| `initializeRequest` | 3 | Returns capabilities: `supportsConfigurationDoneRequest: true`, `supportsEvaluateForHovers: true`, `supportsProgressReporting: true`. Note: `supportsStepBack`, `supportsGotoTargetsRequest`, `supportsRestartFrame` are NOT advertised yet — added in Phase 4. Do NOT advertise `supportsExceptionInfoRequest` unless an `exceptionInfoRequest` handler is implemented. |
| `launchRequest` | 3 | **Async handler pattern:** `LoggingDebugSession` handlers are synchronous, but launch requires async work (spawn, port read, TCP connect). Do NOT call `sendResponse(response)` immediately — store the response object and call `sendResponse` at the end of the async chain. Implementation: cast `args` to `PyttdLaunchConfig`, call `this.backend.spawn(...).then(() => this.backend.connect(...)).then(() => { ... this.sendEvent(new InitializedEvent()); this.sendResponse(response); }).catch((err) => { this.sendErrorResponse(response, 1, err.message); this.sendEvent(new TerminatedEvent()); })`. The `.catch()` is critical — without it, spawn failures (bad Python path, missing pyttd) or connect timeouts silently swallow the error and leave the debug session in a broken state. Resolves Python path (see below). Spawns `python -m pyttd serve --script <path> --cwd <dir>` (if launch config has `module` instead of `program`, adds `--module` flag). Reads `PYTTD_PORT:<port>` from child stdout (with 10s timeout). Also listens for stderr data events and logs them as error output. Connects TCP. Sends `backend_init` RPC (NOT `initialize` — avoid confusion with DAP's own `initialize`), then `launch` RPC. Then sends `InitializedEvent` and finally `sendResponse`. **Source path normalization:** The adapter must normalize `program` paths to absolute paths (via `path.resolve(cwd, program)`) before sending to the backend, and must normalize source paths from the backend (which uses `co_filename` — already absolute on CPython) for consistent DAP `Source` objects. On Windows, normalize path separators and drive letter casing. |
| `setBreakpointsRequest` | 3 | Stores breakpoints in adapter state keyed by source file + sends `set_breakpoints` RPC to backend with the merged complete list across all files. DAP sends `setBreakpointsRequest` per source file with the complete list for that file — the adapter must maintain a `Map<string, Breakpoint[]>` and send the flattened union to the backend. Called by VSCode during configuration phase (before `configurationDone`) and whenever the user modifies breakpoints during replay. |
| `setExceptionBreakpointsRequest` | 3 | Configures exception breakpoint filters. The `filters` array contains active filter IDs (as declared in `package.json` `exceptionBreakpointFilters`): `"raised"` = stop on ALL `exception` frame events during continue/reverse-continue; `"uncaught"` = stop only on `exception_unwind` events at `call_depth == 0` (exceptions that propagate out of the top-level frame). Backend stores active filters and applies them in `continue_forward` / `reverse_continue` queries. |
| `configurationDoneRequest` | 3 | Sends `configuration_done` RPC to backend. Backend starts recording thread (executes user script). |
| `threadsRequest` | 3 | Returns `[{id: 1, name: "Main Thread"}]`. Multi-thread: deferred to Phase 7. |
| `stackTraceRequest` | 3 | Sends `get_stack_trace` RPC with `current_frame_seq` from last StoppedEvent |
| `scopesRequest` | 3 | Returns `[{name: "Locals", variablesReference: <encodedRef>}]`. Use `sequence_no + 1` as `variablesReference` (DAP reserves 0 to mean "no variables", so raw seq 0 would incorrectly hide variables). In `stackTraceResponse`, set `frameId = seq` for each stack frame. In `scopesRequest(frameId=seq)`, set `variablesReference = seq + 1`. In `variablesRequest(variablesReference=ref)`, decode as `seq = ref - 1` and query that frame's `locals_snapshot`. This avoids maintaining a separate ID mapping while avoiding the `variablesReference: 0` edge case. |
| `variablesRequest` | 3 | Decodes `variablesReference` as `seq = ref - 1` (see `scopesRequest` encoding), sends `get_variables` RPC with that seq. Returns flat name=value pairs with `variablesReference: 0` (no expandable children). |
| `evaluateRequest` | 3 | `hover`/`watch` context: sends `evaluate` RPC. `repl` context: returns informational message ("Replay mode — expression evaluation not available. Use Variables panel to inspect recorded state."). |
| `continueRequest` | 3 | Sends `continue` RPC. On result, sends `StoppedEvent('breakpoint')` if stopped at a breakpoint, or `StoppedEvent('step')` with `description: "End of recording"` if the end of recording was reached (backend returns `reason: "end"`). Map backend `reason` field: `"breakpoint"` → `StoppedEvent('breakpoint')`, `"exception"` → `StoppedEvent('exception')`, `"end"` → `StoppedEvent('step')` with description, `"step"` → `StoppedEvent('step')`. |
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
            this.sendEvent(new ProgressEndEvent(this.progressToken));
            this.sendEvent(new StoppedEvent('entry', 1));
            break;
        case 'output':
            this.sendEvent(new OutputEvent(params.output, params.category));
            break;
        case 'progress':
            this.sendEvent(new ProgressUpdateEvent(this.progressToken,
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

- `pyttd/server.py` — JSON-RPC server (TCP, two-thread model: RPC thread + recording thread). Owns stdout/stderr capture via `os.pipe()` + `os.dup2()` (timed after port handshake, before recording starts). Computes DB path: for script mode, `os.path.splitext(os.path.basename(script))[0] + DB_NAME_SUFFIX` placed in the **script's directory**; for module mode (`--module` flag), `module_name.replace('.', '_') + DB_NAME_SUFFIX` placed in `--cwd` (consistent with `_cmd_record`). The `--cwd` argument sets the working directory for the user script. Deletes existing DB + WAL/SHM files via `storage.delete_db_files()` before recording to ensure fresh schema (avoids stale columns from older phases).
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
| `stepBackRequest` | Sends `step_back` RPC. Backend decrements `current_seq` to previous `line` event, reads from SQLite (always warm — no checkpoint needed for ±1 step). Adapter sends `StoppedEvent('step')`. At beginning of recording, lands on first `line` event (NOT seq 0, which is a `call` event with no locals) and returns `{"reason": "start"}` — adapter sends `StoppedEvent('step')` with `description: "Beginning of recording"`. |
| `reverseContinueRequest` | Sends `reverse_continue` RPC with current breakpoint list. Backend scans backward via DB index for breakpoint match. Adapter sends `StoppedEvent('breakpoint')` if a breakpoint was hit. If no breakpoint found behind current position, lands on first `line` event and adapter sends `StoppedEvent('step')` with `description: "Beginning of recording"` (do NOT use `reason: "entry"` — reserved for initial stop after recording completes). |
| `gotoTargetsRequest` | Sends `goto_targets` RPC with file and line. Backend returns list of `{seq, function_name}` for matching `line` events (capped at 1000 to avoid multi-MB responses for hot loop lines). Adapter maps each to a `GotoTarget` with `id = seq` (DAP `targetId` is an opaque integer — using `seq` directly avoids maintaining a separate mapping). Adapter stores these in a `Map<number, GotoTarget>` for validation. |
| `gotoRequest` | Extracts `targetId` from the request (which is `seq` — see `gotoTargetsRequest` mapping). Sends `goto_frame` RPC with that `seq`. Backend uses cold navigation (checkpoint restore) if jumping far, warm (DB read) if within recent range. Adapter sends `StoppedEvent('goto')`. |
| `restartFrameRequest` | Extracts the `frameId` from the request (which is `seq` — see stack trace encoding). Sends `goto_frame` RPC with the first `line` event's `seq` within that frame. The adapter finds the `call` event for the selected stack frame, then the backend navigates to the first `line` event at that depth within that frame (so variables are visible). Backend query: find first `line` event where `sequence_no > call_seq AND call_depth == frame_depth` (must be `==`, not `>=` — using `>=` would incorrectly match lines in child calls if the first thing the function does is call another function). Reuses the `goto_frame` RPC method — no separate method needed. |

### Navigation mode clarification

- **Warm navigation** (always used for step ±1, continue, reverse-continue, and any DB-backed query): Reads frame data directly from SQLite. Sub-millisecond. Variables are `repr()` snapshots — flat, non-expandable. This is the **primary** navigation mode for all operations.
- **Cold navigation** (used only for `goto_frame` jumps to distant frames when live object state reconstruction is desired): Checkpoint restore + fast-forward via pipe-based IPC. 50-300ms. Produces the same `repr()` snapshots but through live re-execution. With I/O hooks active (this phase), non-deterministic function calls produce the same values as during recording.

**Key insight:** `step_back` is always warm. It simply decrements `current_frame_seq` and reads the previous frame from SQLite. No checkpoint needed. Cold navigation is only triggered by explicit `goto_frame` requests (from the timeline scrubber, goto targets, or restart frame) when the target is far from any warm child's position.

### Reverse continue

Scans backward through the `(run_id, sequence_no)` index checking `(filename, line_no)` against the breakpoint set. Uses the DB index on `(run_id, filename, line_no)` to accelerate: query for `sequence_no < current AND filename = bp.file AND line_no = bp.line ORDER BY sequence_no DESC LIMIT 1` for each breakpoint, then take the maximum. This is O(breakpoints) indexed queries, not O(frames).

If exception breakpoints are enabled (via `set_exception_breakpoints`), the reverse-continue also queries backward for exception events — same logic as `continue_forward`: `"raised"` filter matches `frame_event == 'exception'`, `"uncaught"` filter matches `frame_event == 'exception_unwind' AND call_depth == 0`. Take the maximum `sequence_no` across all breakpoint and exception queries. This mirrors the forward logic symmetrically.

Note: breakpoints added during replay (after recording) are handled by the DB query — no special pre-recording index needed.

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

### JSON-RPC messages added

```json
{"jsonrpc": "2.0", "id": 17, "method": "step_back", "params": {}}
{"jsonrpc": "2.0", "id": 18, "method": "reverse_continue", "params": {}}
{"jsonrpc": "2.0", "id": 19, "method": "goto_frame", "params": {"seq": 500}}
{"jsonrpc": "2.0", "id": 20, "method": "goto_targets", "params": {"file": "...", "line": 10}}
```

Note: `continue` and `reverse_continue` do NOT pass breakpoints in params — both read from session state (set via `set_breakpoints` and `set_exception_breakpoints` RPCs). This keeps the navigation API stateless from the adapter's perspective; the adapter sends breakpoint updates once, and all navigation commands use the stored set.

### I/O hooks (`iohook.c`)

During recording, intercept non-deterministic functions by **replacing module attributes** at the C level using `PyObject_SetAttrString()`:
- Save original: `orig_time_time = PyObject_GetAttrString(time_module, "time")`
- Replace: `PyObject_SetAttrString(time_module, "time", hooked_time_time_pyfunc)`
- The hook calls the original, logs the return value as an `IOEvent`, and returns it

**IOEvent storage mechanism:** The I/O hooks are Python-callable C functions (installed as module attributes). When a hooked function is called by the user's script, the hook has the GIL (called from Python code). It stores the IOEvent by calling a Python callback (`io_flush_callback`) provided during initialization, which inserts into the `IOEvent` table via Peewee. Unlike frame events (which go through the ring buffer for async flush), IOEvents are written synchronously because they're infrequent and must be committed before any checkpoint that follows.

Hooks are installed at the start of recording (in `start_recording()`) and removed at the end (in `stop_recording()`).

**Limitation:** If user code captures a function reference before hooks are installed (e.g., `t = time.time` at module level), the captured reference bypasses the hook. This is documented as a known limitation.

Target functions: `time.time`, `time.monotonic`, `time.perf_counter`, `random.random`, `random.randint`, `os.urandom`.

**File I/O hooks (optional, deferred to Phase 7 if too complex):** Hook `builtins.open` to return wrapper file objects that intercept `read()`/`readline()`/`readlines()` and log results. This requires wrapping the file object protocol (iteration, context manager, `seek`, `tell`, `close`, etc.) which is substantially more complex than the scalar function hooks above. For Phase 4, **prioritize the scalar hooks** (`time.*`, `random.*`, `os.urandom`). If time permits, hook `os.read()` as a simpler alternative to full `open()` wrapping. Cold navigation of code that reads files will produce non-deterministic results without these hooks — this is acceptable and should be documented as a known limitation until file I/O hooks are implemented.

**Replay mode (inside a resumed checkpoint child):** When a checkpoint child wakes for fast-forward, the I/O hooks must switch from recording mode to replay mode. The mechanism:

1. Before the child's fast-forward begins, the parent sends the `RESUME` command. The child's checkpoint wake-up code sets a static `g_io_replay_mode = 1` flag in `iohook.c` and pre-loads all `IOEvent` records for the current `run_id` with `sequence_no > checkpoint_seq` into a sorted list (queried via a Python callback `io_replay_loader` provided during `start_recording()`). An `io_replay_cursor` index tracks the next event to consume.

2. In replay mode, each hooked function (e.g., `hooked_time_time`) checks `g_io_replay_mode`. If set, instead of calling the original function and logging, it reads the next `IOEvent` from the pre-loaded list (matched by `function_name`), deserializes the `return_value` using the type-specific format (raw IEEE 754 double for floats, length-prefixed for bytes, etc.), advances `io_replay_cursor`, and returns the deserialized value. If the cursor is exhausted (more I/O calls during fast-forward than were recorded — should not happen for deterministic code), the hook falls back to calling the original function and logs a warning.

3. The pre-loaded list approach avoids per-call DB queries during fast-forward (which would be slow for tight loops calling `time.time()`). The list is ordered by `(sequence_no, io_sequence)` matching the recording order.

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

- `ext/iohook.c/h` — full implementation (module attribute replacement, logging, replay stubs, type-specific serialization)
- `pyttd/models/io_events.py` — IOEvent Peewee model
- `tests/test_iohook.py` — record script with `time.time()` and `random.random()`, verify same values on cold replay

### Update

- **`pyttd/session.py`** — Add `step_back`, `reverse_continue`, `goto_frame`, `goto_targets` methods (see query patterns above)
- **`vscode-pyttd/src/debugAdapter/pyttdDebugSession.ts`** — Add DAP handlers: `stepBackRequest`, `reverseContinueRequest`, `gotoTargetsRequest`, `gotoRequest`, `restartFrameRequest`. Update `initializeRequest` to advertise new capabilities: `supportsStepBack: true`, `supportsGotoTargetsRequest: true`, `supportsRestartFrame: true`
- **`pyttd/server.py`** — Add RPC handlers for `step_back`, `reverse_continue`, `goto_frame`, `goto_targets`
- **`ext/recorder.c/h`** — Update `pyttd_start_recording()` to accept two additional keyword arguments: `io_flush_callback` (for recording-mode IOEvent storage) and `io_replay_loader` (for replay-mode IOEvent pre-loading). Both are stored as `PyObject*` (with `Py_INCREF`) alongside the existing `flush_callback`. `io_flush_callback` is passed through to `iohook.c` for synchronous IOEvent storage during recording. `io_replay_loader` is called by the checkpoint child's wake-up code to pre-load IOEvents for deterministic fast-forward (see I/O replay mechanism above). Update the `PyttdMethods` docstring accordingly.
- **`pyttd/recorder.py`** — Update `start()` to include `IOEvent` in `initialize_schema` and pass both I/O callbacks to `start_recording()`:
  ```python
  def _on_io_event(self, event: dict):
      """Called synchronously by C I/O hooks (with GIL held) to insert a single IOEvent."""
      event['run_id'] = self._run.run_id
      IOEvent.create(**event)

  def _load_io_events_for_replay(self, run_id_bytes: bytes, after_seq: int) -> list[dict]:
      """Called by checkpoint child to pre-load IOEvents for deterministic fast-forward.
      Returns list of {function_name, return_value} dicts ordered by (sequence_no, io_sequence)."""
      from uuid import UUID
      run_id = UUID(bytes=run_id_bytes)
      return list(IOEvent.select(IOEvent.function_name, IOEvent.return_value)
          .where((IOEvent.run_id == run_id) & (IOEvent.sequence_no > after_seq))
          .order_by(IOEvent.sequence_no, IOEvent.io_sequence)
          .dicts())
  ```
  Then pass `io_flush_callback=self._on_io_event` and `io_replay_loader=self._load_io_events_for_replay` in the `start_recording()` call
- **`tests/conftest.py`** — Update `db_setup` fixture to include `IOEvent` in schema initialization:
  ```python
  from pyttd.models.io_events import IOEvent
  storage.initialize_schema([Runs, ExecutionFrames, Checkpoint, IOEvent])
  ```

### Verify

1. Step Back repeatedly from end -> each step shows previous line with correct variables
2. Set breakpoint on a line, click Reverse Continue -> stops at that line
3. Add a NEW breakpoint during replay, Reverse Continue -> stops at it (DB query approach)
4. Forward stepping (Next, Step In, Step Out) still works correctly
5. Goto frame via command palette -> jumps to arbitrary frame
6. Record a script with `time.time()` calls, cold-navigate -> verify same time values
7. Record a script with `random.random()` calls, cold-navigate -> verify same random values
8. Performance: warm step < 10ms, cold jump < 300ms
9. `.venv/bin/pytest tests/test_iohook.py` passes

---

## Phase 5: Timeline Scrubber Webview

**Goal:** A visual timeline panel in the Debug sidebar. Drag or click to any frame. Shows call depth bars, exception markers, breakpoint markers, current position.

### Timeline data model (`pyttd/models/timeline.py`)

```python
def get_timeline_summary(run_id, start_seq, end_seq, bucket_count=500) -> list[dict]:
    """Return downsampled timeline data for rendering.

    Each bucket: {startSeq, endSeq, maxCallDepth, hasException,
                  hasBreakpoint, dominantFunction}

    Uses SQL GROUP BY on sequence number ranges from ExecutionFrames.
    """
    ...
```

This is a query module, not a Peewee model. It aggregates data from `ExecutionFrames` using SQL `GROUP BY` on sequence number ranges.

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
- Mouse drag/click on canvas -> compute target frame from X position -> post `scrub` message to extension -> extension sends `gotoFrame` custom request to adapter
- Keyboard: Left/Right arrow = step back/forward, Home = go to frame 0, End = go to last frame, Page Up/Down = jump by 10% of total frames
- Mousewheel on canvas = zoom in/out on timeline. Zoom re-requests buckets from the backend at higher resolution for the visible range (`get_timeline_summary(run_id, start_seq, end_seq, bucket_count)`). Cache the previous bucket set client-side to avoid round-trip lag during fast zoom.

**Communication:** Custom DAP events via `sendEvent()` with the `Event` base class from `@vscode/debugadapter`:
```typescript
// In pyttdDebugSession.ts:
this.sendEvent(new Event('pyttd/timelineData', { buckets: [...], totalFrames: 25000 }));
this.sendEvent(new Event('pyttd/positionChanged', { seq: 1500, file: '...', line: 10 }));
```
- `pyttd/timelineData` — sent after recording completes (initial bucket set)
- `pyttd/positionChanged` — sent on every navigation action (step, goto, scrub)

Extension main (`extension.ts`) listens for these custom events via `debug.onDidReceiveDebugSessionCustomEvent` and relays to webview via `postMessage`.

### JSON-RPC messages added

```json
{"jsonrpc": "2.0", "id": 21, "method": "get_timeline_summary", "params": {"startSeq": 0, "endSeq": 25000, "bucketCount": 500}}
```

### Create

- `pyttd/models/timeline.py` — timeline summary query functions
- `vscode-pyttd/src/views/timelineScrubberProvider.ts` — WebviewViewProvider
- `vscode-pyttd/src/views/timelineScrubber.html` — timeline webview HTML
- `vscode-pyttd/src/views/timelineScrubber.js` — canvas rendering, interaction handlers
- `vscode-pyttd/src/views/timelineScrubber.css` — themed styles using VSCode CSS variables
- Update `package.json` to register the webview view in `contributes.views.debug`
- Update `extension.ts` to register `TimelineScrubberProvider` and relay custom events

### Verify

1. Timeline panel appears in Debug sidebar when a pyttd session is active
2. Drag scrubber -> editor cursor, variables, and stack all update
3. Step back/forward in debug toolbar -> timeline cursor moves in sync
4. Keyboard navigation works (arrow keys, Home/End)
5. Exception markers visible as red bars at correct positions
6. Zoom in/out shows higher/lower resolution
7. Smooth interaction at 60fps for recordings with 100k+ frames
8. Renders correctly in both dark and light themes

---

## Phase 6: CodeLens, Inline Values, Call History Tree

**Goal:** Rich editor integration.

### CodeLens

Above each traced function: "TTD: 47 calls | 3 exceptions". Click -> jump to first execution of that function in the timeline. `CodeLensProvider` queries backend `get_execution_stats(filename)` — a single query per file returning stats for ALL functions in that file (batched, not per-function). Only queries files that appear in the recorded trace (backend maintains a `traced_files` set; extension queries this on session start and only activates CodeLens for matching documents).

Register in `package.json`:
```jsonc
"contributes": {
  "languages": [{ "id": "python" }]
}
```

The CodeLens provider only activates during a pyttd debug session (check `vscode.debug.activeDebugSession?.type === 'pyttd'`).

### Inline Values

`InlineValuesProvider` returns `InlineValueText` items showing variable values at the current stopped frame. Updates on every step/navigation — the provider is refreshed when a `StoppedEvent` is received. Shows values for variables assigned on visible lines. Debounced: waits 50ms after the last navigation event before querying the backend (avoids flooding during rapid step-back).

Register via `vscode.languages.registerInlineValuesProvider('python', provider)` in `extension.ts`.

### Call History

`TreeDataProvider` in Debug sidebar. Register in `package.json`:
```jsonc
"contributes": {
  "views": {
    "debug": [{
      "id": "pyttd.callHistory",
      "name": "Call History",
      "when": "debugType == 'pyttd'"
    }]
  }
}
```

Expandable call tree built from call/return frame event pairs (matched using a stack in `session.py`). Each node shows: function name, filename:line, frame sequence range. Click node -> navigate to that frame's `call` event. Exception calls shown with a distinct icon (e.g., red circle). **Lazy loading:** Children are fetched on expand (backend query: `get_call_children(run_id, parent_call_seq, parent_return_seq)`), not upfront. **Interrupted recordings:** Call events without a matching return (recording stopped mid-execution) are shown with an "incomplete" indicator.

### JSON-RPC messages added

```json
{"jsonrpc": "2.0", "id": 22, "method": "get_execution_stats", "params": {"filename": "/path/to/script.py"}}
{"jsonrpc": "2.0", "id": 23, "method": "get_traced_files", "params": {}}
{"jsonrpc": "2.0", "id": 24, "method": "get_call_children", "params": {"parentCallSeq": 100, "parentReturnSeq": 500}}
{"jsonrpc": "2.0", "id": 25, "method": "get_variables_at", "params": {"seq": 1500, "visibleLines": [10, 11, 12, 13, 14, 15]}}
```

### Create

- `vscode-pyttd/src/providers/codeLensProvider.ts`
- `vscode-pyttd/src/providers/inlineValuesProvider.ts`
- `vscode-pyttd/src/providers/callHistoryProvider.ts`
- `vscode-pyttd/src/providers/decorationProvider.ts`
- Update `package.json` to register tree view (`contributes.views.debug`) and CodeLens language
- Update `extension.ts` to register all providers (CodeLens, InlineValues, CallHistory)
- Add backend methods to `session.py`: `get_execution_stats()`, `get_traced_files()`, `get_call_children()`

### Verify

1. CodeLens annotations appear above functions in traced files
2. CodeLens does NOT appear on files not in the trace
3. Click CodeLens -> timeline jumps to first execution of that function
4. Inline values visible next to assignments during debug, update on step
5. Call History tree is expandable, clickable, shows correct nesting
6. Exception calls are visually distinguished
7. Interrupted recording: top-level call shown with "incomplete" marker, tree still navigable

---

## Phase 7: Polish, Performance, Packaging

**Goal:** Production quality. VSIX for marketplace, wheel for PyPI.

### Performance targets

| Metric | Target | Notes |
|---|---|---|
| Recording overhead (I/O-bound) | < 2x | Most overhead is in the frame eval hook |
| Recording overhead (compute-bound) | < 5-10x | Short, fast functions amplify per-frame cost |
| Ring buffer flush | < 5ms per 1000 frames | Background thread, batched inserts |
| Step back/forward (warm) | < 10ms | SQLite indexed read |
| Jump to frame (cold) | < 300ms | Checkpoint restore + fast-forward |
| Timeline scrub | < 16ms per update | 60fps target |
| DB size per frame | < 500 bytes | Excludes globals; locals are repr strings |
| Checkpoint memory | < 50MB each (CoW) | Only dirty pages count after fork |

### Work

**Error handling:**
- Backend crash detection: adapter monitors child process exit code, sends error event to VSCode
- RPC timeout: 5s default, configurable via launch config `rpcTimeout` property
- User scripts with syntax errors: caught by `runpy`, reported as error event with traceback
- User scripts that call `os.fork()`: document as unsupported, may cause undefined behavior with checkpoint manager
- Orphan process cleanup: adapter registers `process.on('exit')` handler to kill Python backend
- DB write errors: if the script's directory is read-only, fall back to temp directory and warn

**Multi-thread recording:**
- The PEP 523 frame eval hook is per-interpreter and automatically covers all threads
- Ring buffer upgrade from SPSC to **MPSC** (multiple-producer single-consumer) — recommended approach: per-thread SPSC buffers (thread-local storage) merged by flush thread, avoids contention on hot path
- Each frame event gets a `thread_id` field (add `IntegerField` to `ExecutionFrames`)
- `threadsRequest` returns actual thread list from recorded data
- Note: checkpoint correctness still requires single-threaded state at fork time

**Generator/async frames:** Recorded as regular frames with appropriate event types. No special coroutine visualization in v1.

**VSCode extension testing:**
- Set up Mocha test framework (`npm test`)
- Test DAP message handling with mock backend (fake TCP server returning canned JSON-RPC responses)
- Test backendConnection spawn/connect/close lifecycle
- Test timeline scrubber rendering with mock data

**VSIX packaging:**
- `vsce package` (or `@vscode/vsce`)
- Marketplace metadata: publisher, icon (create simple icon), categories, README
- Extension should work without Python extension installed (standalone debug adapter)

**PyPI wheel:**
- Build matrix for Python 3.12/3.13/3.14
- Platform wheels for Linux x86_64/aarch64 and macOS x86_64/arm64
- Source distribution for Windows and other platforms (compiles from source)
- `cibuildwheel` for CI wheel builds

**CI (GitHub Actions):**
- Test matrix: Python 3.12/3.13/3.14, Linux/macOS/Windows
- ASAN build on Linux
- `npm test` for extension
- `vsce package` validation (no publish)

**Documentation:** Getting started guide, architecture doc, known limitations, CLI reference.

### Known limitations to document

1. Variables in replay mode are `repr()` snapshots, not live expandable objects
2. Debug Console REPL cannot evaluate arbitrary expressions (replay mode)
3. C extension internal state is opaque — third-party C extension object `repr()` may not be informative
4. Windows: record + browse only, no checkpoint-based cold navigation
5. Multi-threaded programs: checkpoint correctness requires single-threaded state at fork time
6. Async/await: recorded as regular frames, no coroutine-specific visualization
7. Relative imports: scripts using `from . import` must be launched via `module` mode, not `program` mode
8. Very large locals (> 256 bytes repr): truncated in recording
9. User scripts that call `os.fork()` directly may conflict with checkpoint manager
10. Output capture: uses `os.dup2` — scripts that close/redirect their own file descriptors may interfere
11. I/O hooks: function references captured before recording starts (e.g., `t = time.time` at module level) bypass the hooks

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


**Phase 0 -> Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 -> Phase 6 -> Phase 7**

This builds the system bottom-up: C foundation first, then Python layer, then TypeScript layer, then polish.

## Estimated Code

| Component | Language | Lines |
|---|---|---|
| C extension (ext/) | C | ~1,800 |
| Python backend (pyttd/) | Python | ~1,500 |
| Debug Adapter | TypeScript | ~900 |
| VSCode Extension + Views | TypeScript + HTML/JS/CSS | ~1,300 |
| Tests | Python + TypeScript | ~900 |
| Config/Build/Docs | Various | ~500 |
| **Total** | | **~6,900** |

## Critical Files

- `ext/recorder.c` — Core of the system. PEP 523 frame eval hook + ring buffer. Everything depends on this. Must handle GIL correctly for flush thread. Must pause flush thread before fork. Version-gated APIs for 3.12-3.14 vs 3.15+ (with `#ifdef` fallback). Trace function must skip `PyTrace_RETURN` when `arg == NULL`. `exception_unwind` must be recorded BEFORE decrementing `call_depth`. `call_depth` initialized to -1 (top-level user frame is depth 0). Locals iteration must use `PyMapping_Items()` on 3.13+ (or `PyDict_Next()` fast path on 3.12) for `FrameLocalsProxy` compatibility. All strings copied into double-buffered string pool. Direct `tstate->c_tracefunc` access is a known portability risk.
- `ext/checkpoint.c` — Fork-based checkpoint manager with pipe-based IPC, condition-variable-based pre-fork synchronization, and exponential thinning eviction. Hardest systems-programming component.
- `ext/iohook.c` — Module attribute replacement for deterministic replay. Required for correct cold navigation of non-deterministic code. Type-specific serialization for faithful value replay.
- `vscode-pyttd/src/debugAdapter/pyttdDebugSession.ts` — All ~20 DAP handlers. Every VSCode debug interaction flows through this file.
- `pyttd/server.py` — JSON-RPC bridge over TCP. Two-thread model (RPC + recording). Port discovery handshake, stdout/stderr capture timing, progress events, signal handling, clean shutdown.
- `pyttd/session.py` — Navigation logic: warm/cold path selection, call depth tracking, breakpoint matching, stack reconstruction, forward/backward stepping.
- `pyttd/runner.py` — User script execution via `run_path` and `run_module`. Simple `runpy` wrapper — does NOT own stdout/stderr capture (that is `server.py`'s responsibility).
- `pyttd/protocol.py` — JSON-RPC Content-Length framing over TCP. Shared wire format between adapter and backend.
- `pyttd/models/frames.py` — Must be rewritten: switch `frame_id` to `AutoField` (insert_many doesn't apply Python defaults), add call_depth, function_name, sequence_no, and composite indexes including `(run_id, frame_event, sequence_no)` for reverse navigation. All query/replay/UI features read from this.
