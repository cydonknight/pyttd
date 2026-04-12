# Protocol Reference

This documents the communication protocols used by pyttd.

## JSON-RPC over TCP

The VSCode extension communicates with the Python backend via JSON-RPC 2.0 over TCP with Content-Length framing.

### Wire Format

Same format as DAP/LSP:

```
Content-Length: <byte_count>\r\n
\r\n
<JSON body>
```

### Security Limits

| Limit | Value | Purpose |
|-------|-------|---------|
| Header accumulation | 1 MB | Prevents memory exhaustion from incomplete headers |
| Content-Length | 10 MB | Prevents oversized message allocation |
| Non-ASCII headers | Rejected | Prevents encoding attacks |

### Port Handshake

The server binds to `127.0.0.1:0` and writes to stdout:

```
PYTTD_PORT:<port>\n
```

The extension reads this line after spawning the server process to discover the port.

### Connection Lifecycle

```
Extension                                Server
    │                                       │
    │────── spawn process ─────────────────►│
    │◄───── PYTTD_PORT:12345 ──────────────│
    │────── TCP connect ───────────────────►│
    │────── backend_init ──────────────────►│
    │◄───── response ──────────────────────│
    │────── launch ────────────────────────►│
    │◄───── response ──────────────────────│
    │────── configuration_done ────────────►│
    │◄───── response ──────────────────────│
    │       (recording starts)              │
    │◄───── progress notifications ────────│
    │◄───── output notifications ──────────│
    │       (recording completes)           │
    │◄───── stopped {reason: "start"} ─────│
    │       (replay mode)                   │
    │────── navigation RPCs ───────────────►│
    │◄───── responses + stopped events ────│
    │────── disconnect ────────────────────►│
    │◄───── response ──────────────────────│
```

### Method Naming Convention

All pyttd JSON-RPC method names use **snake_case**, even when the corresponding DAP concept uses camelCase. For example:

- `configurationDone` (DAP) → `configuration_done` (pyttd RPC)
- `setBreakpoints` (DAP) → `set_breakpoints` (pyttd RPC)
- `stepBack` (DAP) → `step_back` (pyttd RPC)

Sending a camelCase method name silently returns `{"error": {"code": -32601, "message": "Method not found"}}`. Anyone implementing a custom client (e.g. for a non-VSCode IDE plugin) should convert method names to snake_case before sending.

## RPC Methods

### Lifecycle

#### `backend_init`

Initialize the backend. No parameters.

**Response:** `{"version": "0.3.0"}`

#### `launch`

Store launch configuration. Called before `configuration_done`.

**Parameters:** `{"program": "...", "args": [...], ...}` (launch config properties)

**Response:** `{}`

#### `configuration_done`

Start recording (with `--script`) or enter replay (with `--db`).

**Response:** `{}`

After recording completes, the server sends a `stopped` notification with `reason: "start"`.

#### `disconnect`

End the session. Kills checkpoints, closes DB.

**Response:** `{}`

### Navigation

All navigation methods return a position result:

```json
{
    "seq": 42,
    "file": "app.py",
    "line": 10,
    "function_name": "main",
    "call_depth": 0,
    "thread_id": 12345,
    "reason": "step"
}
```

The `reason` field indicates why execution stopped:

| Reason | Description |
|--------|-------------|
| `step` | Single-step completed |
| `breakpoint` | Hit a line breakpoint |
| `exception` | Hit an exception filter |
| `start` | Beginning of recording |
| `end` | End of recording |
| `goto` | Jump to specific frame |

#### `next`

Step over — next `line` event at depth <= current (same thread).

#### `step_in`

Step into — next `line` event (any depth, any thread).

#### `step_out`

Step out — next `line` event after current function returns (same thread).

#### `continue`

Continue forward to next breakpoint/exception match, or end of recording.

#### `step_back`

Step backward — previous `line` event (always warm).

#### `reverse_continue`

Continue backward to previous breakpoint/exception match, or start of recording.

#### `goto_frame`

**Parameters:** `{"seq": <target_sequence_no>}`

Jump to any frame. Uses cold navigation (checkpoint restore) for large jumps, warm for nearby frames.

#### `goto_targets`

**Parameters:** `{"file": "app.py", "line": 10}`

**Response:** Array of `{"seq": int}` — all executions at that file:line (capped at 1000).

#### `restart_frame`

**Parameters:** `{"seq": <frame_sequence_no>}`

Jump to the first line event of the function containing the given frame.

### State

#### `set_breakpoints`

