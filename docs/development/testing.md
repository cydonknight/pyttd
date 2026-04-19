# Testing Guide

## Running Tests

### Python tests (~607 tests across ~55 modules)

```bash
# Run all tests
.venv/bin/pytest tests/ -v

# Quick (no verbose)
.venv/bin/pytest tests/ -q

# Run a specific test file
.venv/bin/pytest tests/test_recorder.py -v

# Run a specific test
.venv/bin/pytest tests/test_recorder.py::TestRecorder::test_basic_recording -v

# List all collected tests (no execution)
.venv/bin/pytest tests/ --collect-only -q
```

### VSCode extension tests (~95 Mocha tests)

```bash
cd vscode-pyttd
npm test    # Compiles TypeScript, then runs Mocha
```

### Test coverage overview

The Python test suite covers, broadly:

- **Recorder / C extension** — ring buffer flush, sequence monotonicity, call/line/return/exception events, locals capture, call-depth tracking, repr reentrancy guard, adaptive sampling.
- **Navigation** — step into/over/out/back, continue / reverse continue, goto_frame (warm + cold), goto_targets, restart_frame, stack reconstruction with cache.
- **Breakpoints** — line, conditional, function, data, hit-count, log points. Condition error surfacing.
- **Variables** — flat repr, expandable containers (dict/list/tuple/set/NamedTuple/dataclass), variable history, expression watchpoints (`find_expression_matches`).
- **Checkpoints** — fork-based creation, smallest-gap eviction, RSS tracking, memory-aware eviction, multi-thread skip guard, `arm(checkpoints=True)` opt-in.
- **I/O hooks** — serialization of `time.*`, `random.*`, `os.urandom`, `datetime.*`, `uuid.*`; replay mode in checkpoint children.
- **Multi-thread recording** — per-thread ring buffers, global sequence ordering, per-thread stacks, thread-aware navigation.
- **Live debugging** — pause / resume, `continue_from_past` branching, variable modification at pause boundaries.
- **CLI** — every subcommand (`record`, `query`, `replay`, `serve`, `export`, `clean`, `diff`, `ci`), interactive REPL commands, exit codes.
- **pytest plugin** — `--pyttd`, `--pyttd-on-fail`, `--pyttd-replay`, artifact eviction, manifest.
- **Storage / models** — schema migrations, `pyttd_meta` versioning, lazy secondary index build, run eviction.
- **Secrets redaction** — word-boundary matching, container-level redaction (dict values, NamedTuple fields), sticky return redaction.

Get the up-to-date test list with:

```bash
.venv/bin/pytest tests/ --collect-only -q | tail -n +1
```

## Test fixtures (`tests/conftest.py`)

| Fixture | Scope | Description |
|---------|-------|-------------|
| `db_path` | function | Temporary `.pyttd.db` path via `tmp_path` |
| `db_setup` | function | Connects to DB, creates schema, yields, closes |
| `record_func` | function | Helper that takes a script string, records it, returns `(db_path, run_id, stats)`. Accepts `checkpoint_interval` kwarg. Auto-cleans checkpoints on teardown |
| `_reset_trace_state` | function (autouse) | Resets `sys.settrace(None)` and `sys.monitoring.restart_events()` to prevent `PyEval_SetTrace` pollution between tests |

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
# In-process recording throughput (hot path only, no subprocess)
.venv/bin/pytest benchmarks/bench_recording_inprocess.py -v -s

# Locals serialization scaling (by count, type, container size)
.venv/bin/pytest benchmarks/bench_locals_scaling.py -v -s

# Recorder microbenchmarks (per-event cost slope, adaptive sampling, return-only)
.venv/bin/pytest benchmarks/bench_recorder_micro.py -v -s

# Component benchmarks (warm nav, timeline, DB size, stack, variables, flush)
.venv/bin/pytest benchmarks/bench_components.py --benchmark-only -v

# Subprocess recording overhead + RSS measurement (what users see)
.venv/bin/python3 benchmarks/bench_overhead.py -n 3

# Scaled subprocess benchmarks (hot-path-dominated ratios, longer workloads)
.venv/bin/python3 benchmarks/bench_overhead_scaled.py -n 3
```

See [BENCHMARKS.md](../../BENCHMARKS.md) for current results, performance
targets, and the methodology section distinguishing in-process / default
subprocess / scaled subprocess measurements.

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
