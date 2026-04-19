# Troubleshooting

## Installation Issues

### `pip install` fails to compile C extension

**Symptoms:** Error messages about missing headers or compiler errors during `pip install pyttd`.

**Solutions:**

- Ensure you have a C compiler installed:
  - macOS: `xcode-select --install`
  - Linux: `sudo apt install build-essential python3-dev`
- Ensure Python 3.12+: `python3 --version`
- Try a clean install: `pip install --no-cache-dir pyttd`

### `import pyttd_native` fails

**Symptoms:** `ModuleNotFoundError: No module named 'pyttd_native'`

**Solutions:**

- The C extension wasn't compiled. Run: `pip install -e .` (or `pip install pyttd`)
- Check you're using the correct Python: `.venv/bin/python -c "import pyttd_native"`
- If developing from source, rebuild after any C file changes: `.venv/bin/pip install -e .`

### Wrong Python version

**Symptoms:** `Python >= 3.12 is required` or `PyUnstable_InterpreterFrame` errors.

pyttd requires Python 3.12 or later. Check: `python3 --version`.

## Recording Issues

### No frames recorded / empty database

**Possible causes:**

1. **Script exits immediately** — if the script has no user code to execute, no frames are recorded
2. **All code filtered** — pyttd filters stdlib, site-packages, and frozen modules. Only user code is recorded
3. **Script path issue** — ensure the script path is correct: `pyttd record ./my_script.py`

### Recording is slow

The C extension adds overhead. Under hot-path-dominated workloads:

- **I/O-bound scripts:** ~1.4x slowdown
- **Tight loops (adaptive sampling kicks in):** ~4x slowdown
- **Compute-bound, every frame a line event:** 40-57x slowdown (worst case)

