import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_USER_ERROR = 1      # bad arguments, missing file
EXIT_RECORDING_ERROR = 2  # recording failed
EXIT_SCRIPT_ERROR = 3     # recorded script raised an exception

# ---- Color utilities (TTY-only) ----

def _use_color():
    """Return True if stderr is a TTY and NO_COLOR is not set."""
    return hasattr(sys.stderr, 'isatty') and sys.stderr.isatty() and 'NO_COLOR' not in os.environ

_RED = '\033[31m'
_GREEN = '\033[32m'
_YELLOW = '\033[33m'
_DIM = '\033[2m'
_BOLD = '\033[1m'
_RESET = '\033[0m'

def _c(code, text):
    """Wrap text in ANSI color if color is enabled."""
    if _use_color():
        return f"{code}{text}{_RESET}"
    return text


def _format_stats(stats: dict, db_path: str = None, show_guidance: bool = False,
                   script_path: str = None) -> str:
    fc = stats.get('frame_count', 0)
    dropped = stats.get('dropped_frames', 0)
    elapsed = stats.get('elapsed_time', 0.0)
    rate = fc / elapsed if elapsed > 0 else 0
    pool_ov = stats.get('pool_overflows', 0)
    cp_count = stats.get('checkpoint_count', 0)
    cp_mem = stats.get('checkpoint_memory_bytes', 0)

    lines = []
    lines.append(f"Recorded {fc:,} frames in {elapsed:.1f}s ({rate:,.0f} frames/sec).")

    if fc == 0:
        lines.append(
            "Hint: No frames recorded. Standard library and frozen modules are "
            "excluded by default. Use --include-file to broaden the filter, or "
            "ensure the script contains user-defined function calls."
        )

    if dropped > 0:
        lines.append(
            f"WARNING: {dropped:,} frames dropped (ring buffer full). "
            "Increase buffer size with --checkpoint-interval or reduce "
            "recording scope with --include/--exclude."
        )
    if pool_ov > 0:
        lines.append(
            f"WARNING: {pool_ov:,} string pool overflows. "
            "Some variable snapshots may be truncated."
        )
    if cp_count > 0:
        cp_mb = cp_mem / (1024 * 1024)
        lines.append(f"{cp_count} checkpoint(s), {cp_mb:.1f} MB total RSS.")

    # UX-1: Show database path
    if db_path:
        lines.append(f"Database: {db_path}")

    # UX-2: Post-record guidance
    if show_guidance and db_path and fc > 0:
        lines.append(f"  Query:  pyttd query --last-run --frames --db {db_path}")
        lines.append(f"  Replay: pyttd replay --last-run --goto-frame 0 --db {db_path}")
        if script_path:
            script_base = os.path.basename(script_path)
            lines.append(f"          pyttd replay --last-run --goto-line {script_base}:1 --db {db_path}")
        lines.append(f"          pyttd replay --last-run --interactive --db {db_path}")

    return "\n".join(lines)


def _format_exception_location(db_path: str, run_id: str) -> str | None:
    """UX-5: Find the last exception_unwind event and format its location."""
    try:
        from pyttd.models import storage
        from pyttd.models.db import db
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        # Prefer 'exception' event (has correct line number) over
        # 'exception_unwind' (which has the def line, not the raise site).
        row = db.fetchone(
            "SELECT sequence_no, function_name, filename, line_no"
            " FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'exception'"
            " ORDER BY sequence_no ASC LIMIT 1",
            (str(run_id),))
        if row is None:
            row = db.fetchone(
                "SELECT sequence_no, function_name, filename, line_no"
                " FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'exception_unwind'"
                " ORDER BY sequence_no ASC LIMIT 1",
                (str(run_id),))
        storage.close_db()
        if row:
            basename = os.path.basename(row.filename)
            return (f"  Exception at frame #{row.sequence_no}"
                    f" in {row.function_name}() at {basename}:{row.line_no}\n"
                    f"  Replay:  pyttd replay --last-run --goto-frame {row.sequence_no} --db {db_path}")
    except Exception:
        pass
    return None

def _format_frame_line(f, source: str) -> str:
    """Format a single frame line for query output (UX-8: with color)."""
    # U11: Display line 0 as line 1 — CPython reports def line as 0 for <module>
    display_line = f.line_no if f.line_no > 0 else 1
    base = f"  #{f.sequence_no:>6} {f.frame_event:<18} {f.function_name}:{display_line}  {source}"
    evt = f.frame_event
    if evt in ('exception', 'exception_unwind'):
        return _c(_RED, base)
    elif evt == 'return':
        return _c(_DIM, base)
    elif evt == 'call':
        return _c(_BOLD, base)
    return base


def _print_locals(frame, changed_only=False, prev_locals=None):
    """Print locals for a frame (shared by --frames, --search, --thread, etc.)."""
    if not frame.locals_snapshot:
        return prev_locals or {}
    try:
        import json as _jl
        ld = _jl.loads(frame.locals_snapshot)
        for vname, vval in ld.items():
            vstr = _format_local_value(vval)
            if changed_only and prev_locals:
                if prev_locals.get(vname) == vstr:
                    continue
            print(f"           {_c(_DIM, vname + ' = ' + vstr)}")
        if changed_only:
            return {k: _format_local_value(v) for k, v in ld.items()}
    except Exception:
        pass
    return prev_locals or {}


def _format_local_value(value) -> str:
    """Format a local variable value for CLI display (UX-3)."""
    if isinstance(value, dict) and '__type__' in value:
        type_name = value.get('__type__', '')
        repr_str = value.get('__repr__', str(value))
        length = value.get('__len__')
        children = value.get('__children__')
        # UX-2: For objects, show attr count instead of misleading len=0
        if type_name == 'object' and children:
            suffix = f"  ({type_name}, {len(children)} attr(s))"
        elif length is not None and length > 0:
            suffix = f"  ({type_name}, len={length})"
        else:
            suffix = f"  ({type_name})"
        return repr_str + _c(_DIM, suffix)
    return str(value)


