# Getting Started

This guide walks you through installing pyttd and making your first time-travel debug recording.

## Requirements

- **Python 3.12 or later** (3.13 recommended)
- **Linux or macOS** for full features (Windows: recording + warm navigation only)
- **VSCode** (optional, for the visual debugger experience)

## Installation

```bash
pip install pyttd
```

Or install from source:

```bash
git clone https://github.com/pyttd/pyttd.git
cd pyttd
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Verify:

```bash
pyttd --version
# pyttd 0.3.0
```

## Quick Start: CLI

### 1. Record a Script

Create a simple script (`example.py`):

```python
def greet(name):
    message = f"Hello, {name}!"
    print(message)
    return message

def main():
    names = ["Alice", "Bob", "Charlie"]
    for name in names:
        greet(name)

main()
```

Record it:

```bash
pyttd record example.py
```

This creates `example.pyttd.db` in the same directory as the script.

### 2. Query the Recording

```bash
pyttd query --last-run --frames
```

Output:

```
Run: abc123-...
  Script: example.py
  Frames: 42
  Duration: 0.01s

  seq=0    call    example.py:7    main
  seq=1    line    example.py:8    main         names = ['Alice', 'Bob', 'Charlie']
  seq=2    line    example.py:9    main         name = 'Alice'
  seq=3    call    example.py:1    greet
  seq=4    line    example.py:2    greet        name = 'Alice'
  ...
```

### 3. Replay to a Specific Frame

```bash
pyttd replay --last-run --goto-frame 10
```

## Quick Start: VSCode

### 1. Install the Extension

Install the `pyttd` extension from the VSCode marketplace, or install the `.vsix` file manually:

```
Extensions sidebar → ... → Install from VSIX...
```

### 2. Create a Launch Configuration

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

### 3. Start Debugging

1. Open a Python file
2. Set breakpoints (optional)
3. Press **F5** or select "Time-Travel Debug" from the Run menu
4. The script runs to completion (recording phase)
5. pyttd enters replay mode — you're now at the first line of your program

### 4. Navigate

- **Step forward**: F10 (step over), F11 (step into), Shift+F11 (step out)
- **Step backward**: click the step-back button in the debug toolbar
- **Continue/Reverse continue**: F5 forward, shift+F5 reverse (stops at breakpoints)
- **Jump to frame**: click in the Timeline scrubber (Debug sidebar)
- **Click CodeLens**: "TTD: N calls" above functions to jump to executions

## Key Concepts

### Post-Mortem Replay

Unlike traditional debuggers, pyttd does **not** pause your program during execution. Instead:

1. **Record** — your script runs to completion while pyttd captures every frame event
2. **Replay** — you navigate the recording freely: forward, backward, jump to any point

This means you always have the complete execution history available.

### Frame Events

pyttd records five types of events:

| Event | When |
|-------|------|
| `call` | Function entry |
| `line` | Line executed |
| `return` | Function exit |
| `exception` | Exception raised within a frame |
| `exception_unwind` | Frame exited via exception propagation |

Each event has a `sequence_no` (monotonically increasing), making every point in execution uniquely addressable.

### Warm vs Cold Navigation

- **Warm** (sub-ms) — reads from SQLite. Used for stepping (forward/backward) and continue
- **Cold** (50-300ms) — restores a fork-based checkpoint and fast-forwards. Used for large jumps via `goto_frame`

Step-back is always warm. You don't need to think about this distinction — pyttd chooses automatically.

### Variable Snapshots

Variables are captured as `repr()` strings at each `line` event. This means:
- You see the value of every local variable at every line
- Values are flat strings (not expandable objects)
- Custom `__repr__` methods are called during recording

### Multi-Thread Support

All Python threads are recorded with per-thread call stacks and globally ordered sequence numbers:
- `step_over` and `step_out` stay on the current thread
- `step_into` and `step_back` follow global sequence order across threads
- The Threads panel shows all threads seen during recording

### The `@ttdbg` Decorator

For quick function-level recording without the CLI:

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    # This function's execution is recorded
    x = compute_something()
    return x

my_function()  # Creates <this_file>.pyttd.db
```

## Next Steps

- [CLI Reference](cli-reference.md) — all commands and flags
- [VSCode Guide](vscode-guide.md) — full extension feature guide
- [API Reference](api-reference.md) — Python programmatic API
- [Architecture](architecture.md) — how pyttd works internally
- [FAQ](faq.md) — common questions
