import logging
import os

from pyttd.models.db import db
from pyttd.models import schema
from pyttd.models.constants import DB_NAME_SUFFIX

logger = logging.getLogger(__name__)

# Column order for ExecutionFrames batch inserts.
_EF_COLUMNS = (
    'run_id', 'sequence_no', 'timestamp', 'line_no', 'filename',
    'function_name', 'frame_event', 'call_depth', 'locals_snapshot',
    'thread_id', 'is_coroutine',
)

_EF_INSERT_SQL = (
    "INSERT INTO executionframes"
    " (run_id, sequence_no, timestamp, line_no, filename,"
    "  function_name, frame_event, call_depth, locals_snapshot,"
    "  thread_id, is_coroutine)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def compute_db_path(script: str | None = None, is_module: bool = False,
                    cwd: str = '.', explicit_path: str | None = None) -> str:
    """Compute the database path for a recording.

    Args:
        script: Script path or module name.
        is_module: True if script is a module name.
        cwd: Working directory (used for module mode).
        explicit_path: If provided, use this path directly.
    """
    if explicit_path:
        return os.path.abspath(explicit_path)
    if is_module:
        return os.path.join(cwd, script.replace('.', '_') + DB_NAME_SUFFIX)
    script_abs = os.path.realpath(script)
    name = os.path.splitext(os.path.basename(script_abs))[0]
    return os.path.join(os.path.dirname(script_abs) or '.', name + DB_NAME_SUFFIX)

def connect_to_db(db_path: str):
    """Initialize the deferred database with the given path."""
    db.init(db_path)
    logger.info("Connected to database: %s", db_path)

def initialize_schema():
    """Create tables and indexes via DDL, then run migrations."""
    db.get_connection().executescript(schema.SCHEMA_DDL)
    for sql in schema.MIGRATION_SQL:
        try:
            db.execute(sql)
        except Exception:
            pass  # Column already exists

def delete_db_files(db_path: str):
    """Delete a SQLite database, WAL/SHM companions, and binlog if present."""
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)
    binlog_path = db_path.replace(".pyttd.db", ".pyttd.binlog")
    if binlog_path != db_path and os.path.exists(binlog_path):
        os.remove(binlog_path)

def batch_insert(model_class, rows: list[dict], batch_size: int = 500):
    """Batch-insert rows into the ExecutionFrames table.

    The model_class parameter is accepted for backward compatibility but
    ignored; all inserts target the executionframes table.
    """
    with db.atomic():
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            params = [
                tuple(row.get(col, 0 if col in ('thread_id', 'is_coroutine') else None)
                      for col in _EF_COLUMNS)
                for row in batch
            ]
            db.executemany(_EF_INSERT_SQL, params)

def close_db():
    """Close the database connection."""
    if not db.is_closed():
        db.close()


def evict_old_runs(db_path: str, keep: int, dry_run: bool = False) -> list:
    """Evict all but the last `keep` runs from the database.

    Returns list of evicted run_ids.
    """
    connect_to_db(db_path)
    initialize_schema()
    try:
        result = _evict_old_runs_internal(keep, dry_run)
        if not dry_run and result:
            db.execute('VACUUM')
        return result
    finally:
        close_db()


def _evict_old_runs_internal(keep: int, dry_run: bool = False) -> list:
    """Evict old runs assuming DB is already connected. Returns list of evicted run_ids."""
    all_runs = db.fetchall(
        "SELECT run_id FROM runs ORDER BY timestamp_start DESC"
    )
    if len(all_runs) <= keep:
        return []

    to_evict = all_runs[keep:]
    evicted_ids = [r.run_id for r in to_evict]

    if dry_run:
        return evicted_ids

    with db.atomic():
        for run_id in evicted_ids:
            db.execute("DELETE FROM ioevent WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM checkpoint WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM executionframes WHERE run_id = ?", (run_id,))
            db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    return evicted_ids
