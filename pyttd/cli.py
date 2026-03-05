import argparse
import os
import sys

def main():
    parser = argparse.ArgumentParser(prog='pyttd', description='Python Time-Travel Debugger')
    subparsers = parser.add_subparsers(dest='command')

    record_parser = subparsers.add_parser('record', help='Record script execution')
    record_parser.add_argument('script', help='Script path or module name (with --module)')
    record_parser.add_argument('--module', action='store_true', help='Treat script as module name')
    record_parser.add_argument('--checkpoint-interval', type=int, default=1000)
    record_parser.add_argument('--args', nargs='*', default=[])

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
    serve_parser.add_argument('--script', required=True)
    serve_parser.add_argument('--module', action='store_true')
    serve_parser.add_argument('--cwd', default='.')
    serve_parser.add_argument('--checkpoint-interval', type=int, default=1000)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == 'record':
        _cmd_record(args)
    elif args.command == 'query':
        _cmd_query(args)
    elif args.command == 'replay':
        print("pyttd replay: not yet implemented (Phase 2)")
    elif args.command == 'serve':
        print("pyttd serve: not yet implemented (Phase 3)")

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
        script_abs = os.path.abspath(args.script)
        script_name = os.path.splitext(os.path.basename(script_abs))[0]
        script_dir = os.path.dirname(script_abs) or '.'
    db_path = os.path.join(script_dir, script_name + DB_NAME_SUFFIX)
    cwd = script_dir

    config = PyttdConfig(checkpoint_interval=args.checkpoint_interval)
    recorder = Recorder(config)
    runner = Runner()

    delete_db_files(db_path)
    recorder.start(db_path, script_path=script_abs)
    script_error = None
    try:
        if args.module:
            runner.run_module(args.script, cwd, args.args)
        else:
            runner.run_script(script_abs, cwd, args.args)
    except BaseException as e:
        script_error = e
    finally:
        stats = recorder.stop()
        recorder.cleanup()
    if script_error:
        print(f"Script exited with {type(script_error).__name__}: {script_error}")
    print(f"Recording complete: {stats}")

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

    run = get_last_run(db_path)
    print(f"Run: {run.run_id} ({run.script_path or 'unknown'}) — {run.total_frames} frames")

    if args.frames:
        frames = get_frames(run.run_id, limit=args.limit)
        for f in frames:
            source = get_line_code(f.filename, f.line_no)
            print(f"  #{f.sequence_no:>6} {f.frame_event:<18} {f.function_name}:{f.line_no}  {source}")

    storage.close_db()
