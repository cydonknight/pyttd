# pyttd

**pyttd** (Python Time-Travel Debugger) is an open-source time-travel debugger for Python with full VSCode integration. It records complete program execution at the C level, then lets you step backward and forward, jump to any point in the trace, and visually scrub through a timeline — all from within your editor.

---

## What is Time-Travel Debugging?

Traditional debuggers only move forward. If you step past a bug, you start over. pyttd takes a different approach: it records every execution frame during a single run, then drops you into a **replay session** where you can navigate freely in both directions.

This means you can:

- **Step backward** through execution to see exactly how state evolved
- **Reverse continue** to find the last time a breakpoint was hit before the current position
- **Jump to any frame** in the entire recording instantly
- **Scrub a visual timeline** to navigate through call depth, exceptions, and execution flow
- **Inspect variables** at any point in the recording without re-running the program

pyttd is a **post-mortem replay debugger** — your script runs to completion (or interruption), and then you debug the recorded trace. This gives you perfect reproducibility: the same recording, explored as many times as you need.

---

## Architecture

pyttd is a three-layer system:

```
VSCode Extension Host
  +-------------------------------------------------+
  |  Extension (TypeScript)                         |
  |  +-------------------------------------------+ |
  |  | Debug Adapter (inline, LoggingDebugSession)| |
  |  | Translates DAP <-> JSON-RPC               | |
  |  +---------------------+---------------------+ |
  |                        | JSON-RPC over TCP      |
  |  +-------------------------------------------+ |
  |  | Timeline Webview, CodeLens, Inline Values, | |
  |  | Call History Tree                          | |
  |  +-------------------------------------------+ |
  +-------------------------------------------------+
                           | TCP (localhost)
  +-------------------------------------------------+
  |  Python TTD Backend (child process)             |
  |  +----------------+  +------------------------+|
  |  | pyttd_native   |  | pyttd (Python)         ||
  |  | (C extension)  |  | - JSON-RPC server      ||
  |  | - recorder     |  | - session navigation   ||
  |  | - ring buffer  |  | - script execution     ||
  |  | - checkpoints  |  | - Peewee ORM / SQLite  ||
  |  | - replay       |  | - query API            ||
  |  | - I/O hooks    |  +------------------------+||
  |  +----------------+                             |
  +-------------------------------------------------+
```

### C Extension (`pyttd_native`)

The performance-critical core, written in C and loaded as a CPython extension module.

**Frame Eval Hook (PEP 523):** Instead of using Python's `sys.settrace`, pyttd installs a custom frame evaluation function via `_PyInterpreterState_SetEvalFrameFunc`. This hooks into CPython's interpreter loop at the C level, capturing every frame with minimal overhead. The hook extracts the filename, function name, line number, event type, and `repr()` snapshots of local variables.

**Lock-Free Ring Buffer:** Captured frame events are written into a lock-free single-producer single-consumer (SPSC) ring buffer with a power-of-2 capacity (default 65,536 entries). A separate string pool arena holds serialized locals. A background flush thread wakes every 10ms (or at 75% capacity) and batch-inserts frames into SQLite via Peewee. The flush thread acquires the GIL only during the Python-side insert, keeping contention low.

**Fork-Based Checkpointing (Unix):** During recording, the extension periodically calls `fork()` to create full-process snapshots. Each checkpoint child blocks on a pipe, frozen in time. When the user jumps to a distant frame, the nearest checkpoint child is woken via the pipe, fast-forwards to the target frame (counting frames without serializing — pure speed), serializes the state, and sends it back through a result pipe. The parent process relays this to the debug adapter.

Checkpoints are managed with an **exponential thinning** eviction strategy: recent checkpoints are dense, older ones are exponentially spaced, giving O(log N) coverage of the full recording within a fixed budget of 32 live checkpoints.

**I/O Hooks:** During recording, non-deterministic functions (`time.time`, `random.random`, `os.urandom`, file reads) are intercepted by replacing module attributes at the C level. Return values are logged. During checkpoint-based replay, these functions return the logged values, ensuring deterministic state reconstruction.

### Python Backend (`pyttd/`)

The Python layer manages the recording session, trace database, navigation logic, and communication with the VSCode debug adapter.

**JSON-RPC Server:** On launch, the server binds a TCP socket on `localhost` (OS-assigned port), writes `PYTTD_PORT:<port>` to stdout as a handshake, then communicates exclusively over TCP. User script stdout/stderr is captured separately and forwarded as JSON-RPC events — they never touch the protocol channel.

**Session & Navigation:** The session manager tracks the current position in the trace and handles all navigation commands. Two modes:

