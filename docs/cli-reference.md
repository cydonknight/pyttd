# CLI Reference

pyttd provides a command-line interface for recording, querying, replaying,
exporting, diffing, and serving debug sessions, plus a CI wrapper.

## Global options

```
pyttd [--version] [-v|--verbose] SUBCOMMAND [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--version` | Print version and exit |
| `-v`, `--verbose` | Enable debug logging (sets log level to DEBUG) |

## Subcommands

| Command | Purpose |
|---------|---------|
| `record` | Record a script's execution to a trace database |
| `query` | Query a recorded trace (frames, stats, exceptions, variable history, expressions) |
| `replay` | Warm-navigation replay with optional interactive REPL |
| `serve` | Start the JSON-RPC debug server (used by the VSCode extension) |
| `export` | Export a trace to Perfetto / Chrome Trace Event Format |
| `clean` | Delete old `.pyttd.db` files or evict old runs |
| `diff` | Compare two recordings to find the first divergence |
| `ci` | Wrap a command (e.g., `pytest`); preserve the trace on failure |

---

## `pyttd record`

Record a script's execution to a trace database.

```
pyttd record SCRIPT [OPTIONS] [--args ARGS...]
```

**Output:** `<script>.pyttd.db` in the script's directory (or the path set via
`--db-path`). Environment variable `PYTTD_RECORDING=1` is set during the
recording and cleared at exit.

### Flags

| Flag | Description | Default |
|------|-------------|---------|
| `SCRIPT` | Path to Python script, or module name if `--module` is set | required |
| `--module` | Treat `SCRIPT` as a module name (dotted path) | off |
| `--checkpoint-interval N` | Frames between fork-based checkpoints. `0` disables | `1000` |
| `--args VALUE...` | Arguments passed to the recorded script. **Must be the last flag** | `[]` |
| `--no-redact` | Disable secrets redaction | off |
| `--secret-patterns PAT` | Extra pattern for redaction (repeatable) | built-in list |
| `--include FUNC` | Record only matching functions (glob; repeatable) | all |
| `--include-file GLOB` | Record only files matching glob (`*` matches `/`; repeatable) | all |
| `--exclude FUNC` | Exclude matching functions (repeatable) | none |
| `--exclude-file GLOB` | Exclude files matching glob (repeatable) | none |
| `--max-frames N` | Approximate frame cap; `0` unlimited | `0` |
| `--db-path PATH` | Override default DB path | `<script>.pyttd.db` |
| `--max-db-size MB` | Auto-stop when DB exceeds this size | unlimited |
| `--keep-runs N` | Keep last N runs, evict older | keep all |
| `--checkpoint-memory-limit MB` | Total checkpoint RSS cap | unlimited |
| `--env KEY=VALUE...` | Env vars for the script (must follow `SCRIPT`) | — |
| `--env-file PATH` | Load env vars from a dotenv file | — |

### Examples

```bash
# Record a script
pyttd record my_app.py

# Record with script arguments (--args must be LAST)
pyttd record my_app.py --include process_data --args --port 8080 --debug

# Record a module
pyttd record --module mypackage.main

# Scope to a single function (cuts overhead 10-100x on compute-heavy code)
pyttd record my_app.py --include compute_metrics

# Scope by file glob
pyttd record my_app.py --include-file '*/billing/*.py'

# Custom DB path + aggressive size cap
pyttd record my_app.py --db-path /tmp/trace.db --max-db-size 50

# Disable checkpoints (warm-only replay, fastest recording)
pyttd record my_app.py --checkpoint-interval 0
```

### Include / exclude filter semantics

- Function patterns match the fully-qualified `__qualname__`. Bare names
  also match (e.g., `--include failing` matches `main.<locals>.failing`).
- File globs support `*` matching across `/` (e.g., `*/billing/*.py`).
- Bare filenames without a slash match anywhere in the path suffix.
- `--exclude` wins over `--include`.

---

## `pyttd query`

Query a recorded trace. Without any mode flag, prints a one-line run summary.

```
pyttd query [RUN-SELECTION] [MODE] [FILTERS] [OUTPUT] [--db PATH]
```

### Run selection

| Flag | Description |
|------|-------------|
| `--last-run` | Most recent recording in the DB |
| `--run-id UUID` | Specific run (UUID or prefix) |
| `--list-runs` | List all runs, then exit |

### Modes (what to show)

| Flag | Description |
|------|-------------|
| `--frames` | List frame events (call/line/return/exception) with source lines |
| `--search PATTERN` | Find frames with matching function name or filename |
| `--stats` | Per-function call and exception counts |
| `--exceptions` | Exception and exception_unwind events |
| `--line [FILE:]N` | All executions of a specific line |
| `--var-history VAR` | All value changes for a named variable |
| `--where EXPR` | Frames where a Python expression is truthy (e.g., `--where "len(x) > 5"`) |
| `--list-threads` | List distinct thread IDs |
| `--thread ID` | Filter frames by thread ID |