def main():
    from pyttd import __version__
    parser = argparse.ArgumentParser(prog='pyttd', description='Python Time-Travel Debugger')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging')
    subparsers = parser.add_subparsers(dest='command')

    record_parser = subparsers.add_parser('record', help='Record script execution')
    record_parser.add_argument('script', help='Script path or module name (with --module)')
    record_parser.add_argument('--module', action='store_true', help='Treat script as module name')
    record_parser.add_argument('--checkpoint-interval', type=int, default=1000)
    record_parser.add_argument('--args', nargs=argparse.REMAINDER, default=[],
                               help='Arguments to pass to the script. MUST be the last flag — '
                                    'all tokens after --args are passed to the script, including '
                                    'any that look like pyttd flags. Place pyttd flags before --args.')
    record_parser.add_argument('--no-redact', action='store_true',
                               help='Disable secrets redaction in recorded variables')
    record_parser.add_argument('--secret-patterns', action='append', default=None,
                               help='Additional secret pattern for variable redaction (repeatable)')
    record_parser.add_argument('--include', action='append', default=None,
                               help='Only record functions matching this pattern (repeatable, supports glob: *, ?, [])')
    record_parser.add_argument('--include-file', action='append', default=None,
                               help='Only record functions in files whose full path matches this glob '
                                    '(* matches any chars including /). Repeatable')
    record_parser.add_argument('--exclude', action='append', default=None,
                               help='Exclude functions matching this pattern from recording (repeatable)')
    record_parser.add_argument('--exclude-file', action='append', default=None,
                               help='Exclude files whose full path matches this glob from recording '
                                    '(* matches any chars including /). Repeatable')
    record_parser.add_argument('--max-frames', type=int, default=0,
                               help='Approximate max frames to record; may slightly overshoot (0 = unlimited)')
    record_parser.add_argument('--db-path', type=str, default=None,
                               help='Custom database path (default: <script>.pyttd.db)')
    record_parser.add_argument('--max-db-size', type=float, default=0,
                               help='Auto-stop recording when binlog exceeds this size in MB (0 = unlimited)')
    record_parser.add_argument('--keep-runs', type=int, default=0,
                               help='Keep only last N runs, evict older (0 = keep all)')
    record_parser.add_argument('--checkpoint-memory-limit', type=int, default=0,
                               help='Checkpoint memory limit in MB (0 = unlimited)')
    record_parser.add_argument('--env', nargs='+', default=None,
                               help='Environment variables (KEY=VALUE format). '
                                    'Must be placed AFTER the script path since it '
                                    'consumes multiple arguments.')
    record_parser.add_argument('--env-file', type=str, default=None,
                               help='Path to dotenv file to load environment variables from')

    query_parser = subparsers.add_parser('query', help='Query trace data')
    query_parser.add_argument('--last-run', action='store_true')
    query_parser.add_argument('--list-runs', action='store_true', help='List all runs in database')
    query_parser.add_argument('--run-id', type=str, default=None,
                              help='Query specific run by UUID or prefix')
    query_parser.add_argument('--frames', action='store_true')
    query_parser.add_argument('--limit', type=int, default=50)
    query_parser.add_argument('--search', type=str, default=None,
                              help='Search frames by function name or filename substring')
    query_parser.add_argument('--thread', type=int, default=None,
                              help='Filter frames by thread ID')
    query_parser.add_argument('--list-threads', action='store_true',
                              help='List all thread IDs in the recording')
    query_parser.add_argument('--event-type', type=str, default=None,
                              help='Filter frames by event type (call, line, return, exception, exception_unwind)')
    query_parser.add_argument('--offset', type=int, default=0,
                              help='Skip first N frames (use with --limit for pagination)')
    query_parser.add_argument('--line', type=str, default=None,
                              help='Show all executions of a line number. Accepts N or FILE:N.')
    query_parser.add_argument('--var-history', type=str, default=None,
                              help='Show how a variable changes over the recording')
    query_parser.add_argument('--format', choices=['text', 'json'], default='text',
                              help='Output format (default: text)')
    query_parser.add_argument('--show-locals', action='store_true',
                              help='Show variable values alongside frame output')
    query_parser.add_argument('--changed-only', action='store_true',
                              help='With --show-locals, only show variables that changed from the previous frame')
    query_parser.add_argument('--stats', action='store_true',
                              help='Show per-function call and exception counts')
    query_parser.add_argument('--file', type=str, default=None,
                              help='Filter frames by source filename (substring match)')
    query_parser.add_argument('--exceptions', action='store_true',
                              help='Show exception_unwind events (shortcut for --event-type exception_unwind)')
    query_parser.add_argument('--hide-coroutine-internals', action='store_true',
                              help='Hide coroutine-internal exception events (StopIteration noise from async)')
    query_parser.add_argument('--db', type=str, default=None)

    replay_parser = subparsers.add_parser('replay', help='Replay a recorded session')
    replay_parser.add_argument('--last-run', action='store_true')
    replay_parser.add_argument('--run-id', type=str, default=None,
                               help='Replay specific run by UUID or prefix')
    replay_parser.add_argument('--goto-frame', type=int, default=0,
                               help='Jump to frame N (uses stored data; for live variable '
                                    'inspection use pyttd serve with a debugger frontend)')
    replay_parser.add_argument('--interactive', action='store_true',
                               help='Enter interactive replay REPL')
    replay_parser.add_argument('--goto-line', type=str, default=None,
                               help='Jump to first execution of FILE:LINE (e.g., script.py:42)')
    replay_parser.add_argument('--db', type=str, default=None)

    serve_parser = subparsers.add_parser('serve', help='Start JSON-RPC debug server')
    serve_group = serve_parser.add_mutually_exclusive_group(required=True)
    serve_group.add_argument('--script', help='Script to record and debug')
    serve_group.add_argument('--db', type=str, help='Existing .pyttd.db to replay (no recording)')
    serve_parser.add_argument('--module', action='store_true')
    serve_parser.add_argument('--cwd', default='.')
    serve_parser.add_argument('--checkpoint-interval', type=int, default=1000)
    serve_parser.add_argument('--include', action='append', default=None,
                              help='Only record functions matching this pattern (repeatable)')
    serve_parser.add_argument('--include-file', action='append', default=None,
                              help='Only record functions in files whose full path matches this glob '
                                   '(* matches any chars including /). Repeatable')
    serve_parser.add_argument('--exclude', action='append', default=None,
                              help='Exclude functions matching this pattern from recording (repeatable)')
    serve_parser.add_argument('--exclude-file', action='append', default=None,
                              help='Exclude files whose full path matches this glob from recording '
                                   '(* matches any chars including /). Repeatable')
    serve_parser.add_argument('--max-frames', type=int, default=0,
                              help='Approximate max frames to record; may slightly overshoot (0 = unlimited)')
    serve_parser.add_argument('--env', nargs='+', default=None,
                              help='Environment variables (KEY=VALUE format)')
    serve_parser.add_argument('--env-file', type=str, default=None,
                              help='Path to dotenv file to load environment variables from')
    serve_parser.add_argument('--db-path', type=str, default=None,
                              help='Custom database path (default: <script>.pyttd.db)')
    serve_parser.add_argument('--max-db-size', type=float, default=0,
                              help='Auto-stop recording when binlog exceeds this size in MB (0 = unlimited)')
    serve_parser.add_argument('--keep-runs', type=int, default=0,
                              help='Keep only last N runs, evict older (0 = keep all)')
    serve_parser.add_argument('--run-id', type=str, default=None,
                              help='Replay specific run by UUID or prefix (with --db)')

    export_parser = subparsers.add_parser('export', help='Export trace data')
    export_parser.add_argument('--format', choices=['perfetto'], default='perfetto',
                               help='Export format (default: perfetto)')
    export_parser.add_argument('--db', type=str, required=True,
                               help='Path to .pyttd.db file')
    export_parser.add_argument('--run-id', type=str, default=None,
                               help='Export specific run by UUID or prefix')
    export_parser.add_argument('-o', '--output', type=str, required=True,
                               help='Output file path')
    export_parser.add_argument('--force', action='store_true',
                               help='Overwrite output file without warning')

    clean_parser = subparsers.add_parser('clean', help='Clean up database files')
    clean_parser.add_argument('--db', type=str, default=None,
                              help='Specific database file to clean')
    clean_parser.add_argument('--all', action='store_true',
                              help='Delete all .pyttd.db files in current directory (not recursive)')
    clean_parser.add_argument('--keep', type=int, default=None,
                              help='Keep last N runs, evict the rest')
    clean_parser.add_argument('--dry-run', action='store_true',
                              help='Show what would be deleted without deleting')

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format='%(name)s: %(message)s',
    )

    if args.command is None:
        parser.print_help()
        sys.exit(0)  # User asked for guidance, not an error

    if args.command == 'record':
        _cmd_record(args)
    elif args.command == 'query':
        _cmd_query(args)
    elif args.command == 'replay':
        _cmd_replay(args)
    elif args.command == 'serve':
        _cmd_serve(args)
    elif args.command == 'export':
        _cmd_export(args)
    elif args.command == 'clean':
        _cmd_clean(args)

