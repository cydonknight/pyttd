# API Reference

This documents pyttd's public Python API for programmatic use.

## `@ttdbg` Decorator

The simplest way to record a function's execution.

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    x = 42
    return x * 2

my_function()  # Records execution to <source_file>.pyttd.db
```

The decorator:
- Creates a `.pyttd.db` file in the source file's directory
- Deletes any existing DB (and WAL/SHM companion files) before recording
- Disables checkpoints (`checkpoint_interval=0`)
- Starts/stops the C extension recorder around the function call

## `PyttdConfig`

Configuration dataclass for the recorder.

```python
from pyttd.config import PyttdConfig

config = PyttdConfig(
    checkpoint_interval=1000,    # Frames between checkpoints (0 = disabled)
    ring_buffer_size=65536,      # Ring buffer capacity (0 = default, min 64)
    flush_interval_ms=10,        # Flush thread wakeup interval in ms
    ignore_patterns=[],          # Additional patterns to ignore
    db_path=None,                # Optional DB path override
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `checkpoint_interval` | `int` | `1000` | Frames between fork-based checkpoints. 0 disables |
| `ring_buffer_size` | `int` | `65536` | Ring buffer slot count. Must be 0 (default) or >= 64. Power-of-2 internally |
| `flush_interval_ms` | `int` | `10` | How often the flush thread wakes to drain the ring buffer |
| `ignore_patterns` | `list[str]` | `[]` | Additional file/directory patterns to exclude from recording |
| `db_path` | `str \| None` | `None` | Override the default DB path (`<script>.pyttd.db`) |

## `Recorder`

Manages the recording lifecycle. Wraps the C extension.

```python
from pyttd.recorder import Recorder
from pyttd.config import PyttdConfig

recorder = Recorder(PyttdConfig())
recorder.start(db_path="trace.pyttd.db", script_path="my_app.py")

# ... execute user code ...

stats = recorder.stop()
# stats = {'frame_count': 12345, 'dropped_frames': 0, 'elapsed_time': 2.1, ...}

recorder.kill_checkpoints()  # Send DIE to all checkpoint children
recorder.cleanup()           # Close DB connection
```

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `start(db_path, script_path=None)` | `None` | Initialize DB, create Run record, start C recorder and flush thread |
| `stop()` | `dict` | Stop recording, update Run record, return stats |
| `kill_checkpoints()` | `None` | Send DIE command to all checkpoint children, update DB |
| `cleanup()` | `None` | Kill checkpoints and close DB connection |

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `run_id` | `UUID` | The current run's unique identifier |

### Callbacks (internal, called by C extension)

| Callback | Description |
|----------|-------------|
| `_on_flush(events)` | Batch-insert frame event dicts into SQLite |
| `_on_io_event(event)` | Insert a single I/O event |
| `_on_checkpoint(child_pid, sequence_no)` | Create Checkpoint DB record |
| `_load_io_events_for_replay(after_seq)` | Load IOEvents for checkpoint child replay |

## `Session`

Navigation state machine for replay mode.

```python
from pyttd.session import Session

session = Session()
session.enter_replay(run_id, first_line_seq)
```

### Navigation Methods

All navigation methods return a result dict:

```python
{
    "seq": 42,                   # Sequence number of current position
    "file": "my_app.py",        # Source filename
    "line": 10,                  # Line number
    "function_name": "greet",   # Function name
    "call_depth": 1,            # Nesting depth (0 = top-level)
    "thread_id": 12345,         # OS thread ID
    "reason": "step",           # Why we stopped: "step", "breakpoint", "exception", "start", "end", "goto"
}
```

| Method | Description |
|--------|-------------|
| `step_into()` | Next `line` event (any thread, any depth) |
| `step_over()` | Next `line` event at depth <= current (same thread) |
| `step_out()` | Next `line` event after current function returns (same thread) |
| `continue_forward()` | Next breakpoint or exception filter match, or end of recording |
| `step_back()` | Previous `line` event (always warm, no checkpoint needed) |
| `reverse_continue()` | Previous breakpoint or exception filter match, or start of recording |
| `goto_frame(target_seq)` | Jump to any frame by sequence number. Uses cold navigation for large jumps |
| `goto_targets(filename, line)` | All executions at a file:line (capped at 1000 results) |
| `restart_frame(frame_seq)` | Jump to the first line event of the function containing `frame_seq` |

### State Methods

| Method | Description |
|--------|-------------|
| `enter_replay(run_id, first_line_seq)` | Enter replay mode, build initial stack |
| `set_breakpoints(breakpoints)` | Set line breakpoints. Each: `{"file": str, "line": int}` |
| `set_exception_filters(filters)` | Set exception filters: `["raised"]`, `["uncaught"]`, or both |
| `get_threads()` | List of `{"id": int, "name": str}` for all threads seen during recording |

### Query Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `get_stack_at(seq)` | `list[dict]` | Stack frames at a position. Each: `{"seq", "name", "file", "line", "depth"}` |
| `get_variables_at(seq)` | `list[dict]` | Variables at a position. Each: `{"name", "value", "type"}` |
| `evaluate_at(seq, expr, context)` | `dict` | Evaluate expression. Context: `"hover"`, `"watch"`, `"repl"` |
| `get_traced_files()` | `list[str]` | Distinct filenames from the recording |
| `get_execution_stats(filename)` | `list[dict]` | Per-function stats: `{"function_name", "line_no", "call_count", "exception_count", "firstCallSeq"}` |
| `get_call_children(parent_call_seq, parent_return_seq)` | `list[dict]` | Call tree children for lazy loading |

## `ReplayController`

Handles warm (SQLite) and cold (checkpoint) navigation.

```python
from pyttd.replay import ReplayController

replay = ReplayController()

# Warm navigation (sub-ms, reads from SQLite)
result = replay.warm_goto_frame(run_id, target_seq)

# Cold navigation (50-300ms, restores checkpoint + fast-forward)
result = replay.goto_frame(run_id, target_seq)
```

| Method | Description |
|--------|-------------|
| `goto_frame(run_id, target_seq)` | Try cold (checkpoint), fall back to warm |
| `warm_goto_frame(run_id, target_seq)` | Warm-only: read from SQLite. Returns dict with `warm_only=True` |

## Query Functions

Standalone query functions in `pyttd.query`.

```python
from pyttd.query import get_last_run, get_frames, get_frame_at_seq, get_line_code

run = get_last_run(db_path)           # Returns Runs model instance
frames = get_frames(run_id, limit=50) # Returns list of ExecutionFrames
frame = get_frame_at_seq(run_id, 42)  # Returns single ExecutionFrames or None
source = get_line_code("app.py", 10)  # Returns source line via linecache
```

## Timeline Query

```python
from pyttd.models.timeline import get_timeline_summary

buckets = get_timeline_summary(
    run_id=run_id,
    start_seq=0,
    end_seq=10000,
    bucket_count=500,
    breakpoints=[{"file": "app.py", "line": 10}],
)
# Returns list of dicts:
# {"startSeq", "endSeq", "maxCallDepth", "hasException", "hasBreakpoint", "dominantFunction"}
```

## Database Models

All models use Peewee ORM with a deferred database (`SqliteDatabase(None)` initialized at runtime).

### `Runs`

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | `UUIDField` (PK) | Unique run identifier |
| `script_path` | `TextField` (nullable) | Path to recorded script |
| `timestamp_start` | `FloatField` | Wall-clock epoch seconds at start |
| `timestamp_end` | `FloatField` (nullable) | Wall-clock epoch seconds at end |
| `total_frames` | `IntegerField` | Total frame events recorded |

### `ExecutionFrames`

| Field | Type | Description |
|-------|------|-------------|
| `frame_id` | `AutoField` (PK) | Auto-increment ID |
| `run_id` | `UUIDField` (FK) | Run this frame belongs to |
| `sequence_no` | `IntegerField` | Monotonic event counter (0-based) |
| `timestamp` | `FloatField` | Monotonic seconds since recording start |
| `line_no` | `IntegerField` | Source line number |
| `filename` | `TextField` | Source file path |
| `function_name` | `TextField` | Function or method name |
| `frame_event` | `TextField` | Event type: `call`, `line`, `return`, `exception`, `exception_unwind` |
| `call_depth` | `IntegerField` | Nesting depth (0 = top-level) |
| `locals_snapshot` | `TextField` | JSON string of `repr()` variable snapshots |
| `thread_id` | `BigIntegerField` | OS thread identifier |

Indexes: `(run_id, sequence_no)` UNIQUE, `(run_id, filename, line_no)`, `(run_id, function_name)`, `(run_id, frame_event, sequence_no)`, `(run_id, call_depth, sequence_no)`, `(run_id, thread_id, sequence_no)`.

### `Checkpoint`

| Field | Type | Description |
|-------|------|-------------|
| `checkpoint_id` | `AutoField` (PK) | Auto-increment ID |
| `run_id` | `UUIDField` (FK) | Run this checkpoint belongs to |
| `sequence_no` | `IntegerField` | Frame sequence at checkpoint creation |
| `child_pid` | `IntegerField` (nullable) | Forked child PID |
| `is_alive` | `BooleanField` | Whether child process is still running |

### `IOEvent`

| Field | Type | Description |
|-------|------|-------------|
| `io_event_id` | `AutoField` (PK) | Auto-increment ID |
| `run_id` | `UUIDField` (FK) | Run this event belongs to |
| `sequence_no` | `IntegerField` | Frame sequence when I/O occurred |
| `io_sequence` | `IntegerField` | Order within same sequence_no |
| `function_name` | `TextField` | Hooked function (e.g., `time.time`) |
| `return_value` | `BlobField` | Type-specific serialized return value |

## Storage Utilities

```python
from pyttd.models import storage

storage.connect_to_db(path)       # Initialize deferred DB, set pragmas (WAL, busy_timeout)
storage.close_db()                # Close DB connection
storage.delete_db_files(path)     # Delete .pyttd.db and companion -wal/-shm files
storage.initialize_schema(models) # Create tables (safe=True)
```

## Error Hierarchy

```python
from pyttd.errors import (
    PyttdError,          # Base exception
    RecordingError,      # Recording failures
    ReplayError,         # Replay/navigation failures
    CheckpointError,     # Checkpoint/fork failures
    ServerError,         # JSON-RPC server failures
    NoForkError,         # Platform doesn't support fork()
)
```

## Environment Variables

| Variable | Set By | Description |
|----------|--------|-------------|
| `PYTTD_RECORDING` | C extension | `"1"` during recording, cleared after stop |

## See Also

- [CLI Reference](cli-reference.md) — command-line interface
- [Architecture](architecture.md) — system design and data flow
- [Protocol Reference](development/protocol.md) — JSON-RPC method documentation
