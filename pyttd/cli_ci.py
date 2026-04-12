"""``pyttd ci`` — CI wrapper that preserves trace artifacts on failure.

Wraps a command with ``pyttd record`` to automatically capture a trace.
On failure, the .pyttd.db artifact is preserved (and optionally gzipped)
for upload to CI artifact storage. On success, artifacts are deleted
unless ``--keep-on-success`` is set.

For non-Python commands (or when ``--no-record`` is passed), pyttd sets
``PYTTD_DB_PATH`` and ``PYTTD_ARM_SIGNAL`` environment variables instead,
which requires the child command to be pyttd-aware (e.g., ``pytest --pyttd``).
"""
import gzip
import os
import shutil
import subprocess
import sys


def _looks_like_python_command(cmd: list[str]) -> bool:
    """Heuristic: does this command invoke a Python script directly?"""
    if not cmd:
        return False
    first = os.path.basename(cmd[0])
    # python, python3, python3.12, etc.
    if first.startswith('python'):
        return True
    # Direct .py script invocation
    if first.endswith('.py'):
        return True
    # Check if second arg is a .py file (e.g., /usr/bin/env python script.py)
    if len(cmd) >= 2 and cmd[1].endswith('.py'):
        return True
    return False


def _build_record_command(cmd: list[str], db_path: str, max_size_mb: int) -> list[str]:
    """Wrap a Python command with ``pyttd record``."""
    record_cmd = [sys.executable, "-m", "pyttd", "record",
                  "--db-path", db_path]
    if max_size_mb > 0:
        record_cmd.extend(["--max-db-size", str(max_size_mb)])

    first = os.path.basename(cmd[0])

    if first.startswith('python'):
        # cmd is: python [flags] script.py [args...]
        # Find the script argument (skip python flags like -u, -B, etc.)
        script_idx = 1
        while script_idx < len(cmd) and cmd[script_idx].startswith('-'):
            # Skip -m (module), -c (command) — handle separately
            if cmd[script_idx] in ('-m',):
                record_cmd.append("--module")
                script_idx += 1
                break
            elif cmd[script_idx] in ('-c',):
                # Can't record a -c command easily, fall back to env mode
                return None
            script_idx += 1

        if script_idx >= len(cmd):
            return None  # No script found, fall back to env mode

        script = cmd[script_idx]
        record_cmd.append(script)
        # Remaining args passed via --args
        remaining = cmd[script_idx + 1:]
        if remaining:
            record_cmd.append("--args")
            record_cmd.extend(remaining)
        return record_cmd

    elif first.endswith('.py'):
        # Direct script invocation
        record_cmd.append(cmd[0])
        if len(cmd) > 1:
            record_cmd.append("--args")
            record_cmd.extend(cmd[1:])
        return record_cmd

    return None


def _cmd_ci(args):
    """Entry point for ``pyttd ci -- <command> [args...]``."""
    artifact_dir = os.path.abspath(args.artifact_dir)
    os.makedirs(artifact_dir, exist_ok=True)

    db_path = os.path.join(artifact_dir, "run.pyttd.db")

    # Clean stale artifacts
    for suffix in ("", "-wal", "-shm", ".gz"):
        p = db_path + suffix
        if os.path.isfile(p):
            os.unlink(p)

    cmd = args.cmd
    if not cmd:
        print("pyttd ci: no command specified", file=sys.stderr)
        sys.exit(1)

    max_size = getattr(args, 'max_size_mb', 500)
    no_record = getattr(args, 'no_record', False)

    # Try to wrap with pyttd record for automatic recording
    record_cmd = None
    if not no_record and _looks_like_python_command(cmd):
        record_cmd = _build_record_command(cmd, db_path, max_size)

    if record_cmd:
        # Automatic recording mode
        result = subprocess.run(record_cmd)
    else:
        # Env-variable mode (for non-Python commands or --no-record)
        env = os.environ.copy()
        env["PYTTD_DB_PATH"] = db_path
        env["PYTTD_ARM_SIGNAL"] = "USR1"
        result = subprocess.run(cmd, env=env)

    exit_code = result.returncode

    has_db = os.path.isfile(db_path) and os.path.getsize(db_path) > 0

    if exit_code == 0:
        # Success path
        if not args.keep_on_success and has_db:
            _remove_artifacts(db_path)
            print("pyttd ci: command succeeded, artifacts cleaned up",
                  file=sys.stderr)
        elif has_db:
            if args.compress:
                db_path = _compress_db(db_path)
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            print(f"pyttd ci: command succeeded (artifacts kept)",
                  file=sys.stderr)
            print(f"  Artifact: {db_path} ({size_mb:.1f} MB)",
                  file=sys.stderr)
        else:
            print("pyttd ci: command succeeded", file=sys.stderr)
    else:
        # Failure path — preserve artifacts
        if has_db:
            if args.compress:
                db_path = _compress_db(db_path)
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            print(f"pyttd ci: command exited {exit_code}", file=sys.stderr)
            print(f"  Artifact: {db_path} ({size_mb:.1f} MB)", file=sys.stderr)
            if db_path.endswith(".gz"):
                uncompressed = db_path[:-3]
                print(f"  Replay locally: gunzip -k {db_path} && "
                      f"pyttd replay --db {uncompressed} --interactive",
                      file=sys.stderr)
            else:
                print(f"  Replay locally: pyttd replay --db {db_path} --interactive",
                      file=sys.stderr)
        else:
            print(f"pyttd ci: command exited {exit_code} (no trace artifact)",
                  file=sys.stderr)

    sys.exit(exit_code)


def _remove_artifacts(db_path: str):
    """Remove .pyttd.db and companion files."""
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.isfile(p):
            try:
                os.unlink(p)
            except OSError:
                pass


def _compress_db(db_path: str) -> str:
    """Gzip a .pyttd.db file in-place, return the new path."""
    gz_path = db_path + ".gz"
    with open(db_path, "rb") as f_in:
        with gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    os.unlink(db_path)
    # Also clean WAL/SHM
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.isfile(p):
            os.unlink(p)
    return gz_path
