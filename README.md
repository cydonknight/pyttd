# pyttd

[![CI](https://github.com/pyttd/pyttd/actions/workflows/ci.yml/badge.svg)](https://github.com/pyttd/pyttd/actions/workflows/ci.yml)
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
- **Export traces** to Perfetto for external analysis

pyttd is a **post-mortem replay debugger** — your script runs to completion, and then you debug the recorded trace.

## Installation

> **Requires Python 3.12+** — uses CPython C API features introduced in 3.12.

```bash
pip install py-tt-debug
```

Or from source:

```bash
git clone https://github.com/pyttd/pyttd.git
cd pyttd
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Quick Start

### CLI

```bash
# Record a script
pyttd record examples/hello.py

# Query the recording
pyttd query --last-run --frames

# Replay and jump to a specific frame
pyttd replay --last-run --goto-frame 750

# Export to Perfetto trace format
pyttd export --db hello.pyttd.db -o trace.json
# Open trace.json at https://ui.perfetto.dev
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

### Python API

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    x = 42
    return x * 2

my_function()  # Records to <file>.pyttd.db
```

## Features

### Recording
- C extension recorder using PEP 523 frame eval hooks (not `sys.settrace`)
- Lock-free per-thread SPSC ring buffers with background flush
- Fork-based checkpointing for fast cold navigation (Linux/macOS)
- Multi-thread recording with globally ordered sequence numbers
- I/O hooks for deterministic checkpoint replay (`time.time`, `random.random`, `os.urandom`)
- Secrets filtering — sensitive variable names (`password`, `token`, `secret`, `api_key`, etc.) automatically redacted during recording
- Selective function recording — `--include` flag records only matching functions
- Expandable variable trees — dicts, lists, tuples, sets, and objects with `__dict__` are serialized with structure, not just `repr()`

### Navigation
- Forward: step into/over/out, continue with breakpoints
- Reverse: step back, reverse continue with breakpoints and exception filters
- Conditional breakpoints — expressions evaluated against frame locals
- Jump: goto frame, goto targets (all executions of a line), restart frame
- Warm navigation (SQLite, sub-ms) for stepping; cold navigation (checkpoint restore, 50-300ms) for jumps

### VSCode Extension
- Full DAP implementation with step-back and reverse-continue
- Canvas-based timeline scrubber with click/drag/zoom
- CodeLens annotations showing call and exception counts per function
- Inline variable values during stepping
- Call history tree with lazy-loaded nesting and exception markers
- Exception breakpoint filters (uncaught, all raised)
- Variable history tracking across the recording

### Analysis & Export
- Perfetto/Chrome Trace Event Format export — viewable in [ui.perfetto.dev](https://ui.perfetto.dev)
- Variable history queries — track how a variable changes over time, with deduplication
- Execution stats — per-function call counts, exception counts, and entry points

## CLI Reference

```
pyttd [--version] [-v|--verbose] <command>

Commands:
  record    Record script execution
  query     Query trace data
  replay    Replay a recorded session
  serve     Start JSON-RPC debug server (used by VSCode)
  export    Export trace data
```

### record

```bash
pyttd record script.py [options]

Options:
  --module                    Treat argument as a module name (e.g., pkg.mod)
  --checkpoint-interval N     Frames between checkpoints (default: 1000)
  --args VALUE [VALUE ...]    Arguments to pass to the script
  --no-redact                 Disable secrets redaction
  --secret-patterns PAT ...   Additional patterns to redact (added to defaults)
  --include FUNC [FUNC ...]   Only record functions matching these substrings
```

### query

```bash
pyttd query --last-run [--frames] [--limit N] [--db path.pyttd.db]
```

### replay

```bash
pyttd replay --last-run --goto-frame N [--db path.pyttd.db]
```

### serve

```bash
# Record and serve (used by VSCode extension)
pyttd serve --script script.py [--module] [--cwd DIR] [--checkpoint-interval N] [--include FUNC ...]

# Replay existing recording (no re-recording)
pyttd serve --db path.pyttd.db
```

### export

```bash
pyttd export --format perfetto --db path.pyttd.db -o trace.json
```

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

## Testing

207 tests across 20 test modules:

```bash
# Run all tests
.venv/bin/pytest tests/ -v

# Run benchmarks
.venv/bin/pytest benchmarks/ --benchmark-compare
```

Benchmarks cover tight loops, deep recursion, many locals, and multi-thread workloads.

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
