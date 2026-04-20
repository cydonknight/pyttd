# pyttd

[![CI](https://github.com/cydonknight/pyttd/actions/workflows/ci.yml/badge.svg)](https://github.com/cydonknight/pyttd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**pyttd** (Python Time-Travel Debugger) is an open-source time-travel debugger for Python with full VSCode integration. It records complete program execution at the C level, then lets you step backward and forward, jump to any point in the trace, and visually scrub through a timeline — all from within your editor.

## What is Time-Travel Debugging?

Traditional debuggers only move forward. If you step past a bug, you start over. pyttd records every execution frame during a single run, then drops you into a **replay session** where you can navigate freely in both directions:

- **Step backward** through execution to see exactly how state evolved
- **Reverse continue** to find the last time a breakpoint was hit
- **Jump to any frame** in the entire recording
- **Scrub a visual timeline** to navigate through call depth, exceptions, and execution flow
- **Inspect variables** at any point without re-running the program
- **Track variable history** across the entire recording
- **Set conditional, function, data, and log breakpoints** — all work in both directions
- **Attach to running processes** to start recording mid-execution
- **Pause mid-execution** to inspect state and navigate history, then resume recording
- **Resume from any checkpoint** — navigate backward to a historical point, press Continue, and the debugger resumes live execution from that point on a branched timeline (Unix only)
- **Modify variables while paused** — change a variable's value mid-execution and resume to see "what if" scenarios; expressions evaluated with restricted builtins for safety
- **Export traces** to Perfetto for external analysis

pyttd supports both **post-mortem replay** (script runs to completion, then you debug the trace) and **live pause-and-inspect** (pause a running script mid-execution, navigate the recorded history, resume recording, or resume from a historical checkpoint to fork the execution timeline).

## Installation

> **Requires Python 3.12+** — uses CPython C API features introduced in 3.12.

```bash
pip install py-tt-debug    # PyPI package name
import pyttd               # Python import name
```

The PyPI package is `py-tt-debug`; the importable module is `pyttd`. The CLI command is `pyttd`.

Or from source:

```bash
git clone https://github.com/cydonknight/pyttd.git
cd pyttd
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Quick Start

### CLI

```bash
# Record a script
pyttd record examples/hello.py

# Record with arguments
pyttd record examples/hello.py --args --verbose --count 5

# Record only specific functions
pyttd record examples/hello.py --include process_data --include validate

# Query the recording
pyttd query --last-run --frames

# Search for a function across the trace
pyttd query --last-run --search process_data

# List all runs in a database
pyttd query --list-runs --db hello.pyttd.db

# Replay and jump to a specific frame
pyttd replay --last-run --goto-frame 750

# Export to Perfetto trace format
pyttd export --db hello.pyttd.db -o trace.json
# Open trace.json at https://ui.perfetto.dev

# Clean up database files
pyttd clean --all --dry-run

# Diff two runs to find divergence
pyttd diff --runs abc123 def456 --db script.pyttd.db

# CI wrapper — preserve trace on failure
pyttd ci -- pytest tests/
```

### pytest Integration

pyttd ships as a pytest plugin (auto-discovered on install):

```bash
# Record every test
pytest --pyttd

# Record all tests; keep only failing traces
pytest --pyttd-on-fail

# Replay the most recent failure interactively
pytest --pyttd-replay
```

Artifacts are written to `.pyttd-artifacts/` with a JSON manifest. Options:
- `--pyttd-artifact-dir DIR` — custom artifact directory
- `--pyttd-keep N` — retain last N recordings (default: 10)
- `--pyttd-max-db-size MB` — per-test size cap (default: 100)
- `--pyttd-include FUNC` / `--pyttd-exclude FUNC` — scope recording

### VSCode

1. Build the extension: `cd vscode-pyttd && npm install && npm run package`
2. In VSCode: Extensions sidebar → `...` → **Install from VSIX** → select `pyttd-*.vsix`
3. Add to `.vscode/launch.json`:

```json
{
    "type": "pyttd",
    "request": "launch",
    "name": "Time-Travel Debug",
    "program": "${file}"
}
```

4. Press **F5** — your script records, then you navigate freely

To replay an existing recording without re-running, use an `attach` configuration:

```json
{
    "type": "pyttd",
    "request": "attach",
    "name": "Replay Trace",
    "traceDb": "${workspaceFolder}/script.pyttd.db"
}
```

### Python API

```python
from pyttd import ttdbg

@ttdbg
def my_function():
    x = 42
    return x * 2

my_function()  # Records to <file>.pyttd.db
```

Or with explicit start/stop control:

```python
from pyttd import start_recording, stop_recording

start_recording(db_path="trace.pyttd.db")
# NOTE: Only function calls made here are recorded.
# Inline code (x = 42) is NOT captured. Use arm() for inline code.
my_function()
stats = stop_recording()
```

### Attach to a Running Process

```python
from pyttd import arm, disarm

# Start recording mid-execution
arm()
suspect_function()
stats = disarm()  # Returns recording stats

# Or use as a context manager (stats available after exit)
with arm() as ctx:
    suspect_function()
print(ctx.stats)  # {'frame_count': 42, 'elapsed_time': 0.01, ...}

# Or toggle via signal (kill -USR1 <pid>)
from pyttd import install_signal_handler
install_signal_handler()  # First USR1 arms, second disarms
```

## Features

### Recording
- C extension recorder using PEP 523 frame eval hooks (not `sys.settrace`)
- Binary log recording — buffered `fwrite` during execution, bulk SQLite load at stop; no Python objects in the flush path
- Lock-free per-thread SPSC ring buffers with background flush
- Fork-based checkpointing for fast cold navigation (Linux/macOS)
- Multi-thread recording with globally ordered sequence numbers
- Async/await and generator support with coroutine frame tracking
- I/O hooks for deterministic checkpoint replay — `time.time`, `time.monotonic`, `time.perf_counter`, `time.sleep`, `random.random`, `random.randint`, `random.uniform`, `random.gauss`, `random.choice`, `random.sample`, `random.shuffle`, `os.urandom`, `uuid.uuid4`, `uuid.uuid1`, `datetime.datetime.now`, `datetime.datetime.utcnow`
- Secrets filtering — sensitive variable names (`password`, `token`, `secret`, `api_key`, `connection_string`, `database_url`, `dsn`, etc.) automatically redacted during recording with word-boundary matching (catches `auth_token` but not `authenticate`); container-level redaction (dict values and NamedTuple fields with secret-named keys are also scrubbed); configurable patterns with `--secret-patterns`
- Selective recording — `--include` / `--exclude` filter by function name (matches both qualified and bare names, e.g. `--include failing` matches `main.<locals>.failing`), `--include-file` / `--exclude-file` filter by source file path (glob patterns where `*` matches across `/`)
- Expandable variable trees — dicts, lists, tuples, sets, objects with `__dict__`, and `__slots__`-based classes (including `@dataclass(slots=True)` and `NamedTuple` with field names) are serialized with structure, not just `repr()`; nested containers are expandable to arbitrary depth via `ast.literal_eval`-based parsing of child repr strings
- Runtime attach — `arm()` / `disarm()` API to start/stop recording from within a running process, or toggle via Unix signal
- Live pause-and-inspect — pause a running script at the next line boundary, snapshot the binary log into SQLite for navigation, step backward through history, then resume recording; the recording thread releases the GIL during pause so the server can operate
- Checkpoint memory tracking and configurable limits

### Navigation
- Forward: step into/over/out, continue with breakpoints
- Reverse: step back, reverse continue with breakpoints and exception filters
- Conditional breakpoints — expressions evaluated against frame locals
- Hit-count breakpoints — stop after N hits (supports `>=N`, `>N`, `<=N`, `<N`, `==N`, `%N`)
- Log points — emit structured log messages with variable interpolation without stopping
- Function breakpoints — break on any call to a named function
- Data breakpoints — break when a variable's value changes
- Jump: goto frame, goto targets (all executions of a line), restart frame
- Resume from past — while paused, navigate backward to a checkpoint and press Continue to fork execution from that point; the checkpoint child takes over the live process with a new branched timeline (Unix only, requires checkpoints)
- Variable modification — while paused, change any local variable's value via the Variables panel or Debug Console; restricted eval prevents dangerous expressions (`import`, `exec`, `open` blocked)
- Warm navigation (SQLite, sub-ms) for stepping; cold navigation (checkpoint restore, 50-300ms) for jumps

### VSCode Extension
- Full DAP implementation with step-back and reverse-continue
- Canvas-based timeline scrubber with click/drag/zoom
- CodeLens annotations showing call and exception counts per function
- Inline variable values during stepping
- Call history tree with lazy-loaded nesting, exception markers, and coroutine suspend/resume merging (consecutive await cycles shown as a single entry with suspend count)
- Exception breakpoint filters (uncaught, all raised)
- Function breakpoints, data breakpoints, conditional breakpoints, hit conditions, log points
- Variable history webview — canvas chart for numeric values, HTML table for non-numeric, click-to-navigate; accessible from Variables panel context menu
- Breakpoint verification — validates that breakpoints target executable lines; condition eval errors shown in Debug Console
- Live pause — click Pause during recording to suspend execution, inspect variables, navigate backward, then resume with the Resume Recording command (Ctrl+Shift+F6)
- Variable modification — edit variable values in the Variables panel while paused; changes take effect on resume
- Status bar with recording progress (frame count, dropped frames, DB size, checkpoint count and memory); shows "Paused" state during live pause
- Keyboard shortcuts: Ctrl+Shift+F11 (step back), Ctrl+Shift+F5 (reverse continue), Ctrl+Shift+F6 (resume recording)

### Analysis & Export
- Perfetto/Chrome Trace Event Format export — viewable in [ui.perfetto.dev](https://ui.perfetto.dev); preserves multi-thread structure
- Variable history queries — track how a variable changes over time, with deduplication
- Execution stats — per-function call counts, exception counts, and entry points
- Checkpoint memory profiling — per-checkpoint RSS tracking
- **Trace diff** — compare two recording runs to find the earliest control-flow or data divergence; best-effort alignment with single-step resync lookahead

### Testing & CI Integration
- **pytest plugin** — `--pyttd` records every test, `--pyttd-on-fail` keeps only failing traces, `--pyttd-replay` opens interactive replay of the last failure; artifact management with configurable eviction
- **CI wrapper** — `pyttd ci -- <command>` automatically wraps Python commands with `pyttd record`, preserves `.pyttd.db` artifacts on failure, auto-gzips for upload; exits with the original command's exit code
- **Exception chain summary** — `pyttd record` now shows the full exception propagation chain (raise site through propagation frames) instead of a single frame; coroutine noise (`StopIteration`/`StopAsyncIteration`) is automatically filtered

### Database Management
- Multi-run storage — multiple recording runs in a single database
- Run eviction — `--keep-runs N` auto-evicts old runs; `pyttd clean --keep N` for manual cleanup
- Custom DB paths — `--db-path` overrides the default `<script>.pyttd.db` location
- Size monitoring — `--max-db-size` auto-stops recording when the database exceeds a threshold
- Run selection — `--run-id` to query, replay, or export a specific run by UUID or prefix

## Comparison with Other Python Debuggers

| Feature | pdb | debugpy (VSCode) | PyCharm | pudb | pyttd |
|---------|:---:|:---:|:---:|:---:|:---:|
| Step forward (into/over/out) | Yes | Yes | Yes | Yes | Yes |
| **Step backward** | - | - | - | - | **Yes** |
| **Reverse continue** | - | - | - | - | **Yes** |
| Conditional breakpoints | Yes | Yes | Yes | Yes | Yes |
| Hit-count breakpoints | Partial | Yes | - | Partial | Yes |
| Log points | Partial | Yes | Yes | - | Yes |
| Function breakpoints | Yes | Yes | - | Yes | Yes |
| **Data breakpoints** | - | - | - | - | **Yes** |
| **Time-travel replay** | - | - | - | - | **Yes** |
| **Visual timeline scrubber** | - | - | - | - | **Yes** |
| Variable modification | Yes | Yes | Yes | Yes | Yes |
| Expression evaluation | Yes | Yes | Yes | Yes | Yes |
| Multi-thread support | - | Yes | Yes | Partial | Yes |
| Async/await support | - | Partial | Yes | - | Yes |
| Attach to running process | Partial | Yes | Yes | Partial | Yes |
| VSCode integration | - | Yes | - | - | Yes |
| CLI interface | Yes | Yes | - | Yes | Yes |
| **Record and replay** | - | - | - | - | **Yes** |
| **Export traces (Perfetto)** | - | - | - | - | **Yes** |
| **C-level recording** | - | - | - | - | **Yes** |
| **Checkpoint navigation** | - | - | - | - | **Yes** |
| **Live pause + history nav** | - | Partial | Partial | - | **Yes** |
| **Resume from past** | - | - | - | - | **Yes** |

**Key:** Yes = full support, Partial = limited support, `-` = not supported. Bold features are unique to pyttd.

**Notes:**
- **pdb** uses `sys.settrace`; no reverse execution or recording. Hit-count via `ignore N` only.
- **debugpy** is Microsoft's DAP debugger for VSCode. Supports pause but not history navigation while paused. No data breakpoints ([open request](https://github.com/microsoft/debugpy/issues/1317)).
- **PyCharm** has a powerful GUI debugger but no time-travel, data breakpoints, or function breakpoints by name. Async support added in 2026.1 via PEP 669.
- **pudb** is a curses-based TUI debugger built on `bdb`/`sys.settrace`. No async or multi-thread support.
- **pyttd** records at the C level via PEP 523 eval hooks (not `sys.settrace`), enabling post-mortem replay, reverse debugging, and checkpoint-based navigation that other Python debuggers cannot provide.

### Performance Comparison

Published benchmark data for Python debuggers is sparse and measured under varying conditions, making direct comparison difficult. The table below collects available numbers alongside pyttd's own benchmarks.

| Debugger | Mechanism | Typical Overhead | Conditions | Source |
|----------|-----------|-----------------|------------|--------|
| pdb | `sys.settrace` | ~5–6x † | No published benchmarks | — |
| debugpy | `sys.settrace` / frame eval | 2–3x | Nested loops, Python 3.10 | [debugpy #1378](https://github.com/microsoft/debugpy/issues/1378) |
| debugpy | `sys.monitoring` (3.12+) | ~1x between breakpoints | Near-zero cost between breakpoints; settrace-like during stepping | [PyDev blog](https://pydev.blogspot.com/2024/02/pydev-debugger-and-sysmonitoring-pep.html) |
| PyCharm | pydevd + Cython | 2–3x | Same engine as debugpy | [JetBrains blog](https://blog.jetbrains.com/pycharm/2016/02/faster-debugger-in-pycharm-5-1/) |
| PyCharm | `sys.monitoring` (2026.1+) | ~1x between breakpoints | Near-zero cost between breakpoints; settrace-like during stepping | [JetBrains blog](https://blog.jetbrains.com/pycharm/2024/01/new-low-impact-monitoring-api-in-python-3-12/) |
| pudb | `sys.settrace` | ~5–6x † | No published benchmarks | — |
| pyttd | PEP 523 eval hook + C trace | 4–57x (compute-bound, hot-path dominated) | Recording **every** frame event; `--include` scoping recommended to drop overhead 10-100x (see [Performance](#performance)) | This project |

† Estimated from `sys.settrace` empty-callback overhead ([debugpy #204](https://github.com/microsoft/debugpy/issues/204)). Actual debugger overhead is higher due to breakpoint evaluation and variable inspection.

**Key differences in what's being measured:**

- **Traditional debuggers** (pdb, debugpy, PyCharm, pudb) add overhead only during active debugging sessions. With `sys.monitoring` (Python 3.12+), overhead between breakpoints approaches zero.
- **pyttd** records every frame event (every line, call, return, and exception) to produce a complete trace for post-mortem replay. This captures far more data than a traditional debugger, which is why compute-bound overhead is higher. The tradeoff is full reverse debugging and time-travel navigation after a single run.
- **No memory benchmarks** have been published for pdb, debugpy, PyCharm, or pudb.

## CLI Reference

```
pyttd [--version] [-v|--verbose] <command>

Commands:
  record    Record script execution
  query     Query trace data
  replay    Replay a recorded session
  serve     Start JSON-RPC debug server (used by VSCode)
  export    Export trace data
  clean     Clean up database files
  ci        CI wrapper — run command, preserve trace on failure
  diff      Diff two recording runs to find divergence
```

### record

```bash
pyttd record script.py [options]

Options:
  --module                          Treat argument as a module name (e.g., pkg.mod)
  --checkpoint-interval N           Frames between checkpoints (default: 1000)
  --args VALUE [VALUE ...]          Arguments to pass to the script
  --no-redact                       Disable secrets redaction
  --secret-patterns PAT             Additional pattern to redact (repeatable)
  --include FUNC                    Only record functions matching this pattern (repeatable; matches bare name too)
  --include-file GLOB               Only record functions in files matching this glob (* matches /) (repeatable)
  --exclude FUNC                    Exclude functions matching this pattern (repeatable; matches bare name too)
  --exclude-file GLOB               Exclude files matching this glob (* matches /) (repeatable)
  --max-frames N                    Stop recording after approximately N frames (0 = unlimited)
  --db-path PATH                    Custom database path (default: <script>.pyttd.db)
  --max-db-size MB                  Auto-stop recording when DB exceeds this size in MB (0 = unlimited)
  --keep-runs N                     Keep only last N runs, evict older (0 = keep all)
  --checkpoint-memory-limit MB      Checkpoint memory limit in MB (0 = unlimited)
  --env KEY=VALUE [...]             Environment variables for the script (must be after script path)
  --env-file PATH                   Load environment variables from a dotenv file
```

### query

```bash
pyttd query [--last-run | --run-id UUID] [--list-runs] [--frames] [--limit N]
    [--search PATTERN] [--thread THREAD_ID] [--list-threads]
    [--show-locals] [--changed-only] [--var-history VAR] [--stats]
    [--exceptions] [--event-type TYPE] [--line [FILE:]N] [--file FILE]
    [--offset N] [--format text|json] [--hide-coroutine-internals]
    [--db path.pyttd.db]

Options:
  --show-locals                     Display variable values alongside each frame
  --changed-only                    With --show-locals, only show vars that changed
  --var-history VAR                 Track how a variable changes over the recording
  --stats                           Show per-function call and exception counts
  --exceptions                      Show only exception_unwind events
  --event-type TYPE                 Filter by event type (call, line, return, exception, exception_unwind)
  --line [FILE:]N                   Show all executions of a specific line
  --file FILE                       Filter frames by source filename (substring match)
  --offset N                        Skip first N frames (use with --limit for pagination)
  --format text|json                Output format (default: text); JSON includes locals when --show-locals
  --hide-coroutine-internals        Hide coroutine-internal exception events (async StopIteration noise)
```

### replay

```bash
pyttd replay [--last-run | --run-id UUID] [--goto-frame N] [--goto-line FILE:LINE]
    [--interactive] [--db path.pyttd.db]
```

Note: The CLI `replay` command uses warm navigation only (SQLite reads). Cold navigation via checkpoint restore is available through the VSCode extension (JSON-RPC server), which keeps checkpoint children alive during the debug session.

**Interactive replay commands** (`--interactive`). The REPL supports **readline history** (persists across sessions at `~/.pyttd_history`), **tab completion** for commands, function names, filenames, and variable names, and **Ctrl+R** reverse search. Degrades gracefully on platforms without readline.

| Command | Aliases | Description |
|---------|---------|-------------|
| `step` | `s`, `step_into` | Step to next line event |
| `next` | `n` | Step over function calls |
| `back` | `b`, `step_back` | Step backward |
| `out` | `o`, `step_out` | Step out of current function |
| `continue` | `c` | Continue to next breakpoint or end |
| `rcontinue` | `rc`, `reverse_continue` | Reverse continue to previous breakpoint or start |
| `goto N` | `frame N` | Jump to frame N (also: `goto first`, `goto last`) |
| `vars` | `v`, `locals`, `info` | Show variables at current frame |
| `eval EXPR` | `print EXPR`, `p EXPR` | Evaluate expression against locals |
| `where` | `w`, `bt`, `stack`, `backtrace` | Show call stack |
| `watch VAR` | | Show variable history |
| `search PAT` | | Find frames matching pattern |
| `break F:L` | | Set line breakpoint |
| `break FUNC` | | Set function breakpoint |
| `logpoint F:L MSG` | | Log message on hit without stopping |
| `breaks` | | List breakpoints |
| `delete [N]` | | Delete breakpoint N or all |

### serve

```bash
# Record and serve (used by VSCode extension)
pyttd serve --script script.py [--module] [--cwd DIR] [--checkpoint-interval N]
    [--include FUNC] [--exclude FUNC] [--include-file GLOB] [--exclude-file GLOB]
    [--max-frames N] [--env KEY=VALUE ...] [--env-file .env]
    [--db-path PATH] [--max-db-size MB] [--keep-runs N]

# Replay existing recording (no re-recording)
pyttd serve --db path.pyttd.db [--run-id UUID]
```

### export

```bash
pyttd export --format perfetto --db path.pyttd.db [--run-id UUID] -o trace.json
```

### clean

```bash
pyttd clean [--db path.pyttd.db] [--all] [--keep N] [--dry-run]

Options:
  --db PATH      Specific database file to clean
  --all          Delete all .pyttd.db files in current directory
  --keep N       Keep last N runs, evict the rest
  --dry-run      Show what would be deleted without deleting
```

### ci

```bash
pyttd ci [options] -- <command> [args...]

Options:
  --artifact-dir DIR       Where to write trace files (default: .pyttd-ci-artifacts/)
  --keep-on-success        Keep artifacts even on success (default: delete)
  --compress / --no-compress  Gzip artifacts for smaller uploads (default: on)
  --max-size-mb MB         Max per-recording DB size in MB (default: 500)
  --no-record              Disable auto-wrapping with pyttd record (env-variable mode only)
```

Python commands are automatically wrapped with `pyttd record` for seamless trace capture. Non-Python commands use environment variables (`PYTTD_DB_PATH`, `PYTTD_ARM_SIGNAL`) and require the child command to be pyttd-aware (e.g., `pytest --pyttd`). Use `--no-record` to force env-variable mode.

Example GitHub Actions integration:

```yaml
- run: pyttd ci -- python tests/integration.py
- if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: pyttd-trace
    path: .pyttd-ci-artifacts/*.pyttd.db.gz
```

### diff

```bash
pyttd diff --runs RUN_A RUN_B --db path.pyttd.db [options]

Options:
  --context N              Lines of context around divergence (default: 3)
  --ignore-vars VAR        Variable names to skip when comparing locals (repeatable)
  --format text|json       Output format (default: text)
```

Compares two recording runs and finds the earliest point where execution diverges — either a control-flow split (different branches taken) or a data split (same line, different variable values). Best-effort alignment with single-step resync lookahead; memory addresses and ephemeral objects (functions, modules) are normalized automatically.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `PYTTD_RECORDING` | Set to `1` during active recording; scripts can check `os.environ.get('PYTTD_RECORDING')` |
| `PYTTD_ARM_SIGNAL` | Auto-install signal handler on import — e.g., `PYTTD_ARM_SIGNAL=USR1` installs a SIGUSR1 toggle handler. **Requires `import pyttd` in the target script.** |

## Performance

**Recording every frame event is expensive.** On compute-heavy code, pyttd adds **~40-57x wall-clock slowdown** to the recorded portion. This is the honest cost of capturing every line, call, return, and exception so you can replay and navigate freely afterward.

**You almost always want `--include` scoping.** For typical applications, restricting recording to the specific module or function under investigation cuts overhead by 10-100x and keeps DB size manageable. Treat unscoped recording as a diagnostic of last resort, not a default.

### Honest subprocess slowdown (hot-path dominated)

These numbers use workloads sized so the recorded portion runs 1-5 seconds. Subprocess startup (~40ms) and binlog finalization amortize to under 5% of total time, so the ratio reflects the actual per-event recording cost.

| Workload | Recorded portion | Slowdown |
|----------|-----------------:|---------:|
| Tight loop (25M iterations) | 4.6s | 4.0x |
| Multi-thread (100K per thread) | 0.7s | 28.1x |
| Many locals (20K iter × 20 vars) | 0.8s | 39.9x |
| Deep recursion (1500 depth) | 1.8s | 40.1x |
| Compute-bound (scale 100) | 3.1s | 56.6x |

The compute-bound 57x is the ceiling for a workload that does nothing but function calls and arithmetic — every line is a new event. Real code spends time in I/O, syscalls, and library code (which is filtered out by default), so real-world overhead sits between the tight-loop number (4x, mostly skipped by adaptive sampling) and the compute ceiling.

### When `--include` scoping matters

```bash
# Bad: 57x slowdown, 14MB DB for a 0.02s workload
pyttd record heavy_script.py

# Good: records only the function you're investigating
pyttd record heavy_script.py --include compute_metrics

# Good: scope by file glob
pyttd record heavy_script.py --include-file '*/billing/*.py'

# Pytest integration: record only the failing test's own code
pytest --pyttd-on-fail --pyttd-include 'my_module.*'
```

With realistic scoping, overhead on a typical data-processing pipeline drops to 2-5x — low enough to leave on for development and CI. See [docs/getting-started.md](docs/getting-started.md) for worked examples.

### Short subprocess timing (what `pyttd record small_script.py` takes)

For small scripts, subprocess startup and one-time DB finalization dominate. These are the numbers you see on the command line but they don't reflect per-event cost:

| Workload | Baseline | Recorded | Slowdown | Peak RSS |
|----------|---------:|---------:|---------:|---------:|
| I/O-bound | 0.169s | 0.229s | 1.4x | 27 MB |
| Compute-bound (trivial) | 0.021s | 0.187s | 8.8x | 43 MB |
| Tight loop (10K iter) | 0.017s | 0.090s | 5.2x | 43 MB |
| Deep recursion (500 depth) | 0.017s | 0.048s | 2.9x | 43 MB |
| Many locals (20 vars) | 0.017s | 0.049s | 2.9x | 43 MB |
| Multi-thread (4 threads) | 0.018s | 0.066s | 3.7x | 46 MB |

For the 0.021s compute-bound baseline above, ~40ms of the "recorded" time is pyttd's own import and C extension load — not recording overhead. The same workload scaled up shows 56.6x (above), which is the honest per-event cost.

### In-process throughput (pure recording cost, no subprocess)

Measured via the `bench_record` fixture — no startup, no DB load. These are the absolute hot-path numbers for per-event optimization work.

| Workload | us/event | events/s |
|----------|---------:|---------:|
| Many short calls (10K) | 0.7 | 1.48M |
| Tight loop (50K iterations) | 0.1 | 17.6M |
| Deep recursion (200×50) | 0.2 | 4.96M |
| Mixed types (7 types) | 0.7 | 1.46M |
| Large locals (50 vars) | 1.8 | 542K |
| Expandable vars | 1.1 | 913K |

### Memory

Peak RSS stays under 50 MB for all subprocess workloads. Checkpoint memory is configurable via `--checkpoint-memory-limit MB` and enforced by memory-aware eviction. DB size scales linearly with event count: ~200-1000 bytes per event depending on workload shape.

### How recording works (briefly)

pyttd records to a binary log with buffered writes during execution, then bulk-loads into SQLite at stop time. No Python objects are allocated in the flush path. Zero external runtime dependencies. The hot path uses:

- Fast-path formatting for int/float/bool/None (bypassing `PyObject_Repr()`)
- Return-only serialization when the previous line event already captured locals
- Adaptive sampling that reduces capture frequency in long-running frames
- A lazy secondary-index build — `pyttd record` exits as soon as the binlog loads; indexes build on first `pyttd query` / `pyttd replay` / `pyttd diff`

See [BENCHMARKS.md](BENCHMARKS.md) for the full benchmark suite, phase-by-phase breakdown of where subprocess time goes, locals scaling curves, and navigation/query performance.

### Comparison context

Traditional debuggers (pdb, debugpy, PyCharm) add overhead only during active stepping; with `sys.monitoring` on Python 3.12+ they approach zero cost between breakpoints. pyttd records **every** frame event to produce a complete replayable trace. The ratio is higher because you're capturing fundamentally more data — in exchange you get reverse debugging, time-travel navigation, and post-mortem replay that those tools can't offer at all. Use pyttd when you want the trace; use a traditional debugger when you want fast forward-only stepping.

All numbers captured on Apple M-series with Python 3.13.7. Results vary by platform, CPU, and workload shape.

## Architecture

Three-layer system:

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| C Extension (`pyttd_native`) | C, CPython API | Frame recording, ring buffer, checkpoints, binary log, I/O hooks |
| Python Backend (`pyttd/`) | Python, SQLite | JSON-RPC server, session navigation, query API |
| VSCode Extension (`vscode-pyttd/`) | TypeScript | DAP handlers, timeline webview, CodeLens, inline values |

See [docs/architecture.md](docs/architecture.md) for the full design.

## Platform Support

| Platform | Recording | Warm Navigation | Cold Navigation | Multi-Thread |
|----------|-----------|-----------------|-----------------|--------------|
| Linux | Full | Full | Full | Full |
| macOS | Full | Full | Partial* | Full |
| Windows | Full | Full | None | Full |

\* macOS: checkpoints skip when multiple threads are active.

## Requirements

- **Python >= 3.12** (required for `PyUnstable_InterpreterFrame_*` C API)
- **C compiler** (GCC, Clang, or MSVC) with SQLite development headers
- **VSCode** (for the extension; CLI works standalone)
- **Zero external Python dependencies** — uses only the standard library (`sqlite3`)

## Known Limitations

- Expression evaluation operates on recorded snapshots, not live values
- C extension internals are opaque (third-party C extension objects may have uninformative `repr()`)
- Windows: no cold navigation (no `fork()`)
- Variable repr strings are capped at 256 characters
- Expandable variable children are capped at 50 entries per level; nested containers use repr-parsing for deeper levels (requires `ast.literal_eval`-safe values)
- Attach mode (`arm()`) disables checkpoints — cold navigation is unavailable for attached recordings; the initial call stack is synthesized from frame inspection at arm time
- **Unscoped recording is expensive.** Compute-heavy code without `--include` / `--include-file` scoping incurs 4-57x slowdown (see [Performance](#performance)) and produces large DBs. Scope to the specific function or module under investigation for usable overhead on real workloads
- `--max-frames` is approximate — the actual frame count may slightly exceed the limit because events already in flight complete before the stop signal takes effect
- `--max-db-size` is approximate — the binary log checks per-record and the DB checks every 5 flush batches, so actual size may overshoot the limit; the overshoot is reported in the CLI output
- Exit codes: `0` = success (including `sys.exit(0)`), `N` = `sys.exit(N)`, `3` = uncaught exception in script, `130` = SIGINT
- `start_recording()` / `stop_recording()` only captures function calls — inline code in the calling scope is not recorded; use `arm()` for inline code recording

## Testing

545 Python tests across 48 test modules + 99 VSCode extension (Mocha) tests:

```bash
# Run all Python tests
.venv/bin/pytest tests/ -v

# Run VSCode extension tests
cd vscode-pyttd && npm test

# Run in-process recording benchmarks (throughput, scaling, microbenchmarks)
.venv/bin/pytest benchmarks/ -v -s

# Run subprocess overhead benchmarks (slowdown ratios, RSS)
.venv/bin/python3 benchmarks/bench_overhead.py

# Run scaled subprocess benchmarks (longer baselines, honest ratios)
.venv/bin/python3 benchmarks/bench_overhead_scaled.py -n 3
```

Benchmarks cover recording throughput (6 workload shapes), locals serialization scaling (count, type, container size), recorder internals (per-event cost slope, adaptive sampling, return-only optimization), and subprocess overhead (I/O-bound, compute-bound, tight loop, deep recursion, many locals, multi-thread).

## Documentation

- **[Getting Started](docs/getting-started.md)** — first recording walkthrough
- **[CLI Reference](docs/cli-reference.md)** — all commands and flags
- **[VSCode Guide](docs/vscode-guide.md)** — extension features and configuration
- **[API Reference](docs/api-reference.md)** — Python programmatic API
- **[Architecture](docs/architecture.md)** — system design and data flow
- **[pytest Integration](docs/pytest-integration.md)** — pytest plugin usage and CI setup
- **[Troubleshooting](docs/troubleshooting.md)** — common issues
- **[FAQ](docs/faq.md)** — frequently asked questions
- **[Contributing](CONTRIBUTING.md)** — how to contribute
- **[Changelog](CHANGELOG.md)** — version history

Development guides: [Building](docs/development/building.md) | [Testing](docs/development/testing.md) | [C Extension](docs/development/c-extension.md) | [Protocol](docs/development/protocol.md) | [Releasing](docs/development/releasing.md)

## Roadmap

- **PyCharm plugin** — pyttd's backend is IDE-agnostic (JSON-RPC over TCP with DAP semantics). A PyCharm plugin could integrate time-travel debugging into JetBrains IDEs using the `XDebugProcess` extension point, with custom tool windows for the timeline scrubber and reverse navigation controls. No changes to the Python or C code are needed — only a Kotlin/Java plugin for the PyCharm side.
- **Coverage-aware recording** — integrate with `coverage.py` to selectively record only functions/files that lack test coverage, reducing trace size while targeting the code most likely to contain bugs.
- **Multi-session / collaborative debugging** — share a pyttd recording session between multiple developers. One developer records and exports the `.pyttd.db`; others connect to a shared replay server to navigate the same trace simultaneously, with synchronized cursors and annotations.
- **Windows cold navigation** — checkpoint-based navigation currently requires `fork()` (Linux/macOS). A Windows implementation using `CreateProcess` with process snapshots is planned.
- **Remote debugging** — attach to a pyttd recording session on a remote machine over SSH or TCP.
- **N-way diff** — compare more than two runs simultaneously; full DTW alignment for complex divergence patterns.

## Contributing

Contributions welcome across C, Python, and TypeScript. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.
