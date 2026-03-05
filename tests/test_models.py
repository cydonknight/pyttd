import time
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models import storage
from pyttd.models.base import db


def test_runs_creation(db_setup):
    run = Runs.create()
    assert run.run_id is not None
    assert run.total_frames == 0


def test_runs_unique_ids(db_setup):
    r1 = Runs.create()
    r2 = Runs.create()
    assert r1.run_id != r2.run_id


def test_runs_timestamp_defaults(db_setup):
    r1 = Runs.create()
    time.sleep(0.01)
    r2 = Runs.create()
    assert r2.timestamp_start > r1.timestamp_start


def test_execution_frames_creation(db_setup):
    run = Runs.create()
    frame = ExecutionFrames.create(
        run_id=run.run_id,
        sequence_no=0,
        timestamp=0.001,
        line_no=10,
        filename="test.py",
        function_name="foo",
        frame_event="call",
        call_depth=0,
    )
    assert frame.frame_id is not None
    assert frame.locals_snapshot is None


def test_execution_frames_fk(db_setup):
    run = Runs.create()
    ExecutionFrames.create(
        run_id=run.run_id,
        sequence_no=1,
        timestamp=0.001,
        line_no=5,
        filename="test.py",
        function_name="bar",
        frame_event="line",
        call_depth=0,
    )
    frames = list(run.frames)
    assert len(frames) == 1
    assert frames[0].function_name == "bar"


def test_batch_insert(db_setup):
    run = Runs.create()
    rows = [
        {
            "run_id": run.run_id,
            "sequence_no": i,
            "timestamp": i * 0.001,
            "line_no": i + 1,
            "filename": "test.py",
            "function_name": "func",
            "frame_event": "line",
            "call_depth": 0,
        }
        for i in range(100)
    ]
    storage.batch_insert(ExecutionFrames, rows)
    count = ExecutionFrames.select().where(ExecutionFrames.run_id == run.run_id).count()
    assert count == 100


def test_wal_mode(db_setup, db_path):
    result = db.execute_sql("PRAGMA journal_mode").fetchone()
    assert result[0] == "wal"
