# Changelog

All notable changes to pyttd are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] - 2026-03-17

### Added

#### Multi-Thread Recording
- All Python threads are now recorded with per-thread call stacks and global sequence ordering
- Per-thread SPSC ring buffers with lazy allocation on first frame entry
- Atomic sequence counter (`atomic_fetch_add`) for globally ordered events across threads
- Thread-local storage (TLS) for `call_depth` and `inside_repr` guards
- `ExecutionFrames.thread_id` field stores actual OS thread ID (`BigIntegerField`)
- Thread-aware navigation: `step_over`/`step_out` stay on current thread; `step_into`/`step_back` follow global sequence
- `Session.get_threads()` returns all threads seen during recording with names
- Stack reconstruction filters by target thread's ID
- Checkpoint skip guard: checkpoints are not created when multiple threads are active (fork is unsafe with threads)
- pthread key destructor marks orphaned thread buffers for flush thread cleanup

#### Tests
- 12 new multi-thread tests covering per-thread recording, stacks, thread-aware navigation

### Changed
- Ring buffer system redesigned: per-thread SPSC buffers instead of single global buffer
- Main thread gets 8MB string pools; secondary threads get 2MB pools
- `g_sequence_counter` changed from static to atomic global for cross-thread visibility
- `g_call_depth` and `g_inside_repr` changed from static to TLS (`PYTTD_THREAD_LOCAL`)

## [0.2.0] - 2026-03-15

### Added

#### Fork-Based Checkpointing (Phase 2)
- `fork()` creates full-process snapshots for cold navigation
- Checkpoint store with static array of 32 entries and smallest-gap thinning eviction
- Pipe-based IPC protocol: 9-byte commands (RESUME/STEP/DIE) with length-prefixed JSON results
- Fast-forward mode in checkpoint children (counts sequence numbers without serialization)
- `PyOS_BeforeFork`/`PyOS_AfterFork_Child`/`PyOS_AfterFork_Parent` for Python 3.13+ compatibility
- Pre-fork flush thread synchronization via condvar protocol
- `Checkpoint` Peewee model for tracking checkpoint metadata
- `ReplayController` with `goto_frame` (cold) and `warm_goto_frame` (warm-only) methods
- `pyttd replay --last-run --goto-frame N` CLI subcommand

#### JSON-RPC Server & Debug Adapter (Phase 3)
- `pyttd serve --script s.py` starts JSON-RPC server over TCP (localhost only)
- Content-Length framed JSON-RPC protocol (`JsonRpcConnection` class)
- Two-thread server model: RPC thread (selector-based event loop) + recording thread
- Stdout/stderr capture via `os.pipe()` + `os.dup2()`
- Port handshake: server writes `PYTTD_PORT:<port>` to stdout
- Session navigation: `step_into`, `step_over`, `step_out`, `continue_forward` with breakpoints and exception filters
- Stack reconstruction from frame events with push/pop on call/return
- Variable queries (`get_variables_at`) with type inference from `repr()` values
- Expression evaluation (`evaluate_at`) for hover/watch/repl contexts
- VSCode extension with inline debug adapter (`DebugAdapterInlineImplementation`)
- Full DAP handler implementations for forward navigation
- Backend connection with spawn, TCP connect, JSON-RPC request/response correlation

#### Time-Travel Navigation (Phase 4)
- `step_back` — previous `line` event (always warm, sub-ms)
- `reverse_continue` — backward scan with breakpoint and exception filter matching
- `goto_frame` — jump to any frame by sequence number with line-snapping
- `goto_targets` — find all executions at a file:line (capped at 1000)
- `restart_frame` — jump to first line of a function containing a given frame
- Stack cache optimization for large backward jumps
- I/O hooks for deterministic cold replay: `time.time`, `time.monotonic`, `time.perf_counter`, `random.random`, `random.randint`, `os.urandom`
- Type-specific I/O serialization: IEEE 754 doubles for floats, length-prefixed for ints/bytes
- I/O replay mode in checkpoint children with pre-loaded event cursor
- `IOEvent` Peewee model for I/O event storage
- DAP `supportsStepBack`, `supportsGotoTargetsRequest`, `supportsRestartFrame` capabilities

#### Timeline Scrubber (Phase 5)
- Canvas-based timeline webview in the Debug sidebar
- SQL bucket aggregation with configurable bucket count for timeline summary queries
- Vertical bars scaled by call depth, color-coded (blue=normal, red=exception, orange=breakpoint)
- Yellow cursor line with triangle marker for current position
- Click to navigate, drag with 150ms throttle, mousewheel zoom
- Keyboard shortcuts: arrows=step, Home/End=bounds, PageUp/PageDown=zoom
- Zoom cache (max 4 entries) for responsive interaction
- DPR-aware canvas rendering with `ResizeObserver`
- Custom DAP events: `pyttd/timelineData` (bucket data), `pyttd/positionChanged` (cursor sync)
- Breakpoint markers update immediately on `setBreakPointsRequest`

