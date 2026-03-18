# pyttd — Python Time-Travel Debugger

Record, replay, and step backward through Python program execution.

## Features

- **Record & Replay** — record your program once, then navigate freely forward and backward
- **Step Back** — step backward through execution to see how state evolved
- **Reverse Continue** — find the previous breakpoint or exception in reverse
- **Jump to Any Frame** — click the timeline or use goto-frame to jump anywhere in the recording
- **Timeline Scrubber** — visual canvas-based timeline showing call depth, exceptions, and breakpoints
- **CodeLens Annotations** — call counts and exception counts displayed above traced functions
- **Inline Variable Values** — variable values shown inline in the editor during stepping
- **Call History Tree** — expandable call hierarchy with exception markers in the Debug sidebar
- **Multi-Thread** — all Python threads recorded with per-thread call stacks

## Requirements

- Python 3.12 or later
- `pyttd` Python package installed: `pip install pyttd`
- Linux or macOS for full features (Windows: recording + warm navigation only)

## Getting Started

1. Install the `pyttd` Python package: `pip install pyttd`
2. Open a Python file in VSCode
3. Add a launch configuration (see below)
4. Press **F5** — your program runs to completion, then you navigate the recording

## Launch Configuration

Add to `.vscode/launch.json`:

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "type": "pyttd",
            "request": "launch",
            "name": "Time-Travel Debug",
            "program": "${file}"
        }
    ]
}
```

### Configuration Options

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `program` | string | — | Path to Python script to debug |
| `module` | string | — | Python module name (dotted path) |
| `pythonPath` | string | Auto-detected | Path to Python interpreter |
| `cwd` | string | Workspace root | Working directory |
| `args` | string[] | `[]` | Command-line arguments for the script |
| `traceDb` | string | — | Path to existing `.pyttd.db` for replay-only (no recording) |
| `checkpointInterval` | number | 1000 | Frames between checkpoints (0 to disable) |

## Navigation

### Forward
- **Step Over** (F10) — next line, same depth
- **Step Into** (F11) — next line, any depth
- **Step Out** (Shift+F11) — next line after current function returns
- **Continue** (F5) — forward to next breakpoint or end

### Reverse
- **Step Back** — previous line event (always fast)
- **Reverse Continue** — backward to previous breakpoint or start

### Jump
- **Timeline click** — jump to any point in the recording
- **Timeline drag** — scrub through the recording
- **Timeline zoom** — scroll wheel to zoom in/out
- **Goto Targets** — find all executions of a specific line
- **Restart Frame** — jump to function entry

### Timeline Keyboard Shortcuts
- Arrow keys — step forward/backward
- Home/End — jump to start/end
- PageUp/PageDown — zoom in/out

## Exception Breakpoints

| Filter | Default | Description |
|--------|---------|-------------|
| Uncaught Exceptions | Enabled | Stop on exceptions propagating out of recorded code |
| All Exceptions | Disabled | Stop on every raised exception |

## Multi-Thread Support

All Python threads are recorded with globally ordered sequence numbers:
- Step Over and Step Out stay on the current thread
- Step Into and Step Back follow global sequence order
- The Threads panel shows all recorded threads

## Known Limitations

- Variable values are `repr()` strings — not expandable object trees
- Expression evaluation operates on recorded snapshots
- Cold navigation (goto-frame jumps) unavailable on Windows
- macOS: checkpoints skip when multiple threads are active
- `exception_unwind` line number shows function entry, not exception site

## More Information

- [Documentation](https://github.com/pyttd/pyttd/tree/main/docs)
- [Changelog](https://github.com/pyttd/pyttd/blob/main/CHANGELOG.md)
- [Contributing](https://github.com/pyttd/pyttd/blob/main/CONTRIBUTING.md)
- [Issues](https://github.com/pyttd/pyttd/issues)
