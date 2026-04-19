# pytest Integration

pyttd ships with a pytest plugin that records time-travel traces for test
runs. The plugin is auto-discovered via `pyproject.toml` entry points — no
configuration needed beyond installing pyttd with `pip install py-tt-debug`.

## Quick start

```bash
# Record every test. Per-test traces land in .pyttd-artifacts/.
pytest --pyttd

# Record all tests; keep only failing traces on disk.
pytest --pyttd-on-fail

# Launch interactive replay for the most recent failing test.
pytest --pyttd-replay
```

After a failing test run, download/open the artifact directory:

```bash
ls .pyttd-artifacts/
# tests_test_foo__test_bar__a3b2c1.pyttd.db
# MANIFEST.json
```

Replay the failure with:

```bash
pyttd replay --interactive --db .pyttd-artifacts/tests_test_foo__test_bar__a3b2c1.pyttd.db
```

or re-run `pytest --pyttd-replay` to jump into the most recent failure.

## How it works

The plugin registers three pytest hooks:

- `pytest_runtest_setup` — calls `pyttd.arm()` with a per-test DB path derived
  from the test nodeid (hashed for filename uniqueness across parametrized tests).
- `pytest_runtest_makereport` — captures the test's pass/fail status from the
  call-phase report and stashes it on the item.
- `pytest_runtest_teardown` — calls `pyttd.disarm()`, then records an entry
  in the manifest. In `--pyttd-on-fail` mode, passing tests' DBs are deleted
  at this point.

Attach mode (`arm()` / `disarm()`) is used rather than subprocess recording
because tests run in-process under pytest — `arm()` is the exact primitive
that starts/stops recording in a running process.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--pyttd` | off | Record every test |
| `--pyttd-on-fail` | off | Record every test, keep only failing traces |
| `--pyttd-replay` | off | Launch interactive replay for the most recent failing test |
| `--pyttd-artifact-dir DIR` | `.pyttd-artifacts` | Where to write traces |
| `--pyttd-keep N` | `10` | Retain last N recordings, evict older |
| `--pyttd-max-db-size MB` | `100` | Per-test DB size cap |
| `--pyttd-include FUNC` | — | Restrict recording to matching functions (glob; repeatable) |
| `--pyttd-exclude FUNC` | — | Exclude matching functions (repeatable) |

Inactive when no `--pyttd*` flag is passed — zero overhead for normal `pytest` runs.

## Artifact layout

```
.pyttd-artifacts/
├── MANIFEST.json
├── tests_test_foo__test_bar__a3b2c1.pyttd.db
├── tests_test_foo__test_baz__b4c3d2.pyttd.db
└── ...
```

`MANIFEST.json` is a structured index:

```json
{
    "version": 1,
    "session_id": "2026-04-12T14:30:00",
    "tests": [
        {
            "nodeid": "tests/test_foo.py::test_bar",
            "hash": "a3b2c1",
            "db_path": ".pyttd-artifacts/tests_test_foo__test_bar__a3b2c1.pyttd.db",
            "status": "failed",
            "duration_s": 0.42,
            "exception": "AssertionError: ...",
            "timestamp": 1712847012.34
        }
    ]
}
```

Artifact eviction runs on each pytest startup: the oldest entries beyond
`--pyttd-keep` are deleted (DB, WAL, SHM, and manifest entry).

## Parametrized tests

Each parametrized variant gets its own DB — the nodeid hash disambiguates:

```python
@pytest.mark.parametrize("x", [1, 2, 3])
def test_param(x):
    ...
```

Produces `tests_test_foo__test_param_1__xxxxxx.pyttd.db`,
`..._2_.pyttd.db`, `..._3_.pyttd.db`.

## Scoping recording (performance)

By default, pytest tests record every frame event including library code
(pytest plugins, fixtures, user imports). For compute-heavy tests this can
slow the suite 10-50x.

Use `--pyttd-include` to scope recording to your module under test:

```bash
pytest --pyttd-on-fail --pyttd-include 'my_module.*'
```

Or scope per-test with a conftest fixture:

```python
# conftest.py
@pytest.fixture(autouse=True)
def scope_pyttd_recording(request):
    # Handle scoping here if you want per-test include patterns
    ...
```

## CI integration (GitHub Actions)

```yaml
- name: Run tests with pyttd on failure
  run: pytest --pyttd-on-fail

- name: Upload pyttd traces
  if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: pyttd-traces
    path: .pyttd-artifacts/
```

Alternatively, combine with `pyttd ci` to auto-gzip:

```yaml
- run: pyttd ci -- pytest tests/ --pyttd-on-fail
- if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: pyttd-traces
    path: |
      .pyttd-artifacts/
      .pyttd-ci-artifacts/*.pyttd.db.gz
```

## Troubleshooting

### "pyttd: Failed to arm recording for test_foo: Recording is already active"

You've got nested `pyttd.arm()` calls. Make sure no test fixture or the test
body itself calls `arm()` — the plugin owns the recording lifecycle.

### "pytest --pyttd-replay" doesn't find a recording

Check that `--pyttd-artifact-dir` is the same directory used for the
recording run, and that a failing test exists in `MANIFEST.json`. If running
against a CI artifact, pass the explicit path:

```bash
pytest --pyttd-replay --pyttd-artifact-dir ./downloaded-artifacts/
```

### `pytest-xdist` compatibility

Each xdist worker writes to its own DB file (the nodeid hash makes filenames
unique). The manifest is a single file, so concurrent writes from different
workers can race. For `--pyttd-on-fail` on xdist runs, prefer running serially
(`-p no:xdist`) or per-worker subdirectories.

## See Also

- [CLI Reference](cli-reference.md) — `pyttd ci` wrapper details
- [Getting Started](getting-started.md) — basic recording workflow
- [Troubleshooting](troubleshooting.md) — general debugging issues