`--where` uses a restricted eval sandbox (no imports, no `open`, no side effects) and pre-filters by variable names referenced in the expression for efficiency.

### Output and filtering

| Flag | Description |
|------|-------------|
| `--limit N` | Cap the output at N frames (default `50`) |
| `--offset N` | Skip first N frames (paginate with `--limit`) |
| `--event-type TYPE` | Filter to a single event type (`call`, `line`, `return`, `exception`, `exception_unwind`) |
| `--file FILE` | Substring match on filename |
| `--format text\|json` | Output format (default `text`) |
| `--show-locals` | Include variable values alongside each frame |
| `--changed-only` | With `--show-locals`, only show variables that changed from the prior frame |
| `--expand` | With `--show-locals`, expand containers/objects into child trees |
| `--depth N` | Max expansion depth for `--expand` (default `2`) |
| `--hide-module-dunders` | Hide `__name__`/imports/functions at module scope (default ON) |
| `--show-all-globals` | Override `--hide-module-dunders` and show everything |
| `--hide-coroutine-internals` | Hide StopIteration noise from async code |
| `--unwind-only` | With `--exceptions`, show only `exception_unwind` events |
| `--db PATH` | DB file path (default: newest `.pyttd.db` in CWD) |

### Examples

```bash
# Show last run summary
pyttd query --last-run

# Dump all frames with source lines
pyttd query --last-run --frames

# Find every frame where a condition was true across the whole trace
pyttd query --last-run --where "len(users) > 5"

# Track a variable across the recording
pyttd query --last-run --var-history total

# Exception events, skipping async StopIteration noise
pyttd query --last-run --exceptions --hide-coroutine-internals

# All runs in a DB, JSON output
pyttd query --list-runs --db app.pyttd.db --format json
```

---

## `pyttd replay`

Warm-navigation replay. The CLI uses only SQLite reads (no checkpoint children
kept alive). For cold navigation during interactive debugging, use the VSCode
extension or `pyttd serve`.

```
pyttd replay [RUN-SELECTION] [--goto-frame N | --goto-line FILE:LINE] [--interactive] [--db PATH]
```

| Flag | Description |
|------|-------------|
| `--last-run` | Most recent recording |
| `--run-id UUID` | Specific run by UUID or prefix |
| `--goto-frame N` | Jump to frame N |
| `--goto-line FILE:LINE` | Jump to first execution of `FILE:LINE` (e.g., `app.py:42`) |
| `--interactive` | Open the interactive REPL |
| `--db PATH` | DB path |

### Interactive REPL

Readline history persists across sessions at `~/.pyttd_history`. Tab
completion for commands, function names, filenames, and variable names
(degrades gracefully if `readline` unavailable).

| Command | Aliases | Description |
|---------|---------|-------------|
| `step` | `s`, `step_into` | Next line event |
| `next` | `n` | Step over (same-depth next line) |
| `back` | `b`, `step_back` | Previous line event |
| `out` | `o`, `step_out` | Step out of current function |
| `continue` | `c` | Continue to next breakpoint / end |
| `rcontinue` | `rc`, `reverse_continue` | Reverse continue to previous breakpoint / start |
| `goto N` | `frame N` | Jump to frame N (also: `goto first`, `goto last`) |
| `vars` | `v`, `locals`, `info` | Show variables at current frame |
| `vars -e` | | Show variables with child trees expanded |
| `expand VAR` | | Expand a single variable (supports dotted paths: `expand config.database`) |
| `eval EXPR` | `print EXPR`, `p EXPR` | Evaluate expression against locals |
| `where` | `w`, `bt`, `stack`, `backtrace` | Show call stack |
| `watch VAR` | | Show variable change history |
| `find EXPR` | | Find frames where expression is truthy (e.g., `find len(x) > 5`) |
| `search PAT` | | Search frames by function/file name |
| `break F:L` | | Set line breakpoint |
| `break FUNC` | | Set function breakpoint |
| `logpoint F:L MSG` | | Emit message when hit, don't stop |
| `breaks` | | List breakpoints |
| `delete [N]` | | Delete breakpoint N or all |
| `quit` | `q`, `exit` | Exit REPL |

---

## `pyttd serve`

Start a JSON-RPC debug server over TCP. Used by the VSCode extension.

```
pyttd serve (--script SCRIPT | --db PATH) [OPTIONS]
```

Exactly one of `--script` or `--db` is required:
- `--script SCRIPT` — record the script, then enter replay mode
- `--db PATH` — open an existing trace, replay-only (no recording)

### Flags

