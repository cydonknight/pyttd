# API Reference

This documents pyttd's public Python API for programmatic use.

## Public API

The following are exported from `pyttd` and are the supported entry points.

```python
from pyttd import (
    ttdbg,                    # Decorator for function-level recording
    start_recording,          # Start a recording session
    stop_recording,           # Stop and return stats
    arm,                      # Start recording from within a running process
    disarm,                   # Stop an arm()-started recording
    install_signal_handler,   # Signal-based arm/disarm toggle
)
```

### `@ttdbg` decorator

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    x = 42
    return x * 2

my_function()  # Records execution to <source_file>.pyttd.db
```

Behavior:
- Creates a `.pyttd.db` file in the source file's directory
- Deletes any existing DB (and WAL/SHM companions) before recording
- Disables checkpoints (`checkpoint_interval=0`)
- Starts/stops the C recorder around the function call

### `start_recording` / `stop_recording`

Imperative start/stop for programmatic control.

```python
from pyttd import start_recording, stop_recording

start_recording(db_path="trace.pyttd.db")
my_function()            # Only function *calls* made here are recorded
stats = stop_recording() # dict: frame_count, elapsed_time, dropped_frames, warnings...
```

**Limitation:** only function calls are traced. Inline code in the caller's scope is not recorded. Use `arm()` / `disarm()` if you need to record inline code.

| Function | Signature | Description |
|----------|-----------|-------------|
| `start_recording(db_path=None, **kwargs)` | db_path optional (default: `<caller>.pyttd.db`); kwargs forwarded to `PyttdConfig` | Begin recording. Raises `RuntimeError` if already active. |
| `stop_recording()` | `() -> dict` | Stop recording and return stats. Raises `RuntimeError` if no active recording. |

### `arm()` / `disarm()`

Record a region of code inside an already-running process. Unlike `start_recording`, this captures inline code via stack synthesis.

```python
from pyttd import arm, disarm

arm()
suspect_function()
stats = disarm()

# As a context manager
with arm() as ctx:
    suspect_function()
print(ctx.stats)
```

| Function | Signature | Description |
|----------|-----------|-------------|
| `arm(db_path=None, *, checkpoints=False, **kwargs)` | Returns `ArmContext` | Start recording with stack synthesis. `checkpoints=True` opts into fork-based cold navigation for the live tail (the synthesized prefix remains warm-only). kwargs forwarded to `PyttdConfig`. |
| `disarm()` | `() -> dict` | Stop an `arm()` recording and return stats. |

`arm(checkpoints=True)` is safe only when the process state is fork-safe at the time of checkpoint: no active background threads running Python code, no C-extension locks held across function calls, no open sockets that can't survive duplication. The default (`checkpoints=False`) is conservative — use it unless you know you need cold navigation and the process is quiesced.

### `install_signal_handler`

Set up a signal (default SIGUSR1) that toggles recording on/off externally.

```python
from pyttd import install_signal_handler
install_signal_handler()  # First SIGUSR1 arms, second disarms
# ...
# In another terminal: kill -USR1 <pid>
```

| Parameter | Description |
|-----------|-------------|
| `sig` | Signal number (default `signal.SIGUSR1`; Unix only) |
| `db_path` | DB path (default: derived from `__main__.__file__` at install time) |
| `**kwargs` | Forwarded to `PyttdConfig` on each arm |

Can also be activated on import by setting the environment variable `PYTTD_ARM_SIGNAL=USR1` before running a script that does `import pyttd`.

---

## Configuration

### `PyttdConfig`

```python
from pyttd.config import PyttdConfig

