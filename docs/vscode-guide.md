# VSCode Extension Guide

The pyttd VSCode extension provides a full visual time-travel debugging experience.

## Installation

Install from the VSCode marketplace by searching for "pyttd", or install a `.vsix` file:

```
Extensions sidebar → ... → Install from VSIX...
```

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
| `program` | `string` | — | Path to Python script to debug |
| `module` | `string` | — | Python module name (dotted path, e.g., `mypackage.main`) |
| `pythonPath` | `string` | Auto-detected | Path to Python interpreter. Auto-detects from venv or PATH |
| `cwd` | `string` | Workspace root | Working directory for the script |
| `args` | `string[]` | `[]` | Command-line arguments passed to the script |
| `traceDb` | `string` | — | Path to existing `.pyttd.db` file for replay-only mode (no recording) |
| `checkpointInterval` | `number` | `1000` | Frames between fork-based checkpoints. 0 disables |

### Examples

```json
// Debug a specific script with arguments
{
    "type": "pyttd",
    "request": "launch",
    "name": "Debug App",
    "program": "src/main.py",
    "args": ["--port", "8080"],
    "cwd": "${workspaceFolder}"
}

// Debug a module
{
    "type": "pyttd",
    "request": "launch",
    "name": "Debug Module",
    "module": "mypackage.cli",
    "cwd": "${workspaceFolder}"
}

// Replay an existing recording (no re-recording)
{
    "type": "pyttd",
    "request": "launch",
    "name": "Replay Recording",
    "traceDb": "output.pyttd.db"
}

// Frequent checkpoints for faster jumps (more memory)
{
    "type": "pyttd",
    "request": "launch",
    "name": "Debug (Fast Jumps)",
    "program": "${file}",
    "checkpointInterval": 200
}
```

## Debug Session Flow

1. **Press F5** — pyttd spawns a Python backend server
2. **Recording phase** — your script runs to completion. A progress indicator shows frame count
3. **Replay mode** — the debugger stops at the first line of your program. You can now navigate freely

The Debug Console shows your script's stdout/stderr output during recording.

## Navigation

### Forward Navigation

| Action | Shortcut | Description |
|--------|----------|-------------|
| Step Over | F10 | Next line event (same depth, same thread) |
| Step Into | F11 | Next line event (any depth, any thread) |
| Step Out | Shift+F11 | Next line event after current function returns (same thread) |
| Continue | F5 | Forward to next breakpoint/exception, or end of recording |

### Reverse Navigation

| Action | Description |
|--------|-------------|
| Step Back | Previous line event (always fast, reads from SQLite) |
| Reverse Continue | Backward to previous breakpoint/exception, or start of recording |

Step back and reverse continue are available in the debug toolbar.

### Jump Navigation

| Action | Description |
|--------|-------------|
| Goto Frame | Click in the Timeline scrubber to jump to any point |
| Goto Targets | Right-click a line → "Go to Target" to find all executions of that line |
| Restart Frame | Right-click a stack frame → "Restart Frame" to jump to the function's entry |

## Features

### Timeline Scrubber

The Timeline panel appears in the Debug sidebar during replay. It shows a visual overview of the entire recording:

- **Vertical bars** — height represents call depth at each point
- **Colors** — blue (normal), red (exception), orange (breakpoint)
- **Yellow cursor** — current position with triangle marker

**Interaction:**
- **Click** — jump to that point in the recording
- **Drag** — scrub through the recording (visual updates + navigation on release)
- **Scroll wheel** — zoom in/out (re-fetches timeline data for the visible range)
- **Arrow keys** — step forward/backward
- **Home/End** — jump to start/end of recording
- **PageUp/PageDown** — zoom in/out by 10%

### CodeLens Annotations

Above each traced function, pyttd shows annotations like:

```
TTD: 3 calls | 1 exception
def process_item(item):
    ...
```

Click the annotation to navigate to the first execution of that function.

### Inline Variable Values

During stepping, variable values appear inline next to the source code, showing the value at the current replay position.

### Variable History Webview

Right-click a variable in the Variables panel → "Show History" to open the Variable History panel. Numeric values are plotted as a canvas chart; non-numeric values are shown as a table. Click any point to navigate to the frame where that change happened.

