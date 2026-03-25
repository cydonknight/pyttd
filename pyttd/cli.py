import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)

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
    record_parser.add_argument('--args', nargs='+', default=[])
    record_parser.add_argument('--no-redact', action='store_true',
                               help='Disable secrets redaction in recorded variables')
    record_parser.add_argument('--secret-patterns', action='append', default=None,
                               help='Additional secret pattern for variable redaction (repeatable)')
    record_parser.add_argument('--include', action='append', default=None,
                               help='Only record functions matching this pattern (repeatable, supports glob: *, ?, [])')
    record_parser.add_argument('--include-file', action='append', default=None,
                               help='Only record functions in files matching this glob pattern (repeatable)')
    record_parser.add_argument('--exclude', action='append', default=None,
                               help='Exclude functions matching this pattern from recording (repeatable)')
    record_parser.add_argument('--exclude-file', action='append', default=None,
                               help='Exclude files matching this glob pattern from recording (repeatable)')
    record_parser.add_argument('--max-frames', type=int, default=0,
                               help='Maximum frames to record (0 = unlimited)')
    record_parser.add_argument('--db-path', type=str, default=None,
                               help='Custom database path (default: <script>.pyttd.db)')
    record_parser.add_argument('--max-db-size', type=int, default=0,
                               help='Warn when DB exceeds this size in MB (0 = unlimited)')
    record_parser.add_argument('--keep-runs', type=int, default=0,
                               help='Keep only last N runs, evict older (0 = keep all)')
    record_parser.add_argument('--checkpoint-memory-limit', type=int, default=0,
                               help='Checkpoint memory limit in MB (0 = unlimited)')

    query_parser = subparsers.add_parser('query', help='Query trace data')
    query_parser.add_argument('--last-run', action='store_true')
    query_parser.add_argument('--list-runs', action='store_true', help='List all runs in database')
    query_parser.add_argument('--run-id', type=str, default=None,
                              help='Query specific run by UUID or prefix')
    query_parser.add_argument('--frames', action='store_true')
    query_parser.add_argument('--limit', type=int, default=50)
    query_parser.add_argument('--db', type=str, default=None)

    replay_parser = subparsers.add_parser('replay', help='Replay a recorded session')
    replay_parser.add_argument('--last-run', action='store_true')
    replay_parser.add_argument('--run-id', type=str, default=None,
                               help='Replay specific run by UUID or prefix')
    replay_parser.add_argument('--goto-frame', type=int, default=0)
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
                              help='Only record functions in files matching this glob pattern (repeatable)')
    serve_parser.add_argument('--exclude', action='append', default=None,
                              help='Exclude functions matching this pattern from recording (repeatable)')
    serve_parser.add_argument('--exclude-file', action='append', default=None,
                              help='Exclude files matching this glob pattern from recording (repeatable)')
    serve_parser.add_argument('--max-frames', type=int, default=0,
                              help='Maximum frames to record (0 = unlimited)')
    serve_parser.add_argument('--env', nargs='+', default=None,
                              help='Environment variables (KEY=VALUE format)')
    serve_parser.add_argument('--env-file', type=str, default=None,
                              help='Path to dotenv file to load environment variables from')
    serve_parser.add_argument('--db-path', type=str, default=None,
                              help='Custom database path (default: <script>.pyttd.db)')
    serve_parser.add_argument('--max-db-size', type=int, default=0,
                              help='Warn when DB exceeds this size in MB (0 = unlimited)')
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

    clean_parser = subparsers.add_parser('clean', help='Clean up database files')
    clean_parser.add_argument('--db', type=str, default=None,
                              help='Specific database file to clean')
    clean_parser.add_argument('--all', action='store_true',
                              help='Delete all .pyttd.db files in current directory')
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
        sys.exit(1)

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

