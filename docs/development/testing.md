# Testing Guide

## Running Tests

### Python Tests (159 tests)

```bash
# Run all tests
.venv/bin/pytest tests/ -v

# Run a specific test file
.venv/bin/pytest tests/test_recorder.py -v

# Run a specific test
.venv/bin/pytest tests/test_recorder.py::test_basic_recording -v

# Run with benchmark disabled (faster)
.venv/bin/pytest tests/ -v --benchmark-disable
```

### VSCode Extension Tests (70 tests)

```bash
cd vscode-pyttd
npm test    # Compiles TypeScript, then runs Mocha
```

### Test File Overview

| File | Tests | What It Covers |
|------|-------|----------------|
| `test_models.py` | 7 | Model creation, batch insert, WAL mode |
| `test_native_stub.py` | 4 | C extension import, method names, checkpoint smoke tests |
| `test_recorder.py` | 11 | Recording, sequence monotonicity, events, locals, call depth, repr reentrancy, elapsed time |
| `test_ringbuf.py` | 3 | Flush pipeline, dict keys, recording stats |
| `test_checkpoint.py` | 7 | Checkpoint creation, sequence numbers, cleanup, stale removal |
| `test_replay.py` | 6 | Warm goto_frame, return/exception events, locals validation |
| `test_reverse_nav.py` | 14 | step_back, reverse_continue, goto_frame, goto_targets, restart_frame |
| `test_iohook.py` | 9 | I/O hook recording, serialization, restoration, exception propagation |
| `test_session.py` | 22 | Navigation, stack, variables, evaluate, type inference |
| `test_server.py` | 17 | Server integration, RPC, recording cycle, output capture |
| `test_timeline.py` | 16 | Timeline summary queries, bucket boundaries, breakpoints, exceptions |
| `test_phase6.py` | 13 | get_traced_files, get_execution_stats, get_call_children |
| `test_phase7.py` | 18 | CLI, protocol robustness, env var, serve --db, packaging |
| `test_multithread.py` | 12 | Multi-thread recording, per-thread stacks, thread-aware navigation |

### VSCode Extension Test Files

| File | Tests | What It Covers |
|------|-------|----------------|
| `backendConnection.test.ts` | 17 | TCP connect, JSON-RPC, notifications, findPythonPath |
| `pyttdDebugSession.test.ts` | 34 | DAP handlers, navigation, notifications, customRequest |
| `providers.test.ts` | 19 | CallHistory, CodeLens, InlineValues, TimelineScrubber |

## Test Fixtures

### `conftest.py`

Key shared fixtures:

| Fixture | Scope | Description |
|---------|-------|-------------|
| `db_path` | function | Temporary `.pyttd.db` path via `tmp_path` |
| `db_setup` | function | Connects to DB, creates schema, yields, closes |
| `record_func` | function | Helper to record a function and return frames |

### DB Isolation

All tests use pytest's `tmp_path` fixture for database isolation. No leftover `.pyttd.db` files after tests.

```python
def test_example(db_path, db_setup):
    """db_path is a unique temp path; db_setup connects and creates tables."""
    recorder = Recorder(PyttdConfig())
    recorder.start(db_path=str(db_path))
    # ...
```

### VSCode Extension Mocks

- `mock-vscode.ts` — patches `require.cache['vscode']` so TypeScript modules can import the `vscode` namespace
- `MockRpcServer` — real TCP server with Content-Length framing and handler map for auto-responses

## Writing New Tests

### Python Test Conventions

1. **Use `tmp_path`** for any test that creates a database
2. **Use `db_setup` fixture** to handle DB connection lifecycle
3. **Test file naming**: `tests/test_<component>.py`
4. **Import from package**: `from pyttd.recorder import Recorder`, not relative imports

Example:

```python
def test_my_feature(db_path, db_setup):
    """Test description."""
    recorder = Recorder(PyttdConfig())
    recorder.start(db_path=str(db_path), script_path="test.py")

    # Execute some code under recording
    def target():
        x = 1
        return x

    target()
    stats = recorder.stop()

    assert stats['frame_count'] > 0

    # Query results
    frames = get_frames(recorder.run_id, limit=100)
    assert any(f.function_name == 'target' for f in frames)

    recorder.cleanup()
```

### VSCode Extension Test Conventions

1. **Install vscode mock** before importing modules: `installMock()` in test setup
2. **Use `MockRpcServer`** for backend connection tests
3. **Access protected methods** via `(session as any).methodName()`
4. **Wait for async operations**: use `waitFor()` helper for TCP race conditions

## Benchmarks

Performance benchmarks live in `benchmarks/`:

```bash
# Component benchmarks (warm nav, timeline, DB size, stack, variables, flush)
.venv/bin/pytest benchmarks/ --benchmark-only -v

# Recording overhead + RSS measurement
.venv/bin/python3 benchmarks/bench_overhead.py -n 5

# Update BENCHMARKS.md with fresh results
.venv/bin/python3 benchmarks/bench_overhead.py -n 5 --output BENCHMARKS.md
```

See [BENCHMARKS.md](../../BENCHMARKS.md) for current results and performance targets.

## CI

GitHub Actions CI runs on every push and PR:

- **Test matrix**: Python 3.12 + 3.13 on Linux + macOS
- **ASAN build**: Address Sanitizer on Linux
- **sdist smoke test**: build source distribution and verify it installs
- **VSIX build**: compile and package the VSCode extension

See `.github/workflows/ci.yml` for the full configuration.

## See Also

- [Building](building.md) — build instructions and prerequisites
- [C Extension Guide](c-extension.md) — debugging C extension issues
- [Contributing](../../CONTRIBUTING.md) — development workflow