#### CodeLens, Inline Values, Call History (Phase 6)
- CodeLens annotations above traced functions showing "TTD: N calls | M exceptions"
- Click CodeLens to navigate to first execution of a function
- `InlineValuesProvider` displays variable values inline during stepping
- Call history tree in Debug sidebar with lazy-loaded nesting via `get_call_children`
- Exception icons and incomplete call markers in call history
- `get_traced_files` RPC — distinct filenames from recording
- `get_execution_stats` RPC — per-function call/exception counts via GROUP BY with CASE WHEN
- `get_call_children` RPC — call/return pairing at target depth for tree loading

#### Polish, Packaging, CI (Phase 7)
- `pyttd --version` prints version
- `pyttd -v` / `--verbose` enables debug logging
- `pyttd serve --db path.pyttd.db` replay-only mode (no recording phase)
- `pyttd record` and `pyttd serve --script` validate script exists before starting
- `PYTTD_RECORDING=1` environment variable set during recording, cleared after stop
- Protocol robustness: 1 MB header accumulation limit, 10 MB Content-Length limit, non-ASCII header rejection
- PyPI packaging: pyproject.toml with classifiers, URLs, readme, cibuildwheel config
- `MANIFEST.in` for source distribution (headers, py.typed, tests)
- `py.typed` marker (PEP 561) for type checking support
- GitHub Actions CI: test matrix (Python 3.12/3.13, Linux/macOS), ASAN build, sdist smoke test

#### Tests
- Test count grew from 26 (v0.1.0) to 147 across 14 test files
- 70 VSCode extension Mocha tests (backendConnection, debugSession, providers)

### Fixed

These fixes were developed between v0.1.0 and v0.2.0:

#### Critical (crash/data corruption prevention)

- **Data race on string pool `producer_idx`** — `ringbuf_pool_swap()` and `ringbuf_pool_reset_consumer()` were called outside the GIL in `flush_batch()`, racing with the producer thread. Moved both operations inside the GIL-protected section to serialize access. (`ext/recorder.c`)
- **NULL dereference in `flush_batch` on OOM** — `Py_DECREF(NULL)` (undefined behavior) if any `PyLong_From*`/`PyFloat_From*`/`PyUnicode_FromString` returned NULL. Added NULL checks for all 8 allocations per event; on failure, skips the event gracefully. (`ext/recorder.c`)
- **NULL dereference from `PyUnicode_AsUTF8`** — eval hook and all three trace function cases (`PyTrace_LINE`, `PyTrace_RETURN`, `PyTrace_EXCEPTION`) passed potentially-NULL strings to `should_ignore()`, `strstr()`, and `ringbuf_push()`. Added NULL checks; on failure, clears the error and skips the frame. (`ext/recorder.c`)
- **`strdup` NULL stored in ignore filters** — if `strdup()` returned NULL (OOM), the NULL pointer was stored and later passed to `strstr()`/`strcmp()` (undefined behavior). Now only stores and increments count on successful `strdup`. (`ext/recorder.c`)
- **`request_stop` interrupted the flush thread** — the stop-request check in the eval hook fired before the main-thread check, so the flush thread's Python calls (GIL-acquired imports, `db.close()`) would receive a spurious `KeyboardInterrupt`. Stop check is now gated on `g_main_thread_id`. (`ext/recorder.c`)

#### Correctness