def _cmd_record(args):
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.models.storage import compute_db_path

    if args.module:
        script_abs = args.script
        cwd = os.getcwd()
    else:
        if not os.path.isfile(args.script):
            print(f"Error: script not found: {args.script}", file=sys.stderr)
            sys.exit(1)
        script_abs = os.path.realpath(args.script)
        cwd = os.path.dirname(script_abs) or '.'

    db_path = compute_db_path(
        args.script, is_module=args.module, cwd=cwd,
        explicit_path=getattr(args, 'db_path', None),
    )

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

    recorder.start(db_path, script_path=script_abs)
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
        try:
            stats = recorder.stop()
        finally:
            recorder.cleanup()
    # Determine if KeyboardInterrupt was caused by a recording limit
    limit_stop = (isinstance(script_error, KeyboardInterrupt) and (
        (args.max_frames > 0 and stats.get('frame_count', 0) >= args.max_frames) or
        args.max_db_size > 0
    ))

    if script_error:
        if limit_stop:
            if args.max_frames > 0 and stats.get('frame_count', 0) >= args.max_frames:
                print(f"Recording stopped: frame limit reached ({stats.get('frame_count', 0)} frames)")
            else:
                print(f"Recording stopped: database size limit reached")
        else:
            print(f"Script exited with {type(script_error).__name__}: {script_error}")
    print(f"Recording complete: {stats}")

    # Multi-run guidance
    from pyttd.models import storage as _storage
    try:
        _storage.connect_to_db(db_path)
        _storage.initialize_schema()
        from pyttd.models.db import db as _db
        run_count = _db.fetchval("SELECT COUNT(*) FROM runs") or 0
        if run_count > 1:
            print(f"Database contains {run_count} runs. Use 'pyttd query --list-runs --db {db_path}' to see all.")
    except Exception:
        pass
    finally:
        _storage.close_db()

    if isinstance(script_error, SystemExit):
        sys.exit(script_error.code)
    elif isinstance(script_error, KeyboardInterrupt) and not limit_stop:
        raise script_error

def _cmd_serve(args):
    from pyttd.server import PyttdServer

    if args.script:
        if not args.module and not os.path.isfile(args.script):
            print(f"Error: script not found: {args.script}", file=sys.stderr)
            sys.exit(1)
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
            sys.exit(1)
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
    from pyttd.query import get_last_run, get_all_runs, get_run_by_id, get_frames, get_line_code
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models import storage
    import glob as globmod

    db_path = args.db
    if not db_path:
        dbs = sorted(globmod.glob(f"*{DB_NAME_SUFFIX}"), key=os.path.getmtime, reverse=True)
        if not dbs:
            print("No .pyttd.db files found in current directory. Use --db to specify path.")
            sys.exit(1)
        db_path = dbs[0]

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(1)

    try:
        if args.list_runs:
            runs = get_all_runs(db_path)
            if not runs:
                print("No runs found.")
                return
            print(f"{'Run ID':<40} {'Script':<30} {'Frames':>10} {'Started':<23} {'Duration':>10}")
            print(f"{'-'*40} {'-'*30} {'-'*10} {'-'*23} {'-'*10}")
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
                print(f"{str(r.run_id):<40} {script:<30} {r.total_frames:>10} {started:<23} {duration:>10}")
            return

        if args.run_id:
            try:
                run = get_run_by_id(db_path, args.run_id)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                run = get_last_run(db_path)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        print(f"Run: {run.run_id} ({run.script_path or 'unknown'}) — {run.total_frames} frames")

        if args.frames:
            frames = get_frames(run.run_id, limit=args.limit)
            for f in frames:
                source = get_line_code(f.filename, f.line_no)
                print(f"  #{f.sequence_no:>6} {f.frame_event:<18} {f.function_name}:{f.line_no}  {source}")
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
            sys.exit(1)
        db_path = dbs[0]

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        sys.exit(1)

    try:
        if args.run_id:
            try:
                run = get_run_by_id(db_path, args.run_id)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                run = get_last_run(db_path)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        controller = ReplayController()
        # CLI always uses warm-only (no live checkpoint children after recording exits)
        result = controller.warm_goto_frame(run.run_id, args.goto_frame)
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)
        print(f"Frame {args.goto_frame}: {result}")
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
                sys.exit(1)
            db_path = dbs[0]
        if not os.path.exists(db_path):
            print(f"Database file not found: {db_path}")
            sys.exit(1)
        evicted = evict_old_runs(db_path, args.keep, dry_run=args.dry_run)
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
            sys.exit(1)
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
        sys.exit(1)
    run_id = None
    if args.run_id:
        from pyttd.query import get_run_by_id
        try:
            run = get_run_by_id(args.db, args.run_id)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        run_id = run.run_id
    export_perfetto(args.db, args.output, run_id=run_id)
    print(f"Exported to {args.output}")