| Mode | When Used | Latency | How It Works |
|---|---|---|---|
| **Warm** | Stepping +/-1, continue within recorded data | < 10ms | Reads frame data directly from SQLite |
| **Cold** | Jumping to a distant frame | 50-300ms | Restores nearest checkpoint, fast-forwards to target |

Both modes return `repr()` snapshots of variables. Cold navigation additionally reconstructs live process state inside the checkpoint child for deterministic I/O replay.

**Trace Database:** Each recording produces a `<script>.pyttd.db` SQLite file (WAL mode). Frames are indexed by `(run_id, sequence_no)` and `(run_id, filename, line_no)` for efficient forward/backward scanning and breakpoint matching.

### VSCode Extension (`vscode-pyttd/`)

A TypeScript extension providing the full debug experience through the Debug Adapter Protocol (DAP).

**Debug Adapter:** Runs inline in the extension host via `DebugAdapterInlineImplementation`. Implements ~20 DAP handlers including all standard navigation (step in/out/over, continue) plus time-travel extensions (step back, reverse continue, goto frame, restart frame). Capabilities advertised: `supportsStepBack`, `supportsGotoTargetsRequest`, `supportsRestartFrame`.

**Timeline Scrubber:** A `WebviewViewProvider` rendering a `<canvas>`-based timeline in the Debug sidebar. Shows call depth as vertical bars, exception markers in red, breakpoint regions in blue, and a yellow cursor for the current position. Supports drag-to-scrub, keyboard navigation (arrow keys, Home/End, Page Up/Down), and mousewheel zoom with resolution-adaptive re-bucketing.

**CodeLens:** Annotations above traced functions showing execution stats ("47 calls | 3 exceptions"). Click to jump to the first execution in the timeline.

**Inline Values:** Variable values displayed inline in the editor at the current debug position, updated on every step.

**Call History Tree:** An expandable tree view in the Debug sidebar showing the call hierarchy. Click any node to navigate to that frame. Exception calls are visually distinguished. Lazy-loaded on expand.

---

## Session Lifecycle

1. **F5** in VSCode with a `pyttd` launch configuration
2. Extension spawns `python -m pyttd.server --script <path>`
3. Server binds TCP, reports port, connects to debug adapter
4. **Recording phase:** User script executes with the C frame eval hook active. All frames are captured to the ring buffer, flushed to SQLite. Checkpoints are created at configurable intervals.
5. Script completes (or user clicks Stop for early interruption)
6. **Replay phase:** User is now in replay mode. The debug toolbar shows step back/forward, reverse continue, and all standard navigation. The timeline scrubber appears. Variables and call stack reflect the current position.
7. Navigate freely: step backward, jump to any frame, scrub the timeline, set breakpoints and reverse-continue to them
8. **Disconnect** tears down checkpoint children, closes the socket, and exits

---

## Platform Support

| Platform | Recording | Warm Navigation | Cold Navigation (Checkpoints) |
|---|---|---|---|
| **Linux** | Full | Full | Full |
| **macOS** | Full | Full | Partial (single-threaded at fork time) |
| **Windows** | Full | Full | Not available (no `fork()`) |

On Windows, all recording and SQLite-backed frame browsing works. Cold navigation (checkpoint restore) is unavailable — the system falls back to warm-only mode with recorded `repr()` snapshots.

---

## Requirements

- **Python >= 3.12** (required for `PyUnstable_InterpreterFrame_*` C API accessors)
- **VSCode** (for the debug extension; CLI works standalone)
- **C compiler** (gcc, clang, or MSVC — for building the extension)

---

## Installation

_Not yet available on PyPI._

For development:

```bash
# Clone and install in development mode (compiles C extension)
git clone https://github.com/your-username/pyttd.git
cd pyttd
pip install -e .

# Verify C extension loads
python -c "import pyttd_native; print(dir(pyttd_native))"

# Build VSCode extension
cd vscode-pyttd
npm install
npm run compile
```

---

## Usage

### CLI

```bash
# Record a script
python -m pyttd.cli record script.py

# Query the last recording
python -m pyttd.cli query --last-run --frames --limit 20

# Replay and jump to a specific frame
python -m pyttd.cli replay --last-run --goto-frame 750
```

### VSCode

1. Install the pyttd extension (from VSIX or Extension Development Host)
2. Add a launch configuration to `.vscode/launch.json`:

```json
{
  "type": "pyttd",
  "request": "launch",
  "name": "Time-Travel Debug",
  "program": "${file}"
}
```

3. Press **F5** to record and debug

---

