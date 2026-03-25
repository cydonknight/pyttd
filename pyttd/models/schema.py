"""DDL constants and record-creation helpers for raw sqlite3.

Replaces Peewee model class definitions for schema management.
"""

import uuid
from datetime import datetime
from pyttd.models.db import db


SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    timestamp_start REAL,
    timestamp_end REAL,
    script_path TEXT,
    total_frames INTEGER DEFAULT 0,
    is_attach INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS executionframes (
    frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence_no INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    line_no INTEGER NOT NULL,
    filename TEXT NOT NULL,
    function_name TEXT NOT NULL,
    frame_event TEXT NOT NULL,
    call_depth INTEGER NOT NULL,
    locals_snapshot TEXT,
    thread_id INTEGER DEFAULT 0,
    is_coroutine INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS executionframes_run_id_sequence_no
    ON executionframes(run_id, sequence_no);
CREATE INDEX IF NOT EXISTS executionframes_run_id
    ON executionframes(run_id);
CREATE INDEX IF NOT EXISTS executionframes_run_id_filename_line_no
    ON executionframes(run_id, filename, line_no);
CREATE INDEX IF NOT EXISTS executionframes_run_id_function_name
    ON executionframes(run_id, function_name);
CREATE INDEX IF NOT EXISTS executionframes_run_id_frame_event_sequence_no
    ON executionframes(run_id, frame_event, sequence_no);
CREATE INDEX IF NOT EXISTS executionframes_run_id_call_depth_sequence_no
    ON executionframes(run_id, call_depth, sequence_no);
CREATE INDEX IF NOT EXISTS executionframes_run_id_thread_id_sequence_no
    ON executionframes(run_id, thread_id, sequence_no);
CREATE TABLE IF NOT EXISTS checkpoint (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence_no INTEGER NOT NULL,
    child_pid INTEGER,
    is_alive INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS checkpoint_run_id_sequence_no
    ON checkpoint(run_id, sequence_no);
CREATE TABLE IF NOT EXISTS ioevent (
    io_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence_no INTEGER NOT NULL,
    io_sequence INTEGER NOT NULL,
    function_name TEXT NOT NULL,
    return_value BLOB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ioevent_run_id_sequence_no_io_sequence
    ON ioevent(run_id, sequence_no, io_sequence);
"""

SECONDARY_INDEX_DROP = [
    'DROP INDEX IF EXISTS "executionframes_run_id_filename_line_no"',
    'DROP INDEX IF EXISTS "executionframes_run_id_function_name"',
    'DROP INDEX IF EXISTS "executionframes_run_id_frame_event_sequence_no"',
    'DROP INDEX IF EXISTS "executionframes_run_id_call_depth_sequence_no"',
    'DROP INDEX IF EXISTS "executionframes_run_id_thread_id_sequence_no"',
]

SECONDARY_INDEX_CREATE = [
    'CREATE INDEX IF NOT EXISTS "executionframes_run_id_filename_line_no" ON executionframes(run_id, filename, line_no)',
    'CREATE INDEX IF NOT EXISTS "executionframes_run_id_function_name" ON executionframes(run_id, function_name)',
    'CREATE INDEX IF NOT EXISTS "executionframes_run_id_frame_event_sequence_no" ON executionframes(run_id, frame_event, sequence_no)',
    'CREATE INDEX IF NOT EXISTS "executionframes_run_id_call_depth_sequence_no" ON executionframes(run_id, call_depth, sequence_no)',
    'CREATE INDEX IF NOT EXISTS "executionframes_run_id_thread_id_sequence_no" ON executionframes(run_id, thread_id, sequence_no)',
]

MIGRATION_SQL = [
    'ALTER TABLE runs ADD COLUMN is_attach INTEGER DEFAULT 0',
]


def create_run(script_path=None, is_attach=False):
    """Create a new Runs record. Returns run_id string (hex, no dashes)."""
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, timestamp_start, script_path, is_attach, total_frames)"
        " VALUES (?, ?, ?, ?, 0)",
        (run_id, datetime.now().timestamp(), script_path, int(is_attach)))
    db.commit()
    return run_id


def update_run(run_id, **kwargs):
    """Update a Runs record by run_id."""
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [str(run_id)]
    db.execute(f"UPDATE runs SET {sets} WHERE run_id = ?", vals)
    db.commit()


def create_checkpoint(run_id, sequence_no, child_pid):
    """Insert a checkpoint record."""
    db.execute(
        "INSERT INTO checkpoint (run_id, sequence_no, child_pid) VALUES (?, ?, ?)",
        (str(run_id), sequence_no, child_pid))
    db.commit()


def create_io_event(run_id, sequence_no, io_sequence, function_name, return_value):
    """Insert an IO event record."""
    db.execute(
        "INSERT INTO ioevent (run_id, sequence_no, io_sequence, function_name, return_value) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(run_id), sequence_no, io_sequence, function_name, return_value))
    db.commit()