_PYTTD_FLAGS = frozenset({
    '--db-path', '--max-frames', '--max-db-size', '--keep-runs',
    '--include', '--exclude', '--include-file', '--exclude-file',
    '--checkpoint-interval', '--no-redact', '--secret-patterns',
    '--checkpoint-memory-limit', '--module', '--format',
})


def _cmd_record(args):
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.models.storage import compute_db_path

    # UX-6: Warn if --args consumed tokens that look like pyttd flags
    if args.args:
        trapped = [a for a in args.args if a in _PYTTD_FLAGS]
        if trapped:
            print(f"Warning: {', '.join(trapped)} will be passed to the script, not pyttd. "
                  f"Move pyttd flags before --args.", file=sys.stderr)

    if args.module:
        script_abs = args.script
        cwd = os.getcwd()
    else:
        if not os.path.isfile(args.script):
            print(f"Error: script not found: {args.script}", file=sys.stderr)
            sys.exit(EXIT_USER_ERROR)
        script_abs = os.path.realpath(args.script)
        cwd = os.path.dirname(script_abs) or '.'

    db_path = compute_db_path(
        args.script, is_module=args.module, cwd=cwd,
        explicit_path=getattr(args, 'db_path', None),
    )
    logger.debug("Script: %s, CWD: %s, DB: %s", script_abs if not args.module else args.script, cwd, db_path)

    # CLI mode: use caller's checkpoint-interval (children killed after recording)
    config_kwargs = dict(checkpoint_interval=args.checkpoint_interval)
    if args.no_redact:
        config_kwargs['redact_secrets'] = False
    if args.secret_patterns is not None:
        from pyttd.config import _DEFAULT_SECRET_PATTERNS
        config_kwargs['secret_patterns'] = list(_DEFAULT_SECRET_PATTERNS) + args.secret_patterns
    if args.include is not None:
        config_kwargs['include_functions'] = args.include
    if args.include_file is not None:
        config_kwargs['include_files'] = args.include_file
    if args.exclude is not None:
        config_kwargs['exclude_functions'] = args.exclude
    if args.exclude_file is not None:
        config_kwargs['exclude_files'] = args.exclude_file
    if args.max_frames > 0:
        config_kwargs['max_frames'] = args.max_frames
    if args.max_db_size > 0:
        config_kwargs['max_db_size_mb'] = args.max_db_size
    if args.keep_runs > 0:
        config_kwargs['keep_runs'] = args.keep_runs
    if args.checkpoint_memory_limit > 0:
        config_kwargs['checkpoint_memory_limit_mb'] = args.checkpoint_memory_limit
    config = PyttdConfig(**config_kwargs)
    recorder = Recorder(config)
    runner = Runner()

    # U2: Apply --env and --env-file environment variables
    if getattr(args, 'env_file', None):
        for key, value in _parse_env_file(args.env_file).items():
            os.environ[key] = value
    if getattr(args, 'env', None):
        for item in args.env:
            if '=' in item:
                key, value = item.split('=', 1)
                os.environ[key] = value

    try:
        recorder.start(db_path, script_path=script_abs)
    except Exception as e:
        err_msg = str(e).lower()
        if "unable to open" in err_msg or "no such file" in err_msg:
            parent = os.path.dirname(db_path) or "."
            if not os.path.isdir(parent):
                print(f"Error: directory does not exist: {parent}", file=sys.stderr)
            else:
                print(f"Error: cannot create database: {e}", file=sys.stderr)
            sys.exit(EXIT_USER_ERROR)
        raise
    # UX-6: Progress indicator during recording (TTY only)
    import threading as _threading
    import time as _time
    _progress_stop = _threading.Event()
    def _show_progress():
        import pyttd_native
        start = _time.monotonic()
        while not _progress_stop.wait(0.5):
            try:
                s = pyttd_native.get_recording_stats()
                fc = s.get('frame_count', 0)
                elapsed = _time.monotonic() - start
                cp = s.get('checkpoint_count', 0)
                cp_str = f" ({cp} checkpoints)" if cp > 0 else ""
                sys.stderr.write(f"\rRecording... {fc:,} frames{cp_str}  [{elapsed:.1f}s]  ")
                sys.stderr.flush()
            except Exception:
                break
        sys.stderr.write("\r" + " " * 60 + "\r")  # clear line
        sys.stderr.flush()

    show_progress = hasattr(sys.stderr, 'isatty') and sys.stderr.isatty()
    if show_progress:
        _progress_thread = _threading.Thread(target=_show_progress, daemon=True)
        _progress_thread.start()

    script_error = None
    stats = {}
    try:
        if args.module:
            runner.run_module(args.script, cwd, args.args)
        else:
            runner.run_script(script_abs, cwd, args.args)
    except BaseException as e:
        script_error = e
    finally:
        _progress_stop.set()
        try:
            stats = recorder.stop()
        finally:
            recorder.cleanup()
    # Determine if KeyboardInterrupt was caused by a recording limit
    limit_stop = (isinstance(script_error, KeyboardInterrupt) and (
        (args.max_frames > 0 and stats.get('frame_count', 0) >= args.max_frames) or
        args.max_db_size > 0
    ))

    run_id = recorder.run_id

    # B1: Modules that close stdout (e.g., json.tool) cause print() to raise
    # ValueError. Fall back to stderr so the recording summary is never lost.
    def _safe_print(*a, **kw):
        try:
            print(*a, **kw)
        except (ValueError, OSError):
            try:
                kw['file'] = sys.stderr
                print(*a, **kw)
            except (ValueError, OSError):
                pass

    if script_error:
        if limit_stop:
            if args.max_frames > 0 and stats.get('frame_count', 0) >= args.max_frames:
                _safe_print("Recording stopped: frame limit reached")
            else:
                _safe_print("Recording stopped: database size limit reached")
            _safe_print(_format_stats(stats, db_path=db_path, script_path=script_abs))
        else:
            _safe_print(f"Script exited with {type(script_error).__name__}: {script_error}")
            # UX-5: Show exception frame location
            if run_id:
                exc_loc = _format_exception_location(db_path, run_id)
                if exc_loc:
                    _safe_print(exc_loc)
            _safe_print(_format_stats(stats, db_path=db_path, script_path=script_abs))
    else:
        _safe_print(_format_stats(stats, db_path=db_path, show_guidance=True, script_path=script_abs))

    # UX-12: Recording summary for large recordings
    if stats.get('frame_count', 0) > 0 and run_id:
        try:
            from pyttd.models import storage as _s2
            _s2.connect_to_db(db_path)
            _s2.initialize_schema()
            from pyttd.models.db import db as _d2
            file_count = _d2.fetchval(
                "SELECT COUNT(DISTINCT filename) FROM executionframes WHERE run_id = ?",
                (str(run_id),)) or 0
            func_count = _d2.fetchval(
                "SELECT COUNT(DISTINCT function_name) FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'call'",
                (str(run_id),)) or 0
            exc_count = _d2.fetchval(
                "SELECT COUNT(*) FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'exception_unwind'"
                " AND is_coroutine = 0",
                (str(run_id),)) or 0
            _s2.close_db()
            # Don't count SystemExit (any exit code) as an exception.
            # Python unwinds SystemExit through every frame on the call
            # stack, producing one `exception_unwind` event per frame — so a
            # single `sys.exit(42)` can emit 3-5 phantom exception events
            # the user never raised. sys.exit() is a deliberate program exit
            # mechanism, not an exception in the user-facing sense.
            if isinstance(script_error, SystemExit):
                exc_count = 0
            # Don't count KeyboardInterrupt unwinds when recording was limit-stopped
            # (the forced stop raises KeyboardInterrupt which unwinds all frames on
            # the call stack, producing phantom exception_unwind events).
            if limit_stop:
                exc_count = 0
            parts = [f"{file_count} file(s)", f"{func_count} function(s)"]
            if exc_count > 0:
                parts.append(_c(_RED, f"{exc_count} exception(s)"))
            else:
                parts.append("0 exceptions")
            # U13: DB size in summary
            try:
                db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
                parts.append(f"{db_size_mb:.1f} MB")
            except OSError:
                pass
            _safe_print(f"  {', '.join(parts)}")
            # U13: Active filters and redaction status
            extra = []
            if config.redact_secrets:
                extra.append("secrets redaction active")
            if config.include_functions:
                extra.append(f"include: {', '.join(config.include_functions)}")
            if config.exclude_functions:
                extra.append(f"exclude: {', '.join(config.exclude_functions)}")
            if extra:
                _safe_print(f"  ({'; '.join(extra)})")
        except Exception:
            pass

    # Multi-run guidance
    from pyttd.models import storage as _storage
    try:
        _storage.connect_to_db(db_path)
        _storage.initialize_schema()
        from pyttd.models.db import db as _db
        run_count = _db.fetchval("SELECT COUNT(*) FROM runs") or 0
        if run_count > 1:
            _safe_print(f"Database contains {run_count} runs. Use 'pyttd query --list-runs --db {db_path}' to see all.")
    except Exception:
        pass
    finally:
        _storage.close_db()

    # U4: Clean up empty DB files when 0 frames were recorded (e.g., syntax error)
    if stats.get('frame_count', 0) == 0 and os.path.exists(db_path):
        try:
            from pyttd.models.storage import delete_db_files
            delete_db_files(db_path)
        except Exception:
            pass

    if isinstance(script_error, SystemExit):
        sys.exit(script_error.code)
    elif isinstance(script_error, KeyboardInterrupt) and not limit_stop:
        sys.exit(130)  # Standard SIGINT exit code
    elif script_error is not None and not limit_stop:
        sys.exit(EXIT_SCRIPT_ERROR)