| Flag | Description |
|------|-------------|
| `--script PATH` | Script to record and debug |
| `--db PATH` | Existing `.pyttd.db` for replay-only mode |
| `--module` | Treat `--script` as a module name |
| `--cwd DIR` | Working directory (default `.`) |
| `--checkpoint-interval N` | Frames between checkpoints (default `1000`) |
| `--include`, `--include-file`, `--exclude`, `--exclude-file` | Same semantics as `record` |
| `--max-frames N` | Approximate frame cap |
| `--env KEY=VALUE...` | Environment variables for the script |
| `--env-file PATH` | Dotenv file |
| `--db-path PATH` | Override DB path |
| `--max-db-size MB` | Auto-stop threshold |
| `--keep-runs N` | Evict older runs |
| `--run-id UUID` | Replay a specific run (with `--db`) |

### Port handshake

The server binds to `127.0.0.1:0` (OS-assigned port) and writes to stdout:

```
PYTTD_PORT:<port>
```

The VSCode extension reads this line to connect.

---

## `pyttd export`

Export a recording to Perfetto / Chrome Trace Event Format.

```
pyttd export --format perfetto --db PATH [-o OUTPUT] [--run-id UUID] [--force]
```

| Flag | Description |
|------|-------------|
| `--format perfetto` | Export format (currently only Perfetto) |
| `--db PATH` | DB path (required) |
| `-o OUTPUT`, `--output OUTPUT` | Output file (required) |
| `--run-id UUID` | Specific run (default: latest) |
| `--force` | Overwrite existing output file |

The resulting JSON opens at [ui.perfetto.dev](https://ui.perfetto.dev) or
`chrome://tracing`. Multi-thread structure is preserved.

---

## `pyttd clean`

Delete `.pyttd.db` files or evict old runs.

```
pyttd clean [--db PATH | --all | --keep N] [--dry-run]
```

| Flag | Description |
|------|-------------|
| `--db PATH` | Delete a specific DB file (and its WAL/SHM/binlog companions) |
| `--all` | Delete all `.pyttd.db` files in CWD (not recursive) |
| `--keep N` | Keep last N runs in the DB, evict the rest |
| `--dry-run` | Preview what would be deleted |

---

## `pyttd diff`

Compare two recordings to find the earliest divergence (control-flow or data).

```
pyttd diff --runs RUN_A RUN_B --db PATH [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--runs RUN_A RUN_B` | Run IDs to compare (UUID or prefix, required) |
| `--db PATH` | Database containing both runs (required) |
| `--context N` | Lines of matching context around the divergence (default `3`) |
| `--ignore-vars VAR` | Skip a variable when comparing locals (repeatable) |
| `--format text\|json` | Output format (default `text`) |

The alignment is best-effort: exact lockstep comparison with single-step
resync lookahead. It normalizes memory addresses and filters ephemeral
objects (functions, modules) automatically.

---

## `pyttd ci`

Wrap a command; preserve the trace on failure. Designed for CI pipelines.

```
pyttd ci [OPTIONS] -- COMMAND...
```

| Flag | Description |
|------|-------------|
| `--artifact-dir DIR` | Output directory (default `.pyttd-ci-artifacts/`) |
| `--keep-on-success` | Keep artifacts even on success (default: delete on success) |
| `--compress` / `--no-compress` | Gzip artifacts (default: gzip on) |
| `--max-size-mb MB` | Per-recording DB size cap (default `500`) |
| `--no-record` | Disable auto-wrap with `pyttd record`; just set env vars for a pyttd-aware child |

Python commands (`python script.py`, `python -m pkg.mod`, `./script.py`)
are auto-wrapped with `pyttd record`. For other commands (e.g., `pytest`),
combine with the pytest plugin (`pytest --pyttd-on-fail`) or pass
`--no-record` and enable pyttd inside the child process.

### Example: GitHub Actions

```yaml
- run: pyttd ci -- python tests/integration.py
- if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: pyttd-trace
    path: .pyttd-ci-artifacts/*.pyttd.db.gz
```

---

## Environment variables

| Variable | Description |
|----------|-------------|
| `PYTTD_RECORDING` | Set to `"1"` during recording, cleared after stop. User scripts can `os.environ.get('PYTTD_RECORDING')` to detect recording mode |
| `PYTTD_ARM_SIGNAL` | If set (e.g., `PYTTD_ARM_SIGNAL=USR1`), `import pyttd` installs a signal handler that toggles recording on/off |
| `PYTTD_DB_PATH` | Set by `pyttd ci` to direct a child process's recording to a specific DB |

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success (including `sys.exit(0)` from the recorded script) |
| `1` | User error (invalid arguments, script not found) |
| `2` | Recording error |
| `3` | Uncaught exception in the recorded script |
| `130` | Terminated by SIGINT |
| `N` | Any other code mirrors the recorded script's `sys.exit(N)` |

---

## See Also

- [Getting Started](getting-started.md) — first recording walkthrough
- [API Reference](api-reference.md) — Python programmatic API
- [VSCode Guide](vscode-guide.md) — using pyttd from VSCode
- [Troubleshooting](troubleshooting.md) — common issues and fixes
