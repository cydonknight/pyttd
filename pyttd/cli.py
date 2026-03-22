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
    record_parser.add_argument('--secret-patterns', nargs='+', default=None,
                               help='Additional secret patterns for variable redaction')
    record_parser.add_argument('--include', nargs='+', default=None,
                               help='Only record functions matching these patterns (supports glob: *, ?, [])')
    record_parser.add_argument('--include-file', nargs='+', default=None,
                               help='Only record functions in files matching these glob patterns')
    record_parser.add_argument('--exclude', nargs='+', default=None,
                               help='Exclude functions matching these patterns from recording')
    record_parser.add_argument('--exclude-file', nargs='+', default=None,
                               help='Exclude files matching these glob patterns from recording')
    record_parser.add_argument('--max-frames', type=int, default=0,
                               help='Maximum frames to record (0 = unlimited)')

    query_parser = subparsers.add_parser('query', help='Query trace data')
    query_parser.add_argument('--last-run', action='store_true')
    query_parser.add_argument('--frames', action='store_true')
    query_parser.add_argument('--limit', type=int, default=50)
    query_parser.add_argument('--db', type=str, default=None)

    replay_parser = subparsers.add_parser('replay', help='Replay a recorded session')
    replay_parser.add_argument('--last-run', action='store_true')
    replay_parser.add_argument('--goto-frame', type=int, default=0)
    replay_parser.add_argument('--db', type=str, default=None)

    serve_parser = subparsers.add_parser('serve', help='Start JSON-RPC debug server')
    serve_group = serve_parser.add_mutually_exclusive_group(required=True)
    serve_group.add_argument('--script', help='Script to record and debug')
    serve_group.add_argument('--db', type=str, help='Existing .pyttd.db to replay (no recording)')
    serve_parser.add_argument('--module', action='store_true')
    serve_parser.add_argument('--cwd', default='.')
    serve_parser.add_argument('--checkpoint-interval', type=int, default=1000)
    serve_parser.add_argument('--include', nargs='+', default=None,
                              help='Only record functions matching these patterns')
    serve_parser.add_argument('--include-file', nargs='+', default=None,
                              help='Only record functions in files matching these glob patterns')
    serve_parser.add_argument('--exclude', nargs='+', default=None,
                              help='Exclude functions matching these patterns from recording')
    serve_parser.add_argument('--exclude-file', nargs='+', default=None,
                              help='Exclude files matching these glob patterns from recording')
    serve_parser.add_argument('--max-frames', type=int, default=0,
                              help='Maximum frames to record (0 = unlimited)')
    serve_parser.add_argument('--env', nargs='+', default=None,
                              help='Environment variables (KEY=VALUE format)')
    serve_parser.add_argument('--env-file', type=str, default=None,
                              help='Path to dotenv file to load environment variables from')

    export_parser = subparsers.add_parser('export', help='Export trace data')
    export_parser.add_argument('--format', choices=['perfetto'], default='perfetto',
                               help='Export format (default: perfetto)')
    export_parser.add_argument('--db', type=str, required=True,
                               help='Path to .pyttd.db file')
    export_parser.add_argument('-o', '--output', type=str, required=True,
                               help='Output file path')

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

def _cmd_record(args):
    from pyttd.config import PyttdConfig
    from pyttd.recorder import Recorder
    from pyttd.runner import Runner
    from pyttd.models.constants import DB_NAME_SUFFIX
    from pyttd.models.storage import delete_db_files

    if args.module:
        script_name = args.script.replace('.', '_')
        script_dir = os.getcwd()
        script_abs = args.script
    else:
        if not os.path.isfile(args.script):
            print(f"Error: script not found: {args.script}", file=sys.stderr)
            sys.exit(1)
        script_abs = os.path.realpath(args.script)
        script_name = os.path.splitext(os.path.basename(script_abs))[0]
        script_dir = os.path.dirname(script_abs) or '.'
    db_path = os.path.join(script_dir, script_name + DB_NAME_SUFFIX)
    cwd = script_dir

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
    config = PyttdConfig(**config_kwargs)
    recorder = Recorder(config)
    runner = Runner()

    delete_db_files(db_path)
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
    if script_error:
        print(f"Script exited with {type(script_error).__name__}: {script_error}")
    print(f"Recording complete: {stats}")
    if isinstance(script_error, SystemExit):
        sys.exit(script_error.code)
    elif isinstance(script_error, KeyboardInterrupt):
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
        )
    server.run()

def _cmd_query(args):
    from pyttd.query import get_last_run, get_frames, get_line_code
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
        run = get_last_run(db_path)
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
    from pyttd.query import get_last_run
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
        run = get_last_run(db_path)
        controller = ReplayController()
        # CLI always uses warm-only (no live checkpoint children after recording exits)
        result = controller.warm_goto_frame(run.run_id, args.goto_frame)
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)
        print(f"Frame {args.goto_frame}: {result}")
    finally:
        storage.close_db()

def _parse_env_file(filepath: str) -> dict:
    result = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
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
    export_perfetto(args.db, args.output)
    print(f"Exported to {args.output}")