def _cmd_serve(args):
    from pyttd.server import PyttdServer

    if args.script:
        if not args.module and not os.path.isfile(args.script):
            print(f"Error: script not found: {args.script}", file=sys.stderr)
            sys.exit(EXIT_USER_ERROR)
        include_functions = args.include if args.include is not None else []
        env_vars = {}
        if args.env_file:
            env_vars.update(_parse_env_file(args.env_file))
        if args.env:
            for item in args.env:
                if '=' in item:
                    key, value = item.split('=', 1)
                    env_vars[key] = value
        server = PyttdServer(
            script=args.script,
            is_module=args.module,
            cwd=args.cwd,
            checkpoint_interval=args.checkpoint_interval,
            include_functions=include_functions,
            max_frames=args.max_frames,
            env_vars=env_vars if env_vars else None,
            include_files=args.include_file or [],
            exclude_functions=args.exclude or [],
            exclude_files=args.exclude_file or [],
            db_path=getattr(args, 'db_path', None),
            max_db_size_mb=args.max_db_size,
            keep_runs=args.keep_runs,
        )
    else:
        # --db mode: replay existing recording
        db_path = args.db
        if not os.path.isfile(db_path):
            print(f"Error: database not found: {db_path}", file=sys.stderr)
            sys.exit(EXIT_USER_ERROR)
        # UX-10: Print summary of the loaded trace
        try:
            from pyttd.models import storage as _s10
            from pyttd.models.db import db as _d10
            _s10.connect_to_db(db_path)
            _s10.initialize_schema()
            run_count = _d10.fetchval("SELECT COUNT(*) FROM runs") or 0
            total = _d10.fetchval("SELECT SUM(total_frames) FROM runs") or 0
            _s10.close_db()
            print(f"Loaded trace: {total:,} frames, {run_count} run(s) from {db_path}",
                  file=sys.stderr)
        except Exception:
            pass
        server = PyttdServer(
            script=None,
            is_module=False,
            cwd=args.cwd,
            checkpoint_interval=0,
            replay_db=db_path,
            target_run_id=getattr(args, 'run_id', None),
        )
    server.run()

