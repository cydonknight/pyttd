"""Export trace data to external formats (Phase 10C).

Currently supports Perfetto/Chrome Trace Event Format.
"""
import json
from pyttd.models import storage
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.checkpoints import Checkpoint
from pyttd.models.io_events import IOEvent


def export_perfetto(db_path: str, output_path: str, run_id: int | None = None):
    """Export a recording to Chrome Trace Event Format (Perfetto-compatible).

    Output is a JSON file with {"traceEvents": [...]} viewable in
    chrome://tracing or ui.perfetto.dev.
    """
    storage.connect_to_db(db_path)
    try:
        storage.initialize_schema([Runs, ExecutionFrames, Checkpoint, IOEvent])
        if run_id is None:
            last_run = (Runs.select()
                        .order_by(Runs.timestamp_start.desc())
                        .limit(1).first())
            if not last_run:
                with open(output_path, 'w') as f:
                    json.dump({"traceEvents": []}, f)
                return
            run_id = last_run.run_id

        with open(output_path, 'w') as f:
            f.write('{"traceEvents": [')
            first = True
            for frame in ExecutionFrames.select().where(
                ExecutionFrames.run_id == run_id
            ).order_by(ExecutionFrames.sequence_no).iterator():
                ts_us = int(frame.timestamp * 1_000_000)
                tid = frame.thread_id or 0
                base = {
                    "pid": 1,
                    "tid": tid,
                    "ts": ts_us,
                    "name": frame.function_name,
                }

                event = None
                if frame.frame_event == 'call':
                    event = {
                        **base,
                        "ph": "B",
                        "cat": "call",
                        "args": {
                            "file": frame.filename,
                            "line": frame.line_no,
                            "depth": frame.call_depth,
                        },
                    }
                elif frame.frame_event in ('return', 'exception_unwind'):
                    event = {
                        **base,
                        "ph": "E",
                        "cat": frame.frame_event,
                        "args": {
                            "file": frame.filename,
                            "line": frame.line_no,
                        },
                    }
                elif frame.frame_event == 'line':
                    event = {
                        **base,
                        "ph": "i",
                        "s": "t",
                        "cat": "line",
                        "args": {
                            "file": frame.filename,
                            "line": frame.line_no,
                        },
                    }
                elif frame.frame_event == 'exception':
                    event = {
                        **base,
                        "ph": "i",
                        "s": "t",
                        "cat": "exception",
                        "args": {
                            "file": frame.filename,
                            "line": frame.line_no,
                            "category": "exception",
                        },
                    }

                if event is not None:
                    if not first:
                        f.write(',')
                    first = False
                    json.dump(event, f)
            f.write(']}')
    finally:
        storage.close_db()