To reduce overhead:
- **Scope with `--include` / `--include-file`** — the single biggest lever. Realistic scoping drops overhead to 2-5x. See [Performance](../README.md#performance) for examples.
- Disable checkpoints: `--checkpoint-interval 0`.
- Record only the function of interest using `@ttdbg` or `arm()` / `disarm()`.

### `KeyboardInterrupt` during recording

This is the normal way to stop a long-running script. pyttd catches the interrupt, stops recording, and saves the trace database. The recording up to the interrupt point is preserved.

### Script can't find its files/modules

pyttd changes the working directory to the script's directory before execution (matching normal Python behavior). If your script uses relative paths, they should work as expected.

If using `--module`, ensure the module is importable from the `--cwd` directory.

## Navigation Issues

### "goto_frame" is slow

Cold navigation (50-300ms) is normal for `goto_frame` — it restores a fork checkpoint and fast-forwards. To speed it up:

- Decrease `checkpointInterval` in your launch config (e.g., 200)
- Use warm navigation (step/continue) for nearby frames

### "goto_frame" not available

Cold navigation requires `fork()`, which is only available on Linux and macOS. On Windows, `goto_frame` falls back to warm-only navigation (reads from SQLite).

### Step back is instant but step forward is not

`step_back` is always warm (SQLite read, sub-ms). `step_over` is also warm but may scan more frames to find the next line at the correct depth.

### Variables show as flat `repr()` strings

Most primitive values (int, float, bool, short string) are captured as flat repr for performance. Containers (dict/list/tuple/set/NamedTuple/dataclass) and objects with `__dict__` are captured as expandable trees — use `--expand` in `pyttd query`, the `expand VARNAME` REPL command, or the VSCode Variables panel to drill into them.

If a specific expression's value is flat-repr'd and you need structure, assign it to a dict or `@dataclass` local.

### Stack shows unexpected frames

pyttd reconstructs the stack from recorded events. If CPython internal frames leak through the filter (rare), you may see unexpected entries. These are harmless.

## VSCode Issues

### Extension doesn't start / "Cannot connect to backend"

1. Check the Debug Console for error messages
2. Ensure `pyttd` is installed in the Python environment VSCode is using
3. Check `pythonPath` in your launch config — it should point to a Python with pyttd installed
4. Try setting `pythonPath` explicitly: `"pythonPath": "/path/to/.venv/bin/python3"`

### Timeline scrubber is empty

The timeline appears after recording completes and replay mode begins. If it's empty:

1. Ensure the recording produced frames (check Debug Console)
2. Try clicking in the timeline area to trigger a refresh

### CodeLens not showing

CodeLens annotations appear only for files that were traced during recording. Open a file that contains recorded functions.

### Breakpoints don't work in reverse continue

Ensure breakpoints are set in files that were recorded. Breakpoints in unrecorded files (stdlib, dependencies) are ignored.

## Database Issues

### `.pyttd.db` file is large

The database stores every frame event with variable snapshots. Typical sizes:

- Short script (1K frames): ~100 KB
- Medium script (100K frames): ~10 MB
- Large script (1M frames): ~100 MB

To reduce size:
- **Scope recording with `--include` / `--include-file`** — same lever as reducing overhead.
- Use `--max-db-size MB` to cap the binlog (auto-stops when reached).
- Use `--max-frames N` to cap event count.
- Use `--keep-runs N` or `pyttd clean --keep N` to evict old runs.
- Record only the function of interest with `@ttdbg` or `arm()` / `disarm()`.

### "database is locked" errors

The flush thread and main thread both access SQLite. WAL mode and `busy_timeout=5000` handle most contention. If you see lock errors:

1. Ensure no other process has the `.pyttd.db` file open
2. Delete orphaned `-wal` and `-shm` files: they can corrupt a new DB

### Stale WAL/SHM files

If a recording was interrupted (crash, kill -9), orphaned `-wal` and `-shm` files may remain. Delete them before re-recording:

```bash
rm my_script.pyttd.db-wal my_script.pyttd.db-shm
```

Or let pyttd handle it — `pyttd record` deletes existing DB files (including WAL/SHM) before recording.

## Platform-Specific Issues

<a id="checkpoints"></a>

### Checkpoints skipped — "cold navigation is limited"

**Symptom:** After recording, pyttd prints

```
Note: cold navigation is limited. N checkpoint(s) were skipped because
multiple threads were active at checkpoint time.
```

and `goto_frame` jumps in the affected region of the recording feel unusually slow.

**Why this happens.** Cold navigation works by `fork()`-ing the recording process at periodic checkpoints, then fast-forwarding the child to your target. POSIX `fork()` is only async-signal-safe when no other threads are running — duplicating a process whose other threads hold mutexes, file descriptors, or C-extension state can leave the child in an unrecoverable state. pyttd's checkpoint trigger refuses to fork whenever it observes more than one active recording thread (`ringbuf_thread_count() > 1`).

This affects any Python program with a background thread, including:

- Threads spawned by libraries (HTTP clients, log handlers, schedulers, ORMs).
- `concurrent.futures.ThreadPoolExecutor`.
- `asyncio` event-loop default executors that run blocking work in a thread.
- macOS Cocoa runloops in some GUI bindings.

The recording itself is unaffected — every frame is still captured. Only checkpoint creation, and therefore cold `goto_frame`, is degraded.

**What to do.**

- **Scope the recording.** Use `--include`, `--include-file`, `--exclude`, `--exclude-file` so the recording only covers code that runs single-threaded. Threads spawned by ignored modules still exist, but if you can structure your reproducer to use a single thread, do so.
- **Use warm navigation.** Step back, step forward, reverse continue, and breakpoint navigation are warm — they read from SQLite and are unaffected. Only `goto_frame` to a far-away point relies on checkpoints.
- **Run on Linux.** The constraint applies on every POSIX platform but in practice Linux's libc/`fork()` is more forgiving with simple C-extension state than macOS. You may see fewer skips on the same workload.
- **`arm()` a single-threaded region.** If you only need cold navigation in a specific function, call `pyttd.arm()` / `pyttd.disarm()` around code you know runs on the main thread alone.

**Phase 2 (planned).** An opt-in `pyttd.checkpoint_safe_region()` context manager that quiesces recording threads before fork is on the roadmap; it has not landed yet because it interacts with GIL semantics in subtle ways.

### macOS: checkpoints not created during multi-thread recording

See [Checkpoints skipped](#checkpoints) above. macOS is the platform where this guard fires most aggressively.

### Windows: no cold navigation

Windows doesn't support `fork()`. All navigation is warm-only (SQLite reads). `goto_frame` works but uses warm navigation instead of checkpoint restore.

### Linux: ASAN reports leaks

When running with Address Sanitizer, Python itself reports known "leaks". Use `ASAN_OPTIONS=detect_leaks=0` to suppress these:

```bash
ASAN_OPTIONS=detect_leaks=0 pytest tests/ -v
```

## See Also

- [FAQ](faq.md) — frequently asked questions
- [Building](development/building.md) — build from source
- [Architecture](architecture.md) — understanding warm vs cold navigation