**Parameters:** `{"file": "app.py", "breakpoints": [{"line": 10}, {"line": 20}]}`

**Response:** `{}`

#### `set_exception_breakpoints`

**Parameters:** `{"filters": ["raised", "uncaught"]}`

**Response:** `{}`

#### `interrupt`

Stop recording early (same effect as Ctrl+C).

**Response:** `{}`

#### `get_threads`

**Response:** `{"threads": [{"id": 12345, "name": "Main Thread"}, ...]}`

#### `get_stack_trace`

**Response:** Stack frames at current position.

```json
{
    "stackFrames": [
        {"seq": 42, "name": "inner", "file": "app.py", "line": 15, "depth": 2},
        {"seq": 30, "name": "outer", "file": "app.py", "line": 8, "depth": 1},
        {"seq": 0, "name": "<module>", "file": "app.py", "line": 1, "depth": 0}
    ]
}
```

#### `get_scopes`

**Parameters:** `{"seq": <sequence_no>}`

**Response:** `{"scopes": [{"name": "Locals", "variablesReference": <seq+1>}]}`

#### `get_variables`

**Parameters:** `{"variablesReference": <ref>}`

The `variablesReference` encodes the sequence number as `seq + 1` (0 is reserved for "no reference" in DAP).

**Response:**

```json
{
    "variables": [
        {"name": "x", "value": "42", "type": "int"},
        {"name": "items", "value": "['a', 'b']", "type": "list"}
    ]
}
```

#### `evaluate`

**Parameters:** `{"seq": <sequence_no>, "expression": "x + 1", "context": "hover|watch|repl"}`

**Response:** `{"result": "43", "type": "int"}`

### Query

#### `get_timeline_summary`

**Parameters:** `{"startSeq": 0, "endSeq": 10000, "bucketCount": 500}`

**Response:** Array of timeline buckets:

```json
[
    {
        "startSeq": 0,
        "endSeq": 20,
        "maxCallDepth": 3,
        "hasException": false,
        "hasBreakpoint": true,
        "dominantFunction": "main"
    }
]
```

#### `get_traced_files`

**Response:** `{"files": ["app.py", "utils.py"]}`

#### `get_execution_stats`

**Parameters:** `{"file": "app.py"}`

**Response:**

```json
{
    "stats": [
        {
            "function_name": "main",
            "line_no": 5,
            "call_count": 1,
            "exception_count": 0,
            "firstCallSeq": 0
        }
    ]
}
```

#### `get_call_children`

**Parameters:** `{"parentCallSeq": 0, "parentReturnSeq": 100}`

**Response:** Array of child calls for building the call history tree.

### Notifications (Server → Extension)

#### `stopped`

Sent when replay position changes (navigation result).

```json
{"method": "stopped", "params": {"reason": "step", "seq": 42, ...}}
```

#### `output`

Captured stdout/stderr from the user script.

```json
{"method": "output", "params": {"category": "stdout", "output": "Hello\n"}}
```

#### `progress`

Recording progress updates.

```json
{"method": "progress", "params": {"frames": 5000}}
```

### Custom DAP Events

These are emitted as DAP custom events (not JSON-RPC notifications):

#### `pyttd/timelineData`

Timeline bucket data for the scrubber webview.

#### `pyttd/positionChanged`

Current replay position changed. Used by the timeline scrubber to update the cursor.

## Checkpoint Pipe Protocol

Binary protocol between parent process and forked checkpoint children.

### Command Format (Parent → Child)

9 bytes: 1-byte opcode + 8-byte big-endian uint64 payload.

| Opcode | Name | Payload | Description |
|--------|------|---------|-------------|
| `0x01` | RESUME | target_seq | Fast-forward to target sequence, serialize state, wait |
| `0x02` | STEP | delta | Reserved (not currently used from parent) |
| `0xFF` | DIE | 0 | Child exits immediately |

### Result Format (Child → Parent)

4-byte big-endian uint32 length prefix + JSON string.

```json
{
    "status": "ok",
    "seq": 750,
    "file": "app.py",
    "line": 25,
    "function_name": "process",
    "call_depth": 1,
    "locals": {"x": "42", "items": "['a', 'b']"}
}
```

On error:

```json
{
    "status": "error",
    "error": "target_not_reached",
    "last_seq": 500
}
```

## See Also

- [Architecture](../architecture.md) — system overview
- [C Extension Guide](c-extension.md) — checkpoint and I/O hook internals
- [API Reference](../api-reference.md) — Python public API
