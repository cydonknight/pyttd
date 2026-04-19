# Architecture

pyttd is a time-travel debugger for Python. It records program execution at the C level and provides a full debug experience with step-back, reverse-continue, goto-frame, and a visual timeline.

## Three-Layer Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  VSCode Extension (TypeScript)                                │
│  Debug Adapter Protocol (DAP) · Timeline webview · CodeLens   │
│  Inline values · Call history tree                             │
├──────────────────────────────────────────────────────────────┤
│  Python Backend                                               │
│  JSON-RPC server · Session · Hand-rolled SQL models · CLI     │
├──────────────────────────────────────────────────────────────┤
│  C Extension (pyttd_native)                                   │
│  PEP 523 eval hook · Trace function · Ring buffer · Fork      │
│  checkpoints · I/O hooks                                      │
└──────────────────────────────────────────────────────────────┘
```

### Layer 1: C Extension (`pyttd_native`)

The C extension is the recording engine. It hooks into CPython's frame evaluation pipeline using two mechanisms:

- **PEP 523 eval hook** — intercepts every frame entry (`call` events). Installed via `_PyInterpreterState_SetEvalFrameFunc`. Fires once per function call.
- **C-level trace function** — installed via `PyEval_SetTrace` for per-line `line`, `return`, `exception`, and `exception_unwind` events.

The eval hook owns `call_depth` management (increment before eval, decrement after) and records `exception_unwind` when a frame exits via exception propagation.

Events are pushed into a **lock-free SPSC ring buffer** (one per thread) with double-buffered string pools. A background flush thread drains the buffers and batch-inserts events into SQLite via a Python callback.

### Layer 2: Python Backend (`pyttd/`)

The Python layer provides:

- **Recorder** — wraps the C extension, manages run lifecycle, writes to hand-rolled SQL models
- **Session** — navigation state machine (forward/reverse stepping, breakpoints, stack reconstruction, expression watchpoints, variable history)
- **ReplayController** — warm (SQLite read) and cold (checkpoint restore + fast-forward) navigation
- **JSON-RPC server** — Content-Length framed protocol over TCP, two-thread model (RPC + recording)
- **Models layer** — lightweight `Database` wrapper over `sqlite3` with thread-local connections, `RowProxy` for attribute access, `SCHEMA_DDL` + `MIGRATION_SQL` as source of truth. Migration versioning via the `pyttd_meta` table
- **CLI** — `record`, `query`, `replay`, `serve`, `export`, `clean`, `diff`, `ci` subcommands
- **pytest plugin** — `--pyttd`, `--pyttd-on-fail`, `--pyttd-replay` registered via pyproject entry point

### Layer 3: VSCode Extension (`vscode-pyttd/`)

The TypeScript extension implements:

- **Debug Adapter** — inline DAP implementation with full forward/reverse navigation handlers
- **Backend Connection** — spawns Python server, TCP connect, JSON-RPC request/response correlation
- **Timeline Scrubber** — canvas-based webview in the Debug sidebar with click/drag/zoom
- **CodeLens** — annotations above traced functions showing call/exception counts
- **Inline Values** — variable values displayed inline during stepping
- **Call History** — tree view with lazy-loaded call hierarchy

## Data Flow

### Recording

```
User script execution
       │
       ▼
PEP 523 eval hook (call events)
       │
       ├──► Trace function (line/return/exception events)
       │
       ▼
Per-thread SPSC ring buffer + string pool
       │
       ▼  (flush thread, every 10ms)
C-level binary log writer (buffered fwrite; no Python objects)
       │
       ▼  (recorder.stop)
Bulk-load binlog → SQLite executionframes (.pyttd.db)
```

Recording uses a binary log file on disk during execution (`.pyttd.binlog`)
rather than writing directly into SQLite per flush — the flush path stays
lock-free and allocation-free, and the SQLite load happens once at stop
time as a single bulk INSERT inside one transaction with secondary
indexes absent. Secondary indexes are built lazily on the first read-path
query (see `storage.ensure_secondary_indexes()`), keeping `pyttd record`'s
exit time fast.

Each event is a `FrameEvent` struct containing:
- `sequence_no` — globally unique, monotonically increasing (atomic counter across threads)
- `timestamp` — monotonic seconds since recording start
- `line_no`, `filename`, `function_name`
- `frame_event` — one of `call`, `line`, `return`, `exception`, `exception_unwind`
- `call_depth` — nesting depth (0 = top-level user frame)
- `locals_json` — JSON-serialized `repr()` snapshots of local variables
- `thread_id` — OS thread identifier

### Frame Filtering

Not all frames are recorded. The `should_ignore()` filter excludes:
- CPython frozen modules (`<frozen runpy>`, `<frozen importlib._bootstrap>`, etc.)
- pyttd's own package directory
- Standard library and site-packages directories
- Specific internal filenames and function names

Ignored frames have their trace function temporarily removed to prevent inherited tracing of sub-frames.

### Navigation

pyttd uses a **post-mortem replay model**: the script runs to completion first, then the user navigates the recorded frames.

#### Warm Navigation (SQLite read, sub-ms)

Used for: `step_into`, `step_over`, `step_out`, `continue_forward`, `step_back`, `reverse_continue`

The Session reads directly from SQLite using indexed queries on `(run_id, sequence_no)`. Stack reconstruction walks forward from the nearest cache point, pushing on `call` events and popping on `return`/`exception_unwind` events.

#### Cold Navigation (checkpoint restore, 50-300ms)

Used for: `goto_frame` (large jumps)

Fork-based checkpoints create full-process snapshots at configurable intervals. To navigate to a distant frame:

1. Find the nearest checkpoint before the target (`checkpoint_store_find_nearest`)
2. Send `RESUME(target_seq)` command via pipe
3. Child enables fast-forward mode (counts sequence numbers without serialization)
4. When target is reached, child serializes state and sends result via pipe
5. Parent updates checkpoint's `current_position`

```
Parent process                    Forked child
     │                                │
     │──── RESUME(target_seq) ───────►│
     │                                │ fast-forward (count only)
     │                                │ ...
     │                                │ target reached
     │◄──── JSON result ─────────────│
     │                                │ (waits for next command)