def _cmd_query(args):
    from pyttd.query import (
        get_last_run, get_all_runs, get_run_by_id, get_frames, get_line_code,
        search_frames, get_frames_by_thread,
    )
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models import storage
    from pyttd.models.db import db as _db
    import glob as globmod

    db_path = args.db
    if not db_path:
        dbs = sorted(globmod.glob(f"*{DB_NAME_SUFFIX}"), key=os.path.getmtime, reverse=True)
        if not dbs:
            print("No .pyttd.db files found in current directory. Use --db to specify path.")
            sys.exit(EXIT_USER_ERROR)
        db_path = dbs[0]
        print(f"Using database: {db_path}", file=sys.stderr)

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(EXIT_USER_ERROR)

    try:
        if args.list_runs:
            runs = get_all_runs(db_path)
            if not runs:
                if getattr(args, 'format', 'text') == 'json':
                    print("[]")
                else:
                    print("No runs found.")
                return
            if getattr(args, 'format', 'text') == 'json':
                import json as _json_runs
                data = []
                for r in runs:
                    files = _db.fetchval(
                        "SELECT COUNT(DISTINCT filename) FROM executionframes WHERE run_id = ?",
                        (str(r.run_id),)) or 0
                    exceptions = _db.fetchval(
                        "SELECT COUNT(*) FROM executionframes WHERE run_id = ? AND frame_event = 'exception_unwind' AND is_coroutine = 0",
                        (str(r.run_id),)) or 0
                    data.append({
                        "run_id": str(r.run_id),
                        "script_path": r.script_path,
                        "is_attach": bool(getattr(r, 'is_attach', False)),
                        "total_frames": r.total_frames,
                        "files": files,
                        "exceptions": exceptions,
                        "timestamp_start": r.timestamp_start,
                        "timestamp_end": r.timestamp_end,
                    })
                print(_json_runs.dumps(data, indent=2))
                return
            print(f"{'Run ID':<40} {'Script':<30} {'Frames':>10} {'Files':>6} {'Exc':>5} {'Started':<23} {'Duration':>10}")
            print(f"{'-'*40} {'-'*30} {'-'*10} {'-'*6} {'-'*5} {'-'*23} {'-'*10}")
            from datetime import datetime
            for r in runs:
                script = os.path.basename(r.script_path) if r.script_path else 'unknown'
                if getattr(r, 'is_attach', False):
                    script += ' [attach]'
                started = datetime.fromtimestamp(r.timestamp_start).strftime('%Y-%m-%d %H:%M:%S') if r.timestamp_start else '?'
                if r.timestamp_end and r.timestamp_start:
                    duration = f"{r.timestamp_end - r.timestamp_start:.1f}s"
                else:
                    duration = '?'
                # Summary stats per run
                files = _db.fetchval(
                    "SELECT COUNT(DISTINCT filename) FROM executionframes WHERE run_id = ?",
                    (str(r.run_id),)) or 0
                exceptions = _db.fetchval(
                    "SELECT COUNT(*) FROM executionframes WHERE run_id = ? AND frame_event = 'exception_unwind' AND is_coroutine = 0",
                    (str(r.run_id),)) or 0
                exc_str = _c(_RED, str(exceptions)) if exceptions > 0 else str(exceptions)
                print(f"{str(r.run_id):<40} {script:<30} {r.total_frames:>10} {files:>6} {exc_str:>5} {started:<23} {duration:>10}")
            return

        if args.run_id:
            try:
                run = get_run_by_id(db_path, args.run_id)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
        else:
            try:
                run = get_last_run(db_path)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
        # Send banner to stderr when JSON format is requested so stdout is pipeable
        _banner_stream = sys.stderr if getattr(args, 'format', 'text') == 'json' else sys.stdout
        print(f"Run: {run.run_id} ({run.script_path or 'unknown'}) — {run.total_frames} frames",
              file=_banner_stream)

        if getattr(args, 'list_threads', False):
            rows = _db.fetchall(
                "SELECT thread_id, COUNT(*) as cnt FROM executionframes"
                " WHERE run_id = ? GROUP BY thread_id ORDER BY cnt DESC",
                (str(run.run_id),)
            )
            if not rows:
                print("No threads found.")
            else:
                print(f"{'Thread ID':>20} {'Frames':>10}")
                print(f"{'-'*20} {'-'*10}")
                for row in rows:
                    print(f"{row.thread_id:>20} {row.cnt:>10}")
            return

        show_locals = getattr(args, 'show_locals', False)
        changed_only = getattr(args, 'changed_only', False)

        search = getattr(args, 'search', None)
        if search is not None:
            frames = search_frames(run.run_id, search, limit=args.limit)
            if not frames:
                print(f"No frames matching '{search}'.")
            else:
                prev_lc = {}
                for f in frames:
                    source = get_line_code(f.filename, f.line_no)
                    print(_format_frame_line(f, source))
                    if show_locals:
                        prev_lc = _print_locals(f, changed_only, prev_lc)
            return

        thread = getattr(args, 'thread', None)
        if thread is not None:
            frames = get_frames_by_thread(run.run_id, thread, limit=args.limit)
            if not frames:
                print(f"No frames found for thread {thread}.")
            else:
                prev_lc = {}
                for f in frames:
                    source = get_line_code(f.filename, f.line_no)
                    print(_format_frame_line(f, source))
                    if show_locals:
                        prev_lc = _print_locals(f, changed_only, prev_lc)
            return

        # UX-9: --var-history
        var_history = getattr(args, 'var_history', None)
        if var_history is not None:
            import json as _json
            from pyttd.models.db import db as _dbvh
            rows = _dbvh.fetchall(
                "SELECT sequence_no, locals_snapshot, function_name, line_no"
                " FROM executionframes"
                " WHERE run_id = ? AND locals_snapshot IS NOT NULL AND locals_snapshot != ''"
                " ORDER BY sequence_no",
                (str(run.run_id),))
            found = []
            for r in rows:
                try:
                    locals_data = _json.loads(r.locals_snapshot)
                    if var_history in locals_data:
                        val = locals_data[var_history]
                        display = val.get('__repr__', str(val)) if isinstance(val, dict) else str(val)
                        found.append((r.sequence_no, r.function_name, r.line_no, display))
                except Exception:
                    pass
            if not found:
                print(f"Variable '{var_history}' not found in any recorded frame.")
            else:
                # Deduplicate consecutive identical values
                deduped = [found[0]]
                for item in found[1:]:
                    if item[3] != deduped[-1][3]:
                        deduped.append(item)
                output_format = getattr(args, 'format', 'text')
                if output_format == 'json':
                    history_list = [
                        {"seq": seq, "function": func, "line": line,
                         "variable": var_history, "value": val}
                        for seq, func, line, val in deduped[:args.limit]
                    ]
                    print(_json.dumps(history_list, indent=2))
                else:
                    print(f"Variable '{var_history}' — {len(deduped)} change(s):")
                    for seq, func, line, val in deduped[:args.limit]:
                        print(f"  #{seq:>6}  {func}:{line}  {var_history} = {val}")
            return

        # Auto-enable --frames when dependent flags are used
        if getattr(args, 'exceptions', False):
            args.event_type = 'exception_unwind'
            args.frames = True
        if getattr(args, 'event_type', None) and not args.frames:
            args.frames = True
        if getattr(args, 'file', None) and not args.frames:
            args.frames = True
        if getattr(args, 'changed_only', False):
            args.show_locals = True
        if getattr(args, 'show_locals', False) and not args.frames:
            args.frames = True
        if getattr(args, 'format', 'text') == 'json' and not args.frames:
            args.frames = True

        # UX-5: --stats
        if getattr(args, 'stats', False):
            from pyttd.models.db import db as _dbs
            # Exclude coroutine frames from exception count — async/await internals
            # use StopIteration for coroutine resumption which surfaces as exception_unwind.
            rows = _dbs.fetchall(
                "SELECT function_name,"
                " SUM(CASE WHEN frame_event = 'call' THEN 1 ELSE 0 END) AS calls,"
                " SUM(CASE WHEN frame_event = 'exception_unwind' AND is_coroutine = 0 THEN 1 ELSE 0 END) AS exceptions,"
                " MIN(CASE WHEN frame_event = 'call' THEN sequence_no END) AS first_seq"
                " FROM executionframes WHERE run_id = ? AND frame_event IN ('call', 'exception_unwind')"
                " GROUP BY function_name ORDER BY calls DESC",
                (str(run.run_id),))
            if getattr(args, 'format', 'text') == 'json':
                import json as _json_stats
                data = [{
                    "function": r.function_name,
                    "calls": r.calls,
                    "exceptions": r.exceptions,
                    "first_seq": r.first_seq,
                } for r in rows]
                print(_json_stats.dumps(data, indent=2))
            elif not rows:
                print("No function calls recorded.")
            else:
                print(f"{'Function':<40} {'Calls':>8} {'Exceptions':>12} {'First':>8}")
                print(f"{'-'*40} {'-'*8} {'-'*12} {'-'*8}")
                for r in rows:
                    exc_str = str(r.exceptions)
                    if r.exceptions > 0:
                        exc_str = _c(_RED, exc_str)
                    print(f"{r.function_name:<40} {r.calls:>8} {exc_str:>12} {r.first_seq if r.first_seq else '':>8}")
            return

        # UX-4: --line filter (accepts N or FILE:N)
        line_filter = getattr(args, 'line', None)
        if line_filter is not None:
            line_file = None
            try:
                if ':' in line_filter:
                    file_part, num_part = line_filter.rsplit(':', 1)
                    line_num = int(num_part)
                    line_file = file_part
                else:
                    line_num = int(line_filter)
            except ValueError:
                print(f"Error: --line value must be N or FILE:N (got {line_filter!r})", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
            from pyttd.models.db import db as _db2
            if line_file:
                rows = _db2.fetchall(
                    "SELECT * FROM executionframes"
                    " WHERE run_id = ? AND line_no = ? AND frame_event = 'line'"
                    " AND filename LIKE ?"
                    " ORDER BY sequence_no LIMIT ?",
                    (str(run.run_id), line_num, f"%{line_file}%", args.limit))
            else:
                rows = _db2.fetchall(
                    "SELECT * FROM executionframes"
                    " WHERE run_id = ? AND line_no = ? AND frame_event = 'line'"
                    " ORDER BY sequence_no LIMIT ?",
                    (str(run.run_id), line_num, args.limit))
            if not rows:
                print(f"No executions of line {line_filter}.")
            else:
                prev_lc = {}
                for f in rows:
                    source = get_line_code(f.filename, f.line_no)
                    print(_format_frame_line(f, source))
                    if show_locals:
                        prev_lc = _print_locals(f, changed_only, prev_lc)
            return

        if args.frames:
            # UX-4: --event-type filter, UX-10: --offset pagination
            event_type = getattr(args, 'event_type', None)
            offset = getattr(args, 'offset', 0) or 0
            extra_where = ""
            extra_params = []
            if event_type:
                extra_where += " AND frame_event = ?"
                extra_params.append(event_type)
            file_filter = getattr(args, 'file', None)
            if file_filter:
                extra_where += " AND filename LIKE ?"
                extra_params.append(f"%{file_filter}%")
            # U7: Filter coroutine-internal exception events (StopIteration noise)
            if getattr(args, 'hide_coroutine_internals', False):
                extra_where += " AND NOT (is_coroutine = 1 AND frame_event IN ('exception', 'exception_unwind'))"
            from pyttd.models.db import db as _db3
            rows = _db3.fetchall(
                "SELECT * FROM executionframes"
                " WHERE run_id = ?" + extra_where +
                " ORDER BY sequence_no LIMIT ? OFFSET ?",
                (str(run.run_id), *extra_params, args.limit, offset))
            output_format = getattr(args, 'format', 'text')
            if output_format == 'json':
                import json as _json
                frames_list = []
                for f in rows:
                    entry = {
                        "seq": f.sequence_no,
                        "event": f.frame_event,
                        "function": f.function_name,
                        "file": f.filename,
                        "line": f.line_no,
                        "depth": f.call_depth,
                        "thread_id": f.thread_id,
                    }
                    # U16: Include locals in JSON when --show-locals
                    if show_locals and f.locals_snapshot:
                        try:
                            entry["locals"] = _json.loads(f.locals_snapshot)
                        except Exception:
                            pass
                    frames_list.append(entry)
                print(_json.dumps(frames_list, indent=2))
            else:
                prev_lc = {}
                for f in rows:
                    source = get_line_code(f.filename, f.line_no)
                    print(_format_frame_line(f, source))
                    if show_locals:
                        prev_lc = _print_locals(f, changed_only, prev_lc)
    finally:
        storage.close_db()

def _cmd_replay(args):
    from pyttd.replay import ReplayController
    from pyttd.query import get_last_run, get_run_by_id
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models import storage
    import glob as globmod

    db_path = args.db
    if not db_path:
        dbs = sorted(globmod.glob(f"*{DB_NAME_SUFFIX}"), key=os.path.getmtime, reverse=True)
        if not dbs:
            print("No .pyttd.db files found. Use --db to specify path.")
            sys.exit(EXIT_USER_ERROR)
        db_path = dbs[0]
        print(f"Using database: {db_path}", file=sys.stderr)

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(EXIT_USER_ERROR)

    try:
        if args.run_id:
            try:
                run = get_run_by_id(db_path, args.run_id)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
        else:
            try:
                run = get_last_run(db_path)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
        controller = ReplayController()

        def _show_frame(result, show_context=True):
            """UX-3: Format frame output like a debugger."""
            if "error" in result:
                print(f"Error: {result['error']}")
                return
            filepath = result.get('file', '?')
            fname = os.path.basename(filepath)
            line = result.get('line', '?')
            func = result.get('function_name', '?')
            depth = result.get('call_depth', 0)
            seq = result.get('seq', 0)
            line_display = "entry" if line == 0 else str(line)
            print(f"Frame {seq} — {func} at {fname}:{line_display}")
            print(f"  call_depth: {depth}")
            # Source context: show 2 lines before and after
            if show_context and isinstance(line, int) and os.path.isfile(filepath):
                try:
                    import linecache
                    start = max(1, line - 2)
                    for ln in range(start, line + 3):
                        src = linecache.getline(filepath, ln).rstrip()
                        if src:
                            marker = ">" if ln == line else " "
                            print(f"  {marker} {ln:>4} | {src}")
                except Exception:
                    pass
            locals_data = result.get('locals')
            if locals_data and isinstance(locals_data, dict):
                print()
                print("  Locals:")
                for name, value in locals_data.items():
                    formatted = _format_local_value(value)
                    print(f"    {name:<16} = {formatted}")

        # --goto-line FILE:LINE: resolve to frame number
        goto_frame = args.goto_frame
        goto_line_arg = getattr(args, 'goto_line', None)
        if goto_line_arg:
            try:
                file_part, line_part = goto_line_arg.rsplit(':', 1)
                target_line = int(line_part)
            except (ValueError, AttributeError):
                print(f"Error: invalid --goto-line format. Use FILE:LINE (e.g., script.py:42)", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
            from pyttd.models.db import db as _dbl
            row = _dbl.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " AND filename LIKE ? AND line_no = ?"
                " ORDER BY sequence_no LIMIT 1",
                (str(run.run_id), f"%{file_part}%", target_line))
            if row is None:
                # U1: Suggest nearby lines that do have executions
                nearby = _dbl.fetchall(
                    "SELECT DISTINCT line_no FROM executionframes"
                    " WHERE run_id = ? AND frame_event = 'line'"
                    " AND filename LIKE ? AND line_no BETWEEN ? AND ?"
                    " ORDER BY ABS(line_no - ?)"
                    " LIMIT 5",
                    (str(run.run_id), f"%{file_part}%",
                     target_line - 10, target_line + 10, target_line))
                msg = f"No execution of {file_part}:{target_line} found in recording."
                if nearby:
                    lines = ', '.join(str(r.line_no) for r in nearby)
                    msg += f" Nearest recorded lines: {lines}"
                print(msg, file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
            goto_frame = row.sequence_no

        # U8: Auto-advance from call event to first line event in the function,
        # so users see arguments instead of "function entry" with no locals.
        from pyttd.models.db import db as _dbgo
        _go_frame = _dbgo.fetchone(
            "SELECT frame_event FROM executionframes"
            " WHERE run_id = ? AND sequence_no = ?",
            (str(run.run_id), goto_frame))
        if _go_frame and _go_frame.frame_event == 'call':
            _next_line = _dbgo.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line' AND sequence_no > ?"
                " ORDER BY sequence_no LIMIT 1",
                (str(run.run_id), goto_frame))
            if _next_line:
                goto_frame = _next_line.sequence_no

        # CLI always uses warm-only (no live checkpoint children after recording exits)
        result = controller.warm_goto_frame(run.run_id, goto_frame)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(EXIT_USER_ERROR)
        _show_frame(result)

        # UX-7: Interactive replay REPL
        if getattr(args, 'interactive', False):
            from pyttd.session import Session
            from pyttd.models.db import db as _dbr
            session = Session()
            first_line = _dbr.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " ORDER BY sequence_no LIMIT 1",
                (str(run.run_id),))
            first_seq = first_line.sequence_no if first_line else 0
            session.enter_replay(run.run_id, first_seq)
            if args.goto_frame > 0:
                session.goto_frame(args.goto_frame)

            # Tab completion for interactive commands
            try:
                import readline
                _commands = ['goto', 'step', 's', 'step_into', 'next', 'n',
                             'back', 'b', 'step_back', 'out', 'o', 'step_out',
                             'continue', 'c', 'vars', 'v', 'locals', 'info',
                             'eval', 'print', 'p',
                             'where', 'w', 'bt', 'stack', 'backtrace', 'frame',
                             'help', 'quit', 'q', 'exit',
                             'break', 'delete', 'breaks', 'search', 'watch',
                             'logpoint']
                def _completer(text, state):
                    matches = [c for c in _commands if c.startswith(text)]
                    return matches[state] if state < len(matches) else None
                readline.set_completer(_completer)
                readline.parse_and_bind('tab: complete')
            except ImportError:
                pass

            # Breakpoint state for interactive session
            _line_bps = []  # [{file, line}]
            _func_bps = []  # [{name}]

            # U12: Show summary on REPL startup
            total_frames = run.total_frames or 0
            func_count = _dbr.fetchval(
                "SELECT COUNT(DISTINCT function_name) FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'call'",
                (str(run.run_id),)) or 0
            print(f"\nInteractive replay ({total_frames} frames, {func_count} functions). "
                  f"Type 'help' for commands.")
            while True:
                try:
                    cmd = input("pyttd> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not cmd:
                    continue
                if cmd in ('quit', 'exit', 'q'):
                    break
                elif cmd in ('step', 's', 'step_into'):
                    r = session.step_into()
                    _show_frame(r)
                elif cmd in ('next', 'n'):
                    r = session.step_over()
                    _show_frame(r)
                elif cmd in ('back', 'b', 'step_back'):
                    r = session.step_back()
                    _show_frame(r)
                elif cmd in ('out', 'o', 'step_out'):
                    r = session.step_out()
                    _show_frame(r)
                elif cmd in ('continue', 'c'):
                    r = session.continue_forward()
                    if r.get('reason') == 'end':
                        print("  End of recording reached (no breakpoint hit).")
                    _show_frame(r)
                    # Show logpoint messages if any
                    for msg in getattr(session, '_log_messages', []):
                        print(f"  {_c(_DIM, '[log] ' + msg)}")
                elif cmd in ('vars', 'v', 'locals', 'info'):
                    variables = session.get_variables_at(session.current_frame_seq)
                    if not variables:
                        print("  (no variables)")
                    for v in variables:
                        vtype = v.get('type', '')
                        type_str = _c(_DIM, f"  ({vtype})") if vtype else ""
                        print(f"    {_c(_BOLD, v['name']):<24} = {v['value']}{type_str}")
                elif cmd.startswith('goto ') or cmd.startswith('frame '):
                    arg = cmd.split(None, 1)[1].strip() if ' ' in cmd else ''
                    try:
                        target = int(arg)
                        r = session.goto_frame(target)
                        _show_frame(r)
                    except ValueError:
                        print("Usage: goto N  (or: frame N)")
                elif cmd.startswith('eval ') or cmd.startswith('print ') or cmd.startswith('p '):
                    # Extract expression after the command keyword
                    expr = cmd.split(None, 1)[1].strip() if ' ' in cmd else ''
                    r = session.evaluate_at(session.current_frame_seq, expr, "repl")
                    print(f"  {r.get('result', '<error>')}")
                elif cmd in ('where', 'w', 'bt', 'stack', 'backtrace'):
                    stack = session.get_stack_at(session.current_frame_seq)
                    for i, frame in enumerate(stack):
                        marker = _c(_GREEN, ">") if i == 0 else " "
                        fn = os.path.basename(frame.get('file', '?'))
                        name = _c(_BOLD, frame.get('name', '?')) if i == 0 else frame.get('name', '?')
                        loc = _c(_DIM, f"at {fn}:{frame.get('line', '?')}")
                        print(f"  {marker} #{frame.get('seq', '?')} {name} {loc}")
                elif cmd.startswith('break '):
                    arg = cmd[6:].strip()
                    if ':' in arg:
                        # break FILE:LINE
                        try:
                            fpart, lpart = arg.rsplit(':', 1)
                            resolved = os.path.realpath(fpart)
                            # If realpath doesn't match a recorded file, try
                            # suffix matching (BUG-2: relative filenames).
                            from pyttd.models.db import db as _bpdb
                            match = _bpdb.fetchone(
                                "SELECT filename FROM executionframes"
                                " WHERE run_id = ? AND filename = ? LIMIT 1",
                                (str(session.run_id), resolved))
                            if not match:
                                basename = os.path.basename(fpart)
                                match = _bpdb.fetchone(
                                    "SELECT filename FROM executionframes"
                                    " WHERE run_id = ? AND filename LIKE '%/' || ?"
                                    " LIMIT 1",
                                    (str(session.run_id), basename))
                                if match:
                                    resolved = match.filename
                            bp = {"file": resolved, "line": int(lpart)}
                            _line_bps.append(bp)
                            session.set_breakpoints(_line_bps)
                            print(f"  Breakpoint {len(_line_bps)}: {os.path.basename(resolved)}:{lpart}")
                        except ValueError:
                            print("Usage: break FILE:LINE or break FUNCNAME")
                    elif arg:
                        # break FUNCNAME
                        fbp = {"name": arg}
                        _func_bps.append(fbp)
                        session.set_function_breakpoints(_func_bps)
                        print(f"  Function breakpoint {len(_func_bps)}: {arg}")
                    else:
                        print("Usage: break FILE:LINE or break FUNCNAME")
                elif cmd == 'breaks':
                    if not _line_bps and not _func_bps:
                        print("  No breakpoints set.")
                    for i, bp in enumerate(_line_bps, 1):
                        fn = os.path.basename(bp.get('file', '?'))
                        print(f"  [{i}] {fn}:{bp.get('line', '?')}")
                    for i, fbp in enumerate(_func_bps, len(_line_bps) + 1):
                        print(f"  [{i}] function: {fbp.get('name', '?')}")
                elif cmd == 'delete':
                    _line_bps.clear()
                    _func_bps.clear()
                    session.set_breakpoints([])
                    session.set_function_breakpoints([])
                    print("  All breakpoints deleted.")
                elif cmd.startswith('delete '):
                    try:
                        idx = int(cmd[7:].strip()) - 1
                        if 0 <= idx < len(_line_bps):
                            removed = _line_bps.pop(idx)
                            session.set_breakpoints(_line_bps)
                            print(f"  Deleted: {os.path.basename(removed['file'])}:{removed['line']}")
                        elif 0 <= idx - len(_line_bps) < len(_func_bps):
                            removed = _func_bps.pop(idx - len(_line_bps))
                            session.set_function_breakpoints(_func_bps)
                            print(f"  Deleted: function {removed['name']}")
                        else:
                            print(f"  No breakpoint #{idx + 1}")
                    except ValueError:
                        print("Usage: delete N or delete (all)")
                elif cmd.startswith('logpoint '):
                    # G4: Log point — emit message on hit without stopping
                    arg = cmd[9:].strip()
                    if ':' not in arg or ' ' not in arg:
                        print("Usage: logpoint FILE:LINE MESSAGE (e.g., logpoint script.py:10 value={x})")
                    else:
                        file_line, log_msg = arg.split(None, 1)
                        try:
                            fpart, lpart = file_line.rsplit(':', 1)
                            resolved = os.path.realpath(fpart)
                            from pyttd.models.db import db as _lpdb
                            match = _lpdb.fetchone(
                                "SELECT filename FROM executionframes"
                                " WHERE run_id = ? AND filename = ? LIMIT 1",
                                (str(session.run_id), resolved))
                            if not match:
                                match = _lpdb.fetchone(
                                    "SELECT filename FROM executionframes"
                                    " WHERE run_id = ? AND filename LIKE '%/' || ? LIMIT 1",
                                    (str(session.run_id), os.path.basename(fpart)))
                                if match:
                                    resolved = match.filename
                            bp = {"file": resolved, "line": int(lpart), "logMessage": log_msg}
                            _line_bps.append(bp)
                            session.set_breakpoints(_line_bps)
                            print(f"  Log point {len(_line_bps)}: {os.path.basename(resolved)}:{lpart} → \"{log_msg}\"")
                        except ValueError:
                            print("Usage: logpoint FILE:LINE MESSAGE")
                elif cmd.startswith('search '):
                    pattern = cmd[7:].strip()
                    if not pattern:
                        print("Usage: search PATTERN")
                    else:
                        like = f"%{pattern}%"
                        results = _dbr.fetchall(
                            "SELECT sequence_no, function_name, filename, line_no"
                            " FROM executionframes"
                            " WHERE run_id = ? AND frame_event = 'line'"
                            " AND (function_name LIKE ? OR filename LIKE ?)"
                            " ORDER BY sequence_no LIMIT 20",
                            (str(run.run_id), like, like))
                        if not results:
                            print(f"  No frames matching '{pattern}'")
                        else:
                            for i, r in enumerate(results, 1):
                                fn = os.path.basename(r.filename)
                                print(f"  [{i:>2}] #{r.sequence_no:>6}  {r.function_name} at {fn}:{r.line_no}")
                            print(f"  Use 'goto N' to navigate to a result.")
                elif cmd.startswith('watch '):
                    varname = cmd[6:].strip()
                    if not varname:
                        print("Usage: watch VARNAME")
                    else:
                        history = session.get_variable_history(
                            varname, 0, session.last_line_seq or 0)
                        if not history:
                            print(f"  Variable '{varname}' not found or never changes.")
                        else:
                            print(f"  '{varname}' — {len(history)} change(s):")
                            for h in history[:50]:
                                fn = os.path.basename(h.get('filename', '?'))
                                val = h.get('value', '?')
                                if isinstance(val, dict):
                                    val = val.get('__repr__', str(val))
                                print(f"    #{h['seq']:>6}  {h.get('functionName', '?')}:{h.get('line', '?')}  {varname} = {val}")
                elif cmd == 'help':
                    print("  " + _c(_BOLD, "Navigation:"))
                    print("  goto N      — jump to frame N  (aliases: frame N)")
                    print("  step (s)    — step into  (aliases: step_into)")
                    print("  next (n)    — step over")
                    print("  back (b)    — step backward  (aliases: step_back)")
                    print("  out  (o)    — step out  (aliases: step_out)")
                    print("  continue(c) — continue to next breakpoint or end")
                    print()
                    print("  " + _c(_BOLD, "Inspection:"))
                    print("  vars (v)    — show variables  (aliases: locals, info)")
                    print("  eval EXPR   — evaluate expression  (aliases: print, p)")
                    print("  where (w)   — show call stack  (aliases: bt, stack, backtrace)")
                    print("  watch VAR   — show variable history across recording")
                    print("  search PAT  — find frames matching function/file name")
                    print()
                    print("  " + _c(_BOLD, "Breakpoints:"))
                    print("  break F:L   — set breakpoint at file:line")
                    print("  logpoint F:L MSG — log message on hit without stopping")
                    print("  break FUNC  — set function breakpoint")
                    print("  breaks      — list breakpoints")
                    print("  delete [N]  — delete breakpoint N or all")
                    print()
                    print("  quit  (q)   — exit")
                else:
                    print(f"Unknown command: {cmd}. Type 'help' for available commands.")
    finally:
        storage.close_db()

def _cmd_clean(args):
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models.storage import delete_db_files, evict_old_runs
    import glob as globmod

    if args.keep is not None:
        # Evict old runs from a specific DB
        db_path = args.db
        if not db_path:
            dbs = sorted(globmod.glob(f"*{DB_NAME_SUFFIX}"), key=os.path.getmtime, reverse=True)
            if not dbs:
                print("No .pyttd.db files found in current directory. Use --db to specify path.")
                sys.exit(EXIT_USER_ERROR)
            db_path = dbs[0]
        if not os.path.exists(db_path):
            print(f"Database file not found: {db_path}")
            sys.exit(EXIT_USER_ERROR)
        try:
            evicted = evict_old_runs(db_path, args.keep, dry_run=args.dry_run)
        except Exception as e:
            import sqlite3 as _sq
            if isinstance(e, (_sq.DatabaseError, _sq.OperationalError)):
                print(f"Error: cannot read database: {e}", file=sys.stderr)
                sys.exit(EXIT_USER_ERROR)
            raise
        if not evicted:
            print(f"Nothing to evict (database has {args.keep} or fewer runs).")
        elif args.dry_run:
            print(f"Would evict {len(evicted)} run(s):")
            for rid in evicted:
                print(f"  {rid}")
        else:
            print(f"Evicted {len(evicted)} run(s). Database vacuumed.")
        return

    if args.all:
        # Delete all .pyttd.db files in CWD
        dbs = globmod.glob(f"*{DB_NAME_SUFFIX}")
        if not dbs:
            print("No .pyttd.db files found in current directory.")
            return
        for db in dbs:
            if args.dry_run:
                print(f"Would delete: {db}")
            else:
                delete_db_files(db)
                print(f"Deleted: {db}")
        return

    if args.db:
        if not os.path.exists(args.db):
            print(f"Database file not found: {args.db}")
            sys.exit(EXIT_USER_ERROR)
        if args.dry_run:
            print(f"Would delete: {args.db}")
        else:
            delete_db_files(args.db)
            print(f"Deleted: {args.db}")
        return

    # Default: list .pyttd.db files and prompt
    dbs = globmod.glob(f"*{DB_NAME_SUFFIX}")
    if not dbs:
        print("No .pyttd.db files found in current directory.")
        return
    print("Found .pyttd.db files:")
    for db in dbs:
        size_mb = os.path.getsize(db) / (1024 * 1024)
        print(f"  {db} ({size_mb:.1f} MB)")
    print("\nUse --db <path> to delete a specific file, --all to delete all, or --keep N to evict old runs.")


def _parse_env_file(filepath: str) -> dict:
    result = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            result[key] = value
    return result

def _cmd_export(args):
    from pyttd.export import export_perfetto
    if not os.path.isfile(args.db):
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        sys.exit(EXIT_USER_ERROR)
    # UX-11: Overwrite warning + parent dir validation
    output_dir = os.path.dirname(args.output)
    if output_dir and not os.path.isdir(output_dir):
        print(f"Error: output directory does not exist: {output_dir}", file=sys.stderr)
        sys.exit(EXIT_USER_ERROR)
    if os.path.exists(args.output) and not getattr(args, 'force', False):
        print(f"Error: {args.output} already exists. Use --force to overwrite.", file=sys.stderr)
        sys.exit(EXIT_USER_ERROR)
    run_id = None
    if args.run_id:
        from pyttd.query import get_run_by_id
        try:
            run = get_run_by_id(args.db, args.run_id)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(EXIT_USER_ERROR)
        run_id = run.run_id
    export_perfetto(args.db, args.output, run_id=run_id)
    print(f"Exported to {args.output}")