- **`flush_interval_ms` parameter was ignored** — the parameter was parsed by `start_recording` but the flush thread hardcoded 10ms. Added `g_flush_interval_ms` global; the flush thread now uses the configured value on both POSIX and Windows. (`ext/recorder.c`)
- **Version-gated macros used dead `#elif defined()` branch** — `defined(_PyInterpreterState_SetEvalFrameFunc)` never matches (it's a function declaration, not a macro), making the `#elif` and `#else` branches identical dead code. Replaced with a clean `PY_VERSION_HEX` range check: `>= 0x030F0000` for 3.15+ and `#else` for 3.12-3.14. (`ext/recorder.c`)
- **`_cmd_query` created empty DB if file didn't exist** — `get_last_run()` called `connect_to_db()` which silently creates a new database, then `Runs.select().get()` raised an unhelpful `DoesNotExist`. Added `os.path.exists()` check with a clear error message before connecting. (`pyttd/cli.py`)
- **`sys.path[0]` restoration could raise `IndexError`** — if a user script cleared `sys.path`, the `finally` block's `sys.path[0] = old_path0` would crash. Now checks `len(sys.path) > 0` first and falls back to `sys.path.insert(0, ...)`. (`pyttd/runner.py`)
- **Negative `buffer_size` caused huge allocation** — a negative `int` silently cast to a large `uint32_t`. Added validation that rejects negative values with `ValueError`. (`ext/recorder.c`)
- **`PyDict_SetItemString` return values unchecked in `flush_batch`** — failures were silently ignored. Now checks return values; on error, clears the exception and continues. (`ext/recorder.c`)
- **`pyttd_get_recording_stats` had no NULL checks** — the 5 `PyLong_From*`/`PyFloat_From*` calls could return NULL on OOM, leading to `PyDict_SetItemString(dict, key, NULL)` and `Py_DECREF(NULL)`. Added NULL check with proper cleanup. (`ext/recorder.c`)
- **Windows `\` path separators not detected in ignore patterns** — `strchr(pattern, '/')` missed backslash-separated paths. Added `strchr(pattern, '\\')` check. (`ext/recorder.c`)

### Removed

- **Dead code: `pyttd/performance/clock.py`** — `Clock` class was unused (holdover from pre-C-extension era).
- **Dead code: `pyttd/performance/performance.py`** — `TracePerformance` class was unused; also had a logic bug (`total_samples` initialized to 1 instead of 0).

### Changed

- Updated project documentation: `exception_unwind` line_no limitation, interrupt mechanism (main-thread-only gating), pool swap/reset inside GIL, recorder.c description.
- Fixed misleading "Thread-local" comment on `g_locals_buf` — it is a static global, safe only because Phase 1 records the main thread exclusively. (`ext/recorder.c`)
- Added documentation comments for `call` event not capturing locals (frame may not be initialized) and `exception_unwind` not capturing locals (internal frame may not be valid after eval). (`ext/recorder.c`)

## [0.1.0] - 2026-03-04

Initial implementation of Phases 0 and 1.

### Added

#### C Extension (`pyttd_native`)
- PEP 523 frame eval hook for intercepting frame entry (`call` events)
- C-level trace function (`PyEval_SetTrace`) for `line`, `return`, `exception` events
- `exception_unwind` event recorded by eval hook when frame exits via exception propagation
- Lock-free SPSC ring buffer (C11 `<stdatomic.h>`, power-of-2 capacity, default 65536 slots)
- Double-buffered 8MB string pools with drop-on-full semantics
- JSON escaping helper (`json_escape_string()`) for locals serialization
- `call_depth` tracking in the eval hook (not trace function)
- Monotonic timestamp recording relative to recording start
- Flush thread with configurable interval, GIL management, and DB connection cleanup on exit
- `request_stop()` — atomic stop flag checked by eval hook, raises `KeyboardInterrupt`
- `set_ignore_patterns()` — substring (directory) and exact-match (basename/function) filtering
- `get_recording_stats()` — frame count, dropped frames, elapsed time, flush count, pool overflows
- Platform detection macros (`PYTTD_HAS_FORK`, Windows/POSIX)
- Stub functions for Phase 2+ (`create_checkpoint`, `restore_checkpoint`, `kill_all_checkpoints`, `install_io_hooks`, `remove_io_hooks`)

#### Python Backend
- `pyttd record script.py` — records all user-code frame events to `<script>.pyttd.db`
- `pyttd record --module pkg.mod` — records module execution
- `pyttd query --last-run --frames` — dumps recorded frames with source lines
- `@ttdbg` decorator for recording function execution
- `Recorder` class — Python wrapper around C recorder with run lifecycle management
- `Runner` class — user script/module execution via `runpy`
- `PyttdConfig` dataclass for configuration
- Custom exception hierarchy (`PyttdError`, `RecordingError`, `ReplayError`, `ServerError`)
- Peewee ORM models with deferred database pattern (`SqliteDatabase(None)`)
  - `Runs` — run metadata (UUID PK, script path, timestamps, frame count)
  - `ExecutionFrames` — frame events with indexes on `(run_id, sequence_no)`, `(run_id, filename, line_no)`, etc.
- Storage utilities: `connect_to_db`, `close_db`, `delete_db_files`, `initialize_schema`
- Query API: `get_last_run`, `get_frames`, `get_frame_at_seq`, `get_line_code`
- WAL mode, `busy_timeout: 5000`, batch insert (500 per batch)
- Stub subcommands for `replay` (Phase 2) and `serve` (Phase 3)

#### VSCode Extension (skeleton)
- TypeScript project structure with `package.json`, `tsconfig.json`
- DAP handler stubs in `pyttdDebugSession.ts`
- Backend connection stub in `backendConnection.ts`

#### Tests
- 26 tests passing across 4 test files
- `test_models.py` (7) — model creation, batch insert, WAL mode
- `test_native_stub.py` (7) — import check, stub functions raise `NotImplementedError`
- `test_recorder.py` (9) — recording, sequence monotonicity, events, locals, call depth
- `test_ringbuf.py` (3) — flush pipeline, dict keys, recording stats
- All tests use `tmp_path` fixture for DB isolation

#### Build System
- `pyproject.toml` — project metadata, dependencies, build system config
- `setup.py` — C extension build (`ext_modules`)
- Editable install via `pip install -e ".[dev]"`