config = PyttdConfig(
    checkpoint_interval=1000,
    ring_buffer_size=65536,
    flush_interval_ms=10,
    redact_secrets=True,
    secret_patterns=None,           # None = defaults
    include_functions=[],
    include_files=[],
    exclude_functions=[],
    exclude_files=[],
    max_frames=0,                   # 0 = unlimited
    max_db_size_mb=0,               # 0 = unlimited
    keep_runs=0,                    # 0 = keep all
    checkpoint_memory_limit_mb=0,   # 0 = unlimited
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `checkpoint_interval` | `int` | `1000` | Frames between fork-based checkpoints. `0` disables |
| `ring_buffer_size` | `int` | `65536` | Ring buffer slot count. 0 (default) or >= 64 |
| `flush_interval_ms` | `int` | `10` | Flush-thread wakeup interval (ms) |
| `ignore_patterns` | `list[str]` | `[]` | Additional file patterns to exclude |
| `db_path` | `str \| None` | `None` | Override default `<script>.pyttd.db` path |
| `redact_secrets` | `bool` | `True` | Enable secret-pattern redaction for variable names and dict/NamedTuple children |
| `secret_patterns` | `list[str]` | default list | Extends the built-in patterns (password, token, api_key, ...) |
| `include_functions` | `list[str]` | `[]` | Record only functions matching these glob patterns |
| `include_files` | `list[str]` | `[]` | Record only files matching these glob patterns |
| `exclude_functions` | `list[str]` | `[]` | Exclude functions matching these patterns |
| `exclude_files` | `list[str]` | `[]` | Exclude files matching these glob patterns |
| `max_frames` | `int` | `0` | Approximate frame cap (0 = unlimited) |
| `max_db_size_mb` | `float` | `0` | Auto-stop when DB exceeds this size (0 = unlimited) |
| `keep_runs` | `int` | `0` | Keep only last N runs, evict older (0 = keep all) |
| `checkpoint_memory_limit_mb` | `int` | `0` | Total checkpoint RSS cap (0 = unlimited) |

---

## Recorder

### `Recorder`

Low-level recording lifecycle. Usually you use the public API wrappers above, but `Recorder` is available for advanced integrations.

```python
from pyttd.recorder import Recorder
from pyttd.config import PyttdConfig

recorder = Recorder(PyttdConfig())
recorder.start(db_path="trace.pyttd.db", script_path="my_app.py")
# ... execute user code ...
stats = recorder.stop()
recorder.cleanup()
```

| Method | Returns | Description |
|--------|---------|-------------|
| `start(db_path, script_path=None, attach=False)` | `None` | Initialize DB, create run record, install eval hook |
| `stop()` | `dict` | Stop recording, bulk-load binlog into SQLite, return stats |
| `kill_checkpoints()` | `None` | Send DIE to all checkpoint children, update DB |
| `cleanup()` | `None` | Kill checkpoints and close DB connection |

| Property | Type | Description |
|----------|------|-------------|
| `run_id` | `UUID` | Current run's unique identifier |

The stats dict returned by `stop()`:

```python
{
    'frame_count': 12345,            # Events actually recorded
    'dropped_frames': 0,             # Ring buffer overflow count (bad sign)
    'elapsed_time': 2.1,             # Wall-clock recording seconds
    'pool_overflows': 0,             # String pool overflow count (truncated values)
    'checkpoint_count': 12,          # Checkpoints fired
    'checkpoint_memory_bytes': 0,    # Total checkpoint RSS
    'checkpoints_skipped_threads': 0, # Times the fork was skipped (multi-thread unsafe)
    'warnings': [],                  # Human-readable warnings from the run
    'attach_safe_seq': 0,            # First "safe for checkpoint" seq in arm() mode
}
```

---

## Replay

### `Session`

Navigation state machine for replay mode.

```python
from pyttd.session import Session

session = Session()
session.enter_replay(run_id, first_line_seq)
result = session.step_into()  # result dict with seq, file, line, function_name, ...
```

### Navigation methods

All return a result dict:

```python
{
    "seq": 42,
    "file": "my_app.py",
    "line": 10,
    "function_name": "greet",
    "call_depth": 1,
    "thread_id": 12345,
    "reason": "step",   # step / breakpoint / exception / start / end / goto / pause_boundary
}
```

| Method | Description |
|--------|-------------|
| `step_into()` | Next `line` event (any thread, any depth) |
| `step_over()` | Next `line` event at `call_depth <= current` (same thread) |
| `step_out()` | Next `line` event after current function returns (same thread) |
| `step_back()` | Previous `line` event (always warm) |
| `continue_forward()` | Next breakpoint / exception filter match, or end |
| `reverse_continue()` | Previous breakpoint / exception filter match, or start |
| `goto_frame(target_seq)` | Jump to sequence number (cold → warm fallback) |
| `goto_targets(filename, line)` | All executions at a file:line (max 1000 results) |
| `restart_frame(frame_seq)` | Jump to first line of the function containing `frame_seq` |

### State methods

| Method | Description |
|--------|-------------|
| `enter_replay(run_id, first_line_seq)` | Enter replay mode, build initial stack |
| `enter_paused_replay(run_id, paused_seq)` | Enter replay mode at a pause boundary (live debugging) |
| `set_breakpoints(breakpoints)` | Line breakpoints: `[{"file": str, "line": int, "condition": str?, "hitCondition": str?, "logMessage": str?}]` |
| `set_exception_filters(filters)` | `["raised"]`, `["uncaught"]`, or both |
| `set_function_breakpoints(bps)` | `[{"name": str}]` |
| `set_data_breakpoints(bps)` | `[{"name": str, "accessType": "write"}]` |
| `get_threads()` | `[{"id": int, "name": str}]` |

### Query methods

| Method | Description |
|--------|-------------|
| `get_stack_at(seq)` | Stack frames at position: `[{"seq", "name", "file", "line", "depth"}]` |
| `get_variables_at(seq)` | Variables: `[{"name", "value", "type", "variablesReference"}]` |
| `get_variable_children(reference)` | Expand a container variable by reference (DAP pattern) |
| `get_variable_children_by_name(seq, name)` | Expand by name/dotted path (e.g., `config.database`) |
| `evaluate_at(seq, expr, context)` | Eval expression. Context: `"hover"`, `"watch"`, `"repl"` |
| `set_variable(var_name, new_value_expr)` | Mutate a variable at a pause boundary (live debugging only) |
| `get_traced_files()` | Distinct filenames |
| `list_function_names()` | Distinct function names (for REPL completion) |
| `list_filenames()` | Distinct filenames, sorted |
| `list_variable_names(sample_limit=100)` | Variable names sampled from recent frames |
| `get_execution_stats(filename="")` | Per-function call and exception counts |
| `get_call_children(parent_call_seq, parent_return_seq)` | Call tree children (lazy-loaded) |
| `get_variable_history(name, start, end, max_points=500)` | Change points for a named variable |
| `find_expression_matches(expr, start, end, max_results=100, mode="truthy")` | Frames where an expression is truthy or where its result changes |
| `verify_breakpoints(breakpoints)` | Validate BP locations, return DAP verification results |

### `ReplayController`

Handles warm (SQLite) and cold (checkpoint-restore) navigation.

```python
from pyttd.replay import ReplayController

replay = ReplayController()
result = replay.warm_goto_frame(run_id, target_seq)  # warm path
result = replay.goto_frame(run_id, target_seq)       # cold → warm fallback
```

| Method | Description |
|--------|-------------|
| `goto_frame(run_id, target_seq)` | Try cold (checkpoint), fall back to warm |
| `warm_goto_frame(run_id, target_seq)` | Warm-only: read from SQLite. Returns dict with `warm_only=True` |

---

## Query

Standalone functions in `pyttd.query` for database introspection.

```python
from pyttd.query import (
    get_last_run,
    get_run_by_id,
    get_all_runs,
    get_frames,
    get_frame_at_seq,
    get_line_code,
    search_frames,
    get_frames_by_thread,
)

run = get_last_run(db_path)
run = get_run_by_id(db_path, "abc123")       # UUID or prefix
runs = get_all_runs(db_path)                 # ordered by timestamp_start desc
frames = get_frames(run_id, limit=50)
frame = get_frame_at_seq(run_id, 42)
source = get_line_code("app.py", 10)         # via linecache
```

### Timeline

```python
from pyttd.models.timeline import get_timeline_summary

buckets = get_timeline_summary(
    run_id=run_id,
    start_seq=0,
    end_seq=10000,
    bucket_count=500,
    breakpoints=[{"file": "app.py", "line": 10}],
)
# [{"startSeq", "endSeq", "maxCallDepth", "hasException", "hasBreakpoint", "dominantFunction"}]
```

### Diff

```python
from pyttd.diff import align_and_diff, format_diff_text, format_diff_json

result = align_and_diff(
    db,
    run_id_a, run_id_b,
    ignore_vars={"timestamp"},   # optional
    context=3,                   # lines of matching context
)
print(format_diff_text(result, db_path=db_path))
```

Returns a `DiffResult` dataclass with `kind` (`"identical"`, `"control_flow"`, `"data"`, `"length_mismatch"`), diverging variables, and optional frame context.

---

## Storage

```python
from pyttd.models import storage

storage.connect_to_db(path)          # Initialize deferred DB, set pragmas (WAL, busy_timeout)
storage.initialize_schema()          # Run DDL + migrations (tracks version in pyttd_meta)
storage.ensure_secondary_indexes()   # Lazy: build secondary indexes if missing (first query only)
storage.close_db()                   # Close DB connection
storage.delete_db_files(path)        # Delete .pyttd.db, -wal, -shm, and .pyttd.binlog
```

---

## Database Schema

The models layer uses hand-rolled SQL with a lightweight `Database` wrapper (thread-local sqlite3 connections, `RowProxy` for attribute access). Source of truth: `pyttd/models/schema.py` (`SCHEMA_DDL`, `MIGRATION_SQL`).

### `runs`

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | `TEXT PRIMARY KEY` | UUID hex (no dashes) |
| `timestamp_start` | `REAL` | Wall-clock epoch seconds |
| `timestamp_end` | `REAL` | Wall-clock epoch seconds (null until `stop`) |
| `script_path` | `TEXT` | Recorded script path |
| `total_frames` | `INTEGER` | Total events recorded |
| `is_attach` | `INTEGER` | 1 if this was an `arm()` recording |
| `attach_safe_seq` | `INTEGER` | First "safe for cold navigation" seq (null otherwise) |
| `parent_run_id` | `TEXT` | If this run branched from another (e.g., via `resume_from_past`) |
| `branch_seq` | `INTEGER` | Sequence where the branch forked |

### `executionframes`

| Column | Type | Description |
|--------|------|-------------|
| `frame_id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Auto-increment ID |
| `run_id` | `TEXT NOT NULL` | FK to `runs.run_id` |
| `sequence_no` | `INTEGER` | Monotonic event counter (0-based) |
| `timestamp` | `REAL` | Monotonic seconds since recording start |
| `line_no` | `INTEGER` | Source line number |
| `filename` | `TEXT` | Source file path |
| `function_name` | `TEXT` | Function / method name |
| `frame_event` | `TEXT` | `call`, `line`, `return`, `exception`, `exception_unwind` |
| `call_depth` | `INTEGER` | Nesting depth (0 = top-level) |
| `locals_snapshot` | `TEXT` | JSON string of captured variables |
| `thread_id` | `INTEGER` | OS thread ID |
| `is_coroutine` | `INTEGER` | 1 if frame is from an `async def` function |

Primary indexes: `(run_id, sequence_no)` UNIQUE, `(run_id)`. Secondary indexes (`filename/line_no`, `function_name`, `frame_event/sequence_no`, `call_depth/sequence_no`, `thread_id/sequence_no`) are built lazily on first read-path query via `ensure_secondary_indexes()`.

### `checkpoint`

| Column | Type | Description |
|--------|------|-------------|
| `checkpoint_id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Auto-increment ID |
| `run_id` | `TEXT NOT NULL` | FK to `runs.run_id` |
| `sequence_no` | `INTEGER` | Frame sequence at fork time |
| `child_pid` | `INTEGER` | Forked child PID (null if finalized) |
| `is_alive` | `INTEGER` | 1 if the child process is still running |

### `ioevent`

| Column | Type | Description |
|--------|------|-------------|
| `io_event_id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Auto-increment ID |
| `run_id` | `TEXT NOT NULL` | FK to `runs.run_id` |
| `sequence_no` | `INTEGER` | Frame sequence when I/O occurred |
| `io_sequence` | `INTEGER` | Order within same `sequence_no` |
| `function_name` | `TEXT` | Hooked function (e.g., `time.time`, `random.random`) |
| `return_value` | `BLOB` | Type-tagged serialized return value |

### `pyttd_meta`

| Column | Type | Description |
|--------|------|-------------|
| `key` | `TEXT PRIMARY KEY` | Metadata key (e.g., `migration_version`) |
| `value` | `TEXT` | Value |

Tracks applied migrations so non-idempotent `MIGRATION_SQL` entries run exactly once across DB reopens.

---

## Error Hierarchy

```python
from pyttd.errors import (
    PyttdError,          # Base exception
    RecordingError,      # Recording failures
    ReplayError,         # Replay / navigation failures
    CheckpointError,     # Checkpoint / fork failures
    ServerError,         # JSON-RPC server failures
    NoForkError,         # Platform doesn't support fork()
)
```

---

## Environment Variables

| Variable | Set By | Description |
|----------|--------|-------------|
| `PYTTD_RECORDING` | C extension | `"1"` during active recording, cleared on stop |
| `PYTTD_ARM_SIGNAL` | User (pre-import) | If set (e.g., `USR1`), `import pyttd` installs the signal handler automatically |
| `PYTTD_DB_PATH` | `pyttd ci` wrapper | Override DB path for a child process |

---

## See Also

- [CLI Reference](cli-reference.md) — command-line interface
- [Architecture](architecture.md) — system design and data flow
- [Protocol Reference](development/protocol.md) — JSON-RPC method documentation
- [Getting Started](getting-started.md) — first recording tutorial
