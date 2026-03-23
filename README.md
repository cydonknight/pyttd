# pyttd

[![CI](https://github.com/cydonknight/pyttd/actions/workflows/ci.yml/badge.svg)](https://github.com/cydonknight/pyttd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**pyttd** (Python Time-Travel Debugger) is an open-source time-travel debugger for Python with full VSCode integration. It records complete program execution at the C level, then lets you step backward and forward, jump to any point in the trace, and visually scrub through a timeline — all from within your editor.

## What is Time-Travel Debugging?

Traditional debuggers only move forward. If you step past a bug, you start over. pyttd records every execution frame during a single run, then drops you into a **replay session** where you can navigate freely in both directions:

- **Step backward** through execution to see exactly how state evolved
- **Reverse continue** to find the last time a breakpoint was hit
- **Jump to any frame** in the entire recording
- **Scrub a visual timeline** to navigate through call depth, exceptions, and execution flow
- **Inspect variables** at any point without re-running the program
- **Track variable history** across the entire recording
- **Set conditional, function, data, and log breakpoints** — all work in both directions
- **Attach to running processes** to start recording mid-execution
- **Export traces** to Perfetto for external analysis

pyttd is a **post-mortem replay debugger** — your script runs to completion, and then you debug the recorded trace.

## Installation

> **Requires Python 3.12+** — uses CPython C API features introduced in 3.12.

```bash
pip install py-tt-debug
```

Or from source:

```bash
git clone https://github.com/cydonknight/pyttd.git
cd pyttd
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Quick Start

### CLI

```bash
# Record a script
pyttd record examples/hello.py

# Record with arguments
pyttd record examples/hello.py --args --verbose --count 5

# Record only specific functions
pyttd record examples/hello.py --include process_data --include validate

# Query the recording
pyttd query --last-run --frames

# List all runs in a database
pyttd query --list-runs --db hello.pyttd.db

# Replay and jump to a specific frame
pyttd replay --last-run --goto-frame 750

# Export to Perfetto trace format
pyttd export --db hello.pyttd.db -o trace.json
# Open trace.json at https://ui.perfetto.dev

# Clean up database files
pyttd clean --all --dry-run
```

### VSCode

1. Build the extension: `cd vscode-pyttd && npm install && npm run package`
2. In VSCode: Extensions sidebar → `...` → **Install from VSIX** → select `pyttd-*.vsix`
3. Add to `.vscode/launch.json`:

```json
{
    "type": "pyttd",
    "request": "launch",
    "name": "Time-Travel Debug",
    "program": "${file}"
}
```

4. Press **F5** — your script records, then you navigate freely

To replay an existing recording without re-running, use an `attach` configuration:

```json
{
    "type": "pyttd",
    "request": "attach",
    "name": "Replay Trace",
    "traceDb": "${workspaceFolder}/script.pyttd.db"
}
```

### Python API

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    x = 42
    return x * 2

my_function()  # Records to <file>.pyttd.db
```

Or with explicit start/stop control:

```python
from pyttd import start_recording, stop_recording

start_recording(db_path="trace.pyttd.db")
# ... your code ...
stats = stop_recording()
```

### Attach to a Running Process

```python
from pyttd import arm, disarm

# Start recording mid-execution
arm()
suspect_function()
stats = disarm()  # Returns recording stats

# Or use as a context manager
with arm():
    suspect_function()

# Or toggle via signal (kill -USR1 <pid>)
from pyttd import install_signal_handler
install_signal_handler()  # First USR1 arms, second disarms
```

## Features

### Recording
- C extension recorder using PEP 523 frame eval hooks (not `sys.settrace`)
- Lock-free per-thread SPSC ring buffers with background flush
- Fork-based checkpointing for fast cold navigation (Linux/macOS)
- Multi-thread recording with globally ordered sequence numbers
- Async/await and generator support with coroutine frame tracking
- I/O hooks for deterministic checkpoint replay — `time.time`, `time.monotonic`, `time.perf_counter`, `time.sleep`, `random.random`, `random.randint`, `random.uniform`, `random.gauss`, `random.choice`, `random.sample`, `random.shuffle`, `os.urandom`, `uuid.uuid4`, `uuid.uuid1`, `datetime.datetime.now`, `datetime.datetime.utcnow`
- Secrets filtering — sensitive variable names (`password`, `token`, `secret`, `api_key`, etc.) automatically redacted during recording; configurable patterns with `--secret-patterns`
- Selective function recording — `--include` / `--exclude` filter by function name, `--include-file` / `--exclude-file` filter by source file (glob patterns)
- Expandable variable trees — dicts, lists, tuples, sets, and objects with `__dict__` are serialized with structure, not just `repr()`
- Runtime attach — `arm()` / `disarm()` API to start/stop recording from within a running process, or toggle via Unix signal
- Checkpoint memory tracking and configurable limits

### Navigation
- Forward: step into/over/out, continue with breakpoints
- Reverse: step back, reverse continue with breakpoints and exception filters
- Conditional breakpoints — expressions evaluated against frame locals
- Hit-count breakpoints — stop after N hits (supports `>=N`, `>N`, `<=N`, `<N`, `==N`, `%N`)
- Log points — emit structured log messages with variable interpolation without stopping
- Function breakpoints — break on any call to a named function
- Data breakpoints — break when a variable's value changes
- Jump: goto frame, goto targets (all executions of a line), restart frame
- Warm navigation (SQLite, sub-ms) for stepping; cold navigation (checkpoint restore, 50-300ms) for jumps

### VSCode Extension
- Full DAP implementation with step-back and reverse-continue
- Canvas-based timeline scrubber with click/drag/zoom
- CodeLens annotations showing call and exception counts per function
- Inline variable values during stepping
- Call history tree with lazy-loaded nesting and exception markers
- Exception breakpoint filters (uncaught, all raised)
- Function breakpoints, data breakpoints, conditional breakpoints, hit conditions, log points
- Variable history tracking across the recording
- Breakpoint verification — validates that breakpoints target executable lines
- Status bar with recording progress (frame count, dropped frames, DB size)
- Keyboard shortcuts: Ctrl+Shift+F11 (step back), Ctrl+Shift+F5 (reverse continue)

### Analysis & Export
- Perfetto/Chrome Trace Event Format export — viewable in [ui.perfetto.dev](https://ui.perfetto.dev); preserves multi-thread structure
- Variable history queries — track how a variable changes over time, with deduplication
- Execution stats — per-function call counts, exception counts, and entry points
- Checkpoint memory profiling — per-checkpoint RSS tracking

### Database Management
- Multi-run storage — multiple recording runs in a single database
- Run eviction — `--keep-runs N` auto-evicts old runs; `pyttd clean --keep N` for manual cleanup
- Custom DB paths — `--db-path` overrides the default `<script>.pyttd.db` location
- Size monitoring — `--max-db-size` warns when the database exceeds a threshold
- Run selection — `--run-id` to query, replay, or export a specific run by UUID or prefix

## CLI Reference

```
pyttd [--version] [-v|--verbose] <command>

Commands:
  record    Record script execution
  query     Query trace data
  replay    Replay a recorded session
  serve     Start JSON-RPC debug server (used by VSCode)
  export    Export trace data
  clean     Clean up database files
```

### record

```bash
pyttd record script.py [options]

Options:
  --module                          Treat argument as a module name (e.g., pkg.mod)
  --checkpoint-interval N           Frames between checkpoints (default: 1000)
  --args VALUE [VALUE ...]          Arguments to pass to the script
  --no-redact                       Disable secrets redaction
  --secret-patterns PAT             Additional pattern to redact (repeatable)
  --include FUNC                    Only record functions matching this pattern (repeatable)
  --include-file GLOB               Only record functions in files matching this glob (repeatable)
  --exclude FUNC                    Exclude functions matching this pattern (repeatable)
  --exclude-file GLOB               Exclude files matching this glob (repeatable)
  --max-frames N                    Stop recording after N frames (0 = unlimited)
  --db-path PATH                    Custom database path (default: <script>.pyttd.db)
  --max-db-size MB                  Warn when DB exceeds this size in MB (0 = unlimited)
  --keep-runs N                     Keep only last N runs, evict older (0 = keep all)
  --checkpoint-memory-limit MB      Checkpoint memory limit in MB (0 = unlimited)
```

### query

```bash
pyttd query [--last-run | --run-id UUID] [--list-runs] [--frames] [--limit N] [--db path.pyttd.db]
```

### replay

```bash
pyttd replay [--last-run | --run-id UUID] --goto-frame N [--db path.pyttd.db]
```

### serve

```bash
# Record and serve (used by VSCode extension)
pyttd serve --script script.py [--module] [--cwd DIR] [--checkpoint-interval N]
    [--include FUNC] [--exclude FUNC] [--include-file GLOB] [--exclude-file GLOB]
    [--max-frames N] [--env KEY=VALUE ...] [--env-file .env]
    [--db-path PATH] [--max-db-size MB] [--keep-runs N]

# Replay existing recording (no re-recording)
pyttd serve --db path.pyttd.db [--run-id UUID]
```

### export

```bash
pyttd export --format perfetto --db path.pyttd.db [--run-id UUID] -o trace.json
```

### clean

```bash
pyttd clean [--db path.pyttd.db] [--all] [--keep N] [--dry-run]

Options:
  --db PATH      Specific database file to clean
  --all          Delete all .pyttd.db files in current directory
  --keep N       Keep last N runs, evict the rest
  --dry-run      Show what would be deleted without deleting
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `PYTTD_RECORDING` | Set to `1` during active recording; scripts can check `os.environ.get('PYTTD_RECORDING')` |
| `PYTTD_ARM_SIGNAL` | Auto-install signal handler on import — e.g., `PYTTD_ARM_SIGNAL=USR1` installs a SIGUSR1 toggle handler |

## Architecture

Three-layer system:

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| C Extension (`pyttd_native`) | C, CPython API | Frame recording, ring buffer, checkpoints, I/O hooks |
| Python Backend (`pyttd/`) | Python, Peewee, SQLite | JSON-RPC server, session navigation, query API |
| VSCode Extension (`vscode-pyttd/`) | TypeScript | DAP handlers, timeline webview, CodeLens, inline values |

See [docs/architecture.md](docs/architecture.md) for the full design.

## Platform Support

| Platform | Recording | Warm Navigation | Cold Navigation | Multi-Thread |
|----------|-----------|-----------------|-----------------|--------------|
| Linux | Full | Full | Full | Full |
| macOS | Full | Full | Partial* | Full |
| Windows | Full | Full | None | Full |

\* macOS: checkpoints skip when multiple threads are active.

## Requirements

- **Python >= 3.12** (required for `PyUnstable_InterpreterFrame_*` C API)
- **C compiler** (GCC, Clang, or MSVC)
- **VSCode** (for the extension; CLI works standalone)

## Known Limitations

- Expression evaluation operates on recorded snapshots, not live values
- C extension internals are opaque (third-party C extension objects may have uninformative `repr()`)
- Windows: no cold navigation (no `fork()`)
- `exception_unwind` line number is from function entry, not the exception site
- Variable repr strings are capped at 256 characters
- Expandable variable children are capped at 50 entries, 1 level deep
- Attach mode (`arm()`) disables checkpoints — cold navigation is unavailable for attached recordings
- Tight loops with per-line events have high overhead (hundreds of times slower); use `--include` to scope recording for compute-heavy code

## Testing

351 Python tests across 26 test modules + 95 VSCode extension (Mocha) tests:

```bash
# Run all Python tests
.venv/bin/pytest tests/ -v

# Run VSCode extension tests
cd vscode-pyttd && npm test

# Run overhead benchmarks
.venv/bin/python3 benchmarks/bench_overhead.py
```

Benchmarks cover I/O-bound, compute-bound, tight loop, deep recursion, many locals, and multi-thread workloads.

## Documentation

- **[Getting Started](docs/getting-started.md)** — first recording walkthrough
- **[CLI Reference](docs/cli-reference.md)** — all commands and flags
- **[VSCode Guide](docs/vscode-guide.md)** — extension features and configuration
- **[API Reference](docs/api-reference.md)** — Python programmatic API
- **[Architecture](docs/architecture.md)** — system design and data flow
- **[Troubleshooting](docs/troubleshooting.md)** — common issues
- **[FAQ](docs/faq.md)** — frequently asked questions
- **[Contributing](CONTRIBUTING.md)** — how to contribute
- **[Changelog](CHANGELOG.md)** — version history

Development guides: [Building](docs/development/building.md) | [Testing](docs/development/testing.md) | [C Extension](docs/development/c-extension.md) | [Protocol](docs/development/protocol.md) | [Releasing](docs/development/releasing.md)

## Contributing

Contributions welcome across C, Python, and TypeScript. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.
