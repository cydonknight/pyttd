# CLI Reference

pyttd provides a command-line interface for recording, querying, replaying, and serving debug sessions.

## Global Options

```
pyttd [--version] [-v|--verbose] SUBCOMMAND [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--version` | Print version and exit |
| `-v`, `--verbose` | Enable debug logging (sets log level to DEBUG) |

## `pyttd record`

Record a script's execution to a trace database.

```
pyttd record SCRIPT [--module] [--checkpoint-interval N] [--args ...]
```

| Argument/Flag | Description | Default |
|---------------|-------------|---------|
| `SCRIPT` | Path to Python script, or module name with `--module` | Required |
| `--module` | Treat SCRIPT as a module name (dotted path) | Off |
| `--checkpoint-interval N` | Frames between fork-based checkpoints. 0 disables checkpoints | 1000 |
| `--args ...` | Arguments passed to the recorded script | None |

The script path is validated before recording starts. The trace database is created at `<script_name>.pyttd.db` in the script's directory.

During recording, the environment variable `PYTTD_RECORDING=1` is set. User scripts can check `os.environ.get('PYTTD_RECORDING')` to detect recording mode.

### Examples

```bash
# Record a script
pyttd record my_app.py

# Record with verbose logging
pyttd -v record my_app.py

# Record a module
pyttd record --module mypackage.main

# Record with arguments passed to the script
pyttd record my_app.py --args --port 8080 --debug

# Record without checkpoints (faster, no cold navigation)
pyttd record my_app.py --checkpoint-interval 0

# Record with more frequent checkpoints (more memory, faster jumps)
pyttd record my_app.py --checkpoint-interval 500
```

### Output

After recording completes, pyttd prints statistics:

```
Recording complete: 12,345 frames in 2.1s
  Dropped frames: 0
  Checkpoints: 12
  DB size: 4.2 MB
```

## `pyttd query`

Query a recorded trace database.

```
pyttd query [--last-run] [--frames] [--limit N] [--db PATH]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--last-run` | Query the most recent recording | Off |
| `--frames` | Display frame details with source lines | Off |
| `--limit N` | Maximum number of frames to display | 50 |
| `--db PATH` | Path to `.pyttd.db` file | Auto-detected |

If `--db` is not specified, pyttd looks for `.pyttd.db` files in the current directory.

### Examples

```bash
# Show last run summary
pyttd query --last-run

# Show frames from last run
pyttd query --last-run --frames

# Show first 100 frames
pyttd query --last-run --frames --limit 100

# Query a specific database
pyttd query --db app.pyttd.db --last-run --frames
```

### Output

```
Run: abc123-...
  Script: my_app.py
  Frames: 12,345
  Duration: 2.1s

  seq=0    call    my_app.py:1    <module>
  seq=1    line    my_app.py:3    <module>       x = 42
  seq=2    call    my_app.py:5    greet
  seq=3    line    my_app.py:6    greet          name = 'world'
  ...
```

## `pyttd replay`

Replay a recorded session (warm navigation only, no checkpoints).

```
pyttd replay [--last-run] [--goto-frame N] [--db PATH]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--last-run` | Use the most recent recording | Off |
| `--goto-frame N` | Jump to a specific frame by sequence number | 0 |
| `--db PATH` | Path to `.pyttd.db` file | Auto-detected |

### Examples

```bash
# Replay last run, jump to frame 750
pyttd replay --last-run --goto-frame 750

# Replay from a specific database
pyttd replay --db app.pyttd.db --last-run --goto-frame 100
```

## `pyttd serve`

Start a JSON-RPC debug server over TCP. Used by the VSCode extension — typically not invoked directly.

```
pyttd serve (--script SCRIPT | --db PATH) [--module] [--cwd DIR] [--checkpoint-interval N]
```

| Flag | Description | Default |
|------|-------------|---------|
| `--script SCRIPT` | Script to record and debug | Mutually exclusive with `--db` |
| `--db PATH` | Existing `.pyttd.db` for replay-only mode | Mutually exclusive with `--script` |
| `--module` | Treat script as module name | Off |
| `--cwd DIR` | Working directory | `.` |
| `--checkpoint-interval N` | Frames between checkpoints | 1000 |

Exactly one of `--script` or `--db` is required. With `--script`, the server records the script and enters replay mode after completion. With `--db`, the server opens an existing trace database in replay-only mode (no recording phase).

### Port Handshake

The server binds to `127.0.0.1:0` (OS-assigned port) and writes to stdout:

```
PYTTD_PORT:<port>
```

The VSCode extension reads this line to discover the port, then connects via TCP for JSON-RPC communication.

### Examples

```bash
# Start server for recording + replay
pyttd serve --script my_app.py

# Start server in replay-only mode
pyttd serve --db my_app.pyttd.db

# Start server for a module
pyttd serve --script mypackage.main --module

# Start server with custom working directory
pyttd serve --script my_app.py --cwd /path/to/project
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PYTTD_RECORDING` | Set to `"1"` during recording, cleared after stop. User scripts can check this to detect recording mode |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (invalid arguments, script not found, recording failure) |

## See Also

- [Getting Started](getting-started.md) — first recording walkthrough
- [API Reference](api-reference.md) — Python programmatic API
- [VSCode Guide](vscode-guide.md) — using pyttd from VSCode
