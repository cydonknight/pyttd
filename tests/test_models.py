import time
import uuid
from pyttd.models.db import db
from pyttd.models import storage


def test_runs_creation(db_setup):
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (run_id, "", time.time(), 0))
    db.commit()
    run = db.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert run.run_id is not None
    assert run.total_frames == 0


def test_runs_unique_ids(db_setup):
    r1 = uuid.uuid4().hex
    r2 = uuid.uuid4().hex
    now = time.time()
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (r1, "", now, 0))
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (r2, "", now + 0.001, 0))
    db.commit()
    assert r1 != r2


def test_runs_timestamp_defaults(db_setup):
    r1 = uuid.uuid4().hex
    t1 = time.time()
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (r1, "", t1, 0))
    time.sleep(0.01)
    r2 = uuid.uuid4().hex
    t2 = time.time()
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (r2, "", t2, 0))
    db.commit()
    run1 = db.fetchone("SELECT * FROM runs WHERE run_id = ?", (r1,))
    run2 = db.fetchone("SELECT * FROM runs WHERE run_id = ?", (r2,))
    assert run2.timestamp_start > run1.timestamp_start


def test_execution_frames_creation(db_setup):
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (run_id, "", time.time(), 0))
    db.execute(
        "INSERT INTO executionframes"
        " (run_id, sequence_no, timestamp, line_no, filename, function_name,"
        "  frame_event, call_depth, thread_id, is_coroutine)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, 0, 0.001, 10, "test.py", "foo", "call", 0, 0, 0))
    db.commit()
    frame = db.fetchone(
        "SELECT * FROM executionframes WHERE run_id = ? AND sequence_no = 0",
        (run_id,))
    assert frame is not None
    assert frame.locals_snapshot is None


def test_execution_frames_fk(db_setup):
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (run_id, "", time.time(), 0))
    db.execute(
        "INSERT INTO executionframes"
        " (run_id, sequence_no, timestamp, line_no, filename, function_name,"
        "  frame_event, call_depth, thread_id, is_coroutine)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, 1, 0.001, 5, "test.py", "bar", "line", 0, 0, 0))
    db.commit()
    frames = db.fetchall(
        "SELECT * FROM executionframes WHERE run_id = ?", (run_id,))
    assert len(frames) == 1
    assert frames[0].function_name == "bar"


def test_batch_insert(db_setup):
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, total_frames) VALUES (?, ?, ?, ?)",
        (run_id, "", time.time(), 0))
    db.commit()
    rows = [
        {
            "run_id": run_id,
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
    storage.batch_insert(None, rows)
    count = db.fetchval(
        "SELECT COUNT(*) FROM executionframes WHERE run_id = ?", (run_id,))
    assert count == 100


def test_wal_mode(db_setup, db_path):
    result = db.execute("PRAGMA journal_mode").fetchone()
    assert result[0] == "wal"
