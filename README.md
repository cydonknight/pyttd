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

pyttd is a **post-mortem replay debugger** — your script runs to completion, and then you debug the recorded trace.

## Installation

> **Requires Python 3.12+** — uses CPython C API features introduced in 3.12.

```bash
pip install pyttd
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
# Record the included example
pyttd record examples/hello.py

# Query the recording
pyttd query --last-run --frames

# Replay and jump to a specific frame
pyttd replay --last-run --goto-frame 750
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

### Navigation
- Forward: step into/over/out, continue with breakpoints
- Reverse: step back, reverse continue with breakpoints and exception filters
- Jump: goto frame, goto targets (all executions of a line), restart frame
- Warm navigation (SQLite, sub-ms) for stepping; cold navigation (checkpoint restore, 50-300ms) for jumps

### VSCode Extension
- Full DAP implementation with step-back and reverse-continue
- Canvas-based timeline scrubber with click/drag/zoom
- CodeLens annotations showing call and exception counts per function
- Inline variable values during stepping
- Call history tree with lazy-loaded nesting and exception markers
- Exception breakpoint filters (uncaught, all raised)

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

- Variables are `repr()` snapshots — flat strings, not expandable objects
- Expression evaluation operates on recorded snapshots, not live values
- C extension internals are opaque (third-party C extension objects may have uninformative `repr()`)
- Windows: no cold navigation (no `fork()`)
- `exception_unwind` line number is from function entry, not the exception site
- Variable repr strings are capped at 256 characters

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