### Live Pause

During recording, click the Pause button (or Ctrl+Shift+F6) to suspend execution at the next line boundary. While paused you can:
- Inspect current variables
- Step backward through recorded history (without resuming)
- Set or modify breakpoints
- Edit variable values in the Variables panel (changes take effect on resume)
- Resume recording from the live point, OR "Resume from Past" — navigate backward and press Continue to fork execution from a historical checkpoint onto a new timeline branch

### Call History Tree

The Call History panel in the Debug sidebar shows the call hierarchy:

```
▶ main()                    [seq 0]
  ▶ process_items()         [seq 5]
    ▶ process_item("a")     [seq 8]
    ▶ process_item("b")     [seq 15]
      ⚠ process_item("b")  [exception]
  ▶ cleanup()               [seq 25]
```

- Click any entry to navigate to that call
- Nodes are lazy-loaded (expand to see children)
- Exception icons mark calls that raised exceptions

### Breakpoints

Set breakpoints normally by clicking the gutter. They work in both forward and reverse navigation:

- **Continue** (F5) — stops at the next breakpoint ahead
- **Reverse Continue** — stops at the previous breakpoint behind

### Exception Breakpoints

Configure via the Breakpoints panel:

| Filter | Default | Description |
|--------|---------|-------------|
| Uncaught Exceptions | Enabled | Stop on exceptions that propagate out of the recorded code |
| All Raised Exceptions | Disabled | Stop on every `raise` statement |

### Function, Data, Conditional, Hit-Count, Log Breakpoints

Full DAP breakpoint feature set is supported:

- **Function breakpoints** — break on any call to a named function
- **Data breakpoints** — break when a named variable's value changes (right-click a variable → Break on Value Change)
- **Conditional breakpoints** — expressions evaluated against frame locals in a restricted sandbox. Condition evaluation errors appear in the Debug Console
- **Hit-count breakpoints** — stop after N hits (supports `>=N`, `>N`, `<=N`, `<N`, `==N`, `%N`)
- **Log points** — emit a log message on hit without stopping (curly-brace interpolation: `{var_name}`)

### Threads Panel

Shows all Python threads seen during the recording. The active thread is highlighted. Thread-aware navigation (`step_over`, `step_out`) stays on the selected thread.

## Multi-Thread Debugging

When debugging multi-threaded programs:

- All threads are recorded with globally ordered sequence numbers
- The Threads panel lists all threads by OS thread ID
- `step_over` and `step_out` stay on the current thread
- `step_into` and `step_back` cross thread boundaries (follow global order)
- The Timeline scrubber shows events from all threads

## Tips

- **Use `traceDb`** to replay a recording without re-running the script — useful for sharing recordings or debugging intermittent issues
- **Increase `checkpointInterval`** to reduce memory usage (fewer fork snapshots), at the cost of slower jumps
- **Decrease `checkpointInterval`** (e.g., 200) for faster jump-to-frame in large recordings
- **Set breakpoints before recording** — they're used during replay navigation, not recording
- **Check the Debug Console** for script output during recording
- **Use `PYTTD_RECORDING=1`** in your script to skip expensive operations during recording

## Known Limitations

- Containers (dict/list/tuple/set, objects with `__dict__` / `__slots__`, NamedTuple, `@dataclass`) are expandable; primitives are flat repr strings
- Expression evaluation operates on recorded snapshots by default; live evaluation is available only at a pause boundary (live debugging)
- Cold navigation (goto-frame) is unavailable on Windows (no `fork()`)
- macOS: checkpoints skip when multiple threads are active (fork limitation; see troubleshooting)
- Attach-mode (`arm()`) recordings default to warm-only navigation; pass `arm(checkpoints=True)` for cold nav on the live tail

## See Also

- [Getting Started](getting-started.md) — first recording walkthrough
- [CLI Reference](cli-reference.md) — command-line usage
- [Architecture](architecture.md) — how warm vs cold navigation works
- [Troubleshooting](troubleshooting.md) — common issues
- [FAQ](faq.md) — frequently asked questions