```

### Checkpoint Management

- Max 32 checkpoints in a static array
- **Smallest-gap thinning eviction** — sorts by `sequence_no`, finds the pair with the smallest gap, evicts the earlier one. Never evicts the most recent checkpoint. Provides O(log N) coverage of the full recording
- Evicted children receive `DIE` command, with a 10ms grace period before `SIGKILL`
- Checkpoints are skipped when multiple threads are active (fork is unsafe with threads)

### I/O Hooks (Deterministic Cold Replay)

For cold replay to be correct, non-deterministic functions must return the same values as during recording. pyttd hooks:

- `time.time`, `time.monotonic`, `time.perf_counter` (serialized as IEEE 754 doubles)
- `random.random`, `random.randint` (float/int serialization)
- `os.urandom` (raw bytes with length prefix)

During recording, hooks call the original function and log the return value. During cold replay in checkpoint children, hooks return pre-loaded values from a cursor.

### Multi-Thread Recording

Each Python thread gets its own SPSC ring buffer (allocated lazily on first frame entry). The global `g_sequence_counter` uses `atomic_fetch_add` to assign globally ordered sequence numbers across threads. Per-thread state (`call_depth`, `inside_repr`) uses thread-local storage (TLS).

Thread-aware navigation:
- `step_over` and `step_out` stay on the current thread
- `step_into` and `step_back` follow global sequence order
- Stack reconstruction filters by the target thread's ID

## Communication Protocol

### VSCode Extension ↔ Python Server

JSON-RPC over TCP with Content-Length framing (same wire format as DAP/LSP):

```
Content-Length: 42\r\n
\r\n
{"jsonrpc":"2.0","id":1,"method":"next"}
```

The server binds to `127.0.0.1:0` (OS-assigned port) and writes `PYTTD_PORT:<port>` to stdout for the extension to discover.

Security limits:
- 1 MB header accumulation limit
- 10 MB Content-Length limit
- Non-ASCII header rejection

### Checkpoint Parent ↔ Child

9-byte binary commands over Unix pipes:
- 1-byte opcode: `0x01` RESUME, `0x02` STEP, `0xFF` DIE
- 8-byte big-endian uint64 payload

Results: 4-byte big-endian length prefix + JSON string.

## Database Schema

SQLite with WAL mode, `busy_timeout=5000`. Tables are lowercase
per standard SQL convention. See [api-reference.md](api-reference.md#database-schema)
for full column listings.

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `runs` | Run metadata | `run_id` (TEXT PK), `script_path`, `timestamp_start/end`, `total_frames`, `is_attach`, `attach_safe_seq`, `parent_run_id`, `branch_seq` |
| `executionframes` | Frame events | `frame_id` (AUTOINCREMENT PK), `sequence_no`, `timestamp`, `filename`, `line_no`, `function_name`, `frame_event`, `call_depth`, `locals_snapshot` (JSON TEXT), `thread_id`, `is_coroutine` |
| `checkpoint` | Checkpoint metadata | `run_id`, `sequence_no`, `child_pid`, `is_alive` |
| `ioevent` | I/O hook recordings | `run_id`, `sequence_no`, `io_sequence`, `function_name`, `return_value` (BLOB) |
| `pyttd_meta` | Migration versioning | `key` (TEXT PK), `value` (TEXT) — tracks `migration_version` for one-shot migration application |

## Platform Support

| Platform | Recording | Warm Navigation | Cold Navigation | Multi-Thread |
|----------|-----------|-----------------|-----------------|--------------|
| Linux    | Full      | Full            | Full            | Full         |
| macOS    | Full      | Full            | Partial*        | Full         |
| Windows  | Full      | Full            | None            | Full         |

\* macOS: fork works only if single-threaded at fork time. Checkpoints are skipped when multiple threads are active.

## Key Design Decisions

- **Python >= 3.12 required** — uses `PyUnstable_InterpreterFrame_*` accessors added in 3.12
- **C extension only** — no pure-Python recording fallback (performance critical)
- **Post-mortem model with live pause opt-in** — typical use is "record then navigate." Live pause + resume-from-past is available via the pause RPC and `arm()` attach mode
- **Structured variable snapshots** — containers (dicts, lists, tuples, sets, objects with `__dict__`/`__slots__`) are captured as expandable trees; primitives use a fast-repr path
- **JSON-RPC over TCP** — avoids user script stdout corrupting the protocol channel
- **Two timestamp domains** — `runs.timestamp_*` are wall-clock epoch seconds; `executionframes.timestamp` is monotonic seconds since recording start
- **Step back is always warm** — reads from SQLite, no checkpoint needed
- **Deferred database** — `Database()` stub at import time, `db.init(path)` at runtime. Thread-local sqlite3 connections
- **Binlog + bulk load** — recording writes to a disk binlog; SQLite load runs once at stop time. Secondary indexes are built lazily on first read-path query
- **Zero runtime Python dependencies** — only stdlib (including `sqlite3`)

## See Also

- [Getting Started](getting-started.md) — install and first recording
- [C Extension Guide](development/c-extension.md) — deep dive into the C layer
- [Protocol Reference](development/protocol.md) — JSON-RPC method documentation
- [API Reference](api-reference.md) — Python public API
