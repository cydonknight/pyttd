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
    """Create tables and indexes via DDL, then run unapplied migrations.

    Migration versioning (Issue 4b/5): the pyttd_meta table stores the
    highest-applied migration index. Only migrations beyond that index
    run. Existing DBs without pyttd_meta start at version 0 and re-run
    the idempotent ALTER TABLE migrations (which catch "duplicate column"
    and skip). New non-idempotent migrations can rely on one-shot execution.
    """
    db.get_connection().executescript(schema.SCHEMA_DDL)

    try:
        row = db.fetchone(
            "SELECT value FROM pyttd_meta WHERE key = 'migration_version'")
        current = int(row.value) if row and row.value is not None else 0
    except Exception:
        current = 0

    target = len(schema.MIGRATION_SQL)
    for i in range(current, target):
        sql = schema.MIGRATION_SQL[i]
        try:
            db.execute(sql)
        except Exception as e:
            msg = str(e).lower()
            # Existing DBs (pre-versioning) will have these columns already.
            # Idempotent ALTER TABLE ADD COLUMN fails with "duplicate column"
            # which is expected on re-run. Re-raise anything else.
            if 'duplicate column' not in msg:
                logger.warning("Migration %d failed: %s", i, e)

    try:
        db.execute(
            "INSERT OR REPLACE INTO pyttd_meta (key, value) VALUES (?, ?)",
            ('migration_version', str(target)))
        db.commit()
    except Exception:
        logger.debug("Could not record migration_version (table may be unwritable)")

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


# Track which DB paths have been notified about lazy index building so we
# only print the stderr notice once per session per DB.
_notified_lazy_build: set = set()


def ensure_secondary_indexes(quiet: bool = False) -> bool:
    """Build secondary indexes on executionframes if they don't already exist.

    Idempotent: checks PRAGMA index_list first and returns immediately if
    all expected indexes are present. On a fresh DB just finalized by
    ``Recorder.stop()`` the first call builds indexes (~100-200 ms on a
    50K-frame run); subsequent calls are <1 ms.

    Returns True if a build happened, False if indexes were already present
    or the build failed gracefully. Never raises — index build failures are
    logged as warnings; queries still work without indexes (just slower).
    """
    # Expected index names from schema.SECONDARY_INDEX_CREATE
    expected = {
        'executionframes_run_id_filename_line_no',
        'executionframes_run_id_function_name',
        'executionframes_run_id_frame_event_sequence_no',
        'executionframes_run_id_call_depth_sequence_no',
        'executionframes_run_id_thread_id_sequence_no',
    }

    try:
        rows = db.fetchall("PRAGMA index_list(executionframes)")
        existing = {r.name for r in rows if hasattr(r, 'name')}
        if expected.issubset(existing):
            return False
    except Exception as e:
        logger.debug("ensure_secondary_indexes: index_list probe failed: %s", e)
        # Fall through — we'll attempt the creates anyway; CREATE INDEX IF
        # NOT EXISTS is safe.

    # Print a one-time notice per DB path so users understand the pause.
    # Use the db path as the key. _notified_lazy_build is session-scoped.
    if not quiet:
        import sys
        try:
            path = getattr(db, '_path', None) or '<unknown>'
            if path not in _notified_lazy_build:
                print("Building query indexes (one-time, ~100-200ms)...",
                      file=sys.stderr)
                _notified_lazy_build.add(path)
        except Exception:
            pass

    try:
        db.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass

    try:
        for sql in schema.SECONDARY_INDEX_CREATE:
            db.execute(sql)
        db.commit()
        return True
    except Exception as e:
        logger.warning(
            "Could not build secondary indexes (queries may be slow): %s", e)
        return False


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