## Project Structure

```
pyttd/
  ext/                         # C extension source
    pyttd_native.c             # Module init, Python bindings
    recorder.c/h               # PEP 523 frame eval hook
    ringbuf.c/h                # Lock-free SPSC ring buffer
    frame_event.h              # FrameEvent struct
    checkpoint.c/h             # Fork-based checkpoint manager (Unix)
    checkpoint_store.c/h       # Checkpoint index, eviction
    replay.c/h                 # Checkpoint resume + fast-forward
    iohook.c/h                 # Deterministic I/O replay hooks
    platform.h                 # Platform detection macros
  pyttd/                       # Python package
    main.py                    # @ttdbg decorator, public API
    cli.py                     # CLI interface
    server.py                  # JSON-RPC server (TCP)
    protocol.py                # JSON-RPC framing
    session.py                 # Navigation logic
    query.py                   # Trace data queries
    recorder.py                # Python wrapper around C recorder
    replay.py                  # Replay controller
    runner.py                  # User script execution
    models/                    # Peewee ORM
      base.py                  # Base model class
      frames.py                # ExecutionFrames model
      runs.py                  # Runs model
      storage.py               # DB connection, batch insert
      checkpoints.py           # Checkpoint index model
      io_events.py             # I/O event model
      timeline.py              # Timeline summary queries
    tracing/
      enums.py                 # Event type enum
      constants.py             # Ignore patterns
    performance/
      clock.py                 # High-resolution clock
      performance.py           # Trace performance stats
  vscode-pyttd/                # VSCode extension
    src/
      extension.ts             # Extension activation
      debugAdapter/
        pyttdDebugSession.ts   # DAP session (all handlers)
        backendConnection.ts   # Python process + TCP management
      views/
        timelineScrubberProvider.ts
      providers/
        codeLensProvider.ts
        inlineValuesProvider.ts
        callHistoryProvider.ts
  tests/                       # Python tests
  samplecode/                  # Test scripts
  BUILD_PLAN.md                # Detailed implementation plan
```

---

## Development Status

The project is in **early development**. The existing Python prototype demonstrates tracing concepts but is being rebuilt with the C extension architecture described above.

Implementation is organized into 8 phases:

| Phase | Description | Status |
|---|---|---|
| 0 | Foundation cleanup, bug fixes, scaffolding | Not started |
| 1 | C extension recorder + ring buffer | Not started |
| 2 | Fork-based checkpointing | Not started |
| 3 | JSON-RPC server + debug adapter (first DAP connection) | Not started |
| 4 | Time-travel (step back, reverse continue, I/O hooks) | Not started |
| 5 | Timeline scrubber webview | Not started |
| 6 | CodeLens, inline values, call history tree | Not started |
| 7 | Polish, performance, packaging | Not started |

See [BUILD_PLAN.md](BUILD_PLAN.md) for the complete implementation plan with technical details, verification criteria, and sequencing.

---

## Performance Targets

| Metric | Target |
|---|---|
| Recording overhead (I/O-bound) | < 2x |
| Recording overhead (compute-bound) | < 5-10x |
| Step back/forward (warm) | < 10ms |
| Jump to frame (cold) | < 300ms |
| Timeline scrub | < 16ms (60fps) |
| DB size per frame | < 500 bytes |

---

## Known Limitations

1. **Variables are `repr()` snapshots** — recorded as strings, not live expandable objects in the Variables panel
2. **No expression evaluation in replay** — the Debug Console cannot evaluate arbitrary expressions; use the Variables panel
3. **C extension internals are opaque** — third-party C extension objects may have uninformative `repr()` output
4. **Windows: no cold navigation** — recording and warm browsing work, but checkpoint-based jumps require `fork()`
5. **Multi-threaded programs** — checkpoint correctness requires single-threaded state at fork time; macOS warns on fork with threads
6. **Async/await** — recorded as regular frames; no coroutine-specific visualization in v1
7. **Relative imports** — scripts using `from . import` must use `module` launch mode, not `program` mode
8. **Large locals truncated** — variable repr strings are capped at 256 bytes

---

## Contributing

Contributions are welcome. The project spans C, Python, and TypeScript — there are opportunities across the stack:

- **C extension** — recorder, ring buffer, checkpointing, replay engine
- **Python backend** — server, session management, query API, ORM models
- **VSCode extension** — DAP handlers, timeline webview, CodeLens, inline values
- **Tests** — across all layers
- **Documentation** — architecture docs, getting started guide

See [BUILD_PLAN.md](BUILD_PLAN.md) for implementation details and the current phase of work.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
