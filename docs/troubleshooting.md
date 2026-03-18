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

The C extension adds overhead:

- **I/O-bound scripts:** ~2.5x slowdown (typical)
- **Compute-bound scripts:** ~10-12x slowdown (worst case)

To reduce overhead:
- Disable checkpoints: `--checkpoint-interval 0`
- Record only the function of interest using `@ttdbg`

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

### Variables show as `repr()` strings

This is by design. pyttd captures `repr()` snapshots at each line event. Variables are flat strings, not expandable objects. If you need to see nested structure, add a line that assigns the sub-expression to a local variable.

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
- Record only the function of interest with `@ttdbg`
- Use fewer local variables (each is `repr()`'d and stored)

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

### macOS: checkpoints not created during multi-thread recording

By design. `fork()` is unsafe when multiple threads are active. pyttd skips checkpoint creation when `ringbuf_thread_count() > 1`. Cold navigation falls back to warm-only for those regions.

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
