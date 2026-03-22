import logging
import os
from typing import List, Type

from peewee import Model

from pyttd.models.base import db
from pyttd.models.constants import DB_NAME_SUFFIX, PRAGMAS

logger = logging.getLogger(__name__)


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
    if not db.is_closed():
        db.close()
    db.init(db_path, pragmas=PRAGMAS)
    db.connect(reuse_if_open=True)
    logger.info("Connected to database: %s", db_path)

def initialize_schema(models: List[Type[Model]]):
    """Create tables for the given models (safe=True for idempotency)."""
    db.create_tables(models, safe=True)

def delete_db_files(db_path: str):
    """Delete a SQLite database and its WAL/SHM companion files."""
    import os
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)

def batch_insert(model_class: Type[Model], rows: list[dict], batch_size: int = 500):
    """Batch-insert rows into the given model's table."""
    with db.atomic():
        for i in range(0, len(rows), batch_size):
            model_class.insert_many(rows[i:i + batch_size]).execute()

def close_db():
    """Close the database connection."""
    if not db.is_closed():
        db.close()


def evict_old_runs(db_path: str, keep: int, dry_run: bool = False) -> list:
    """Evict all but the last `keep` runs from the database.

    Returns list of evicted run_ids.
    """
    connect_to_db(db_path)
    from pyttd.models.runs import Runs
    from pyttd.models.frames import ExecutionFrames
    from pyttd.models.checkpoints import Checkpoint
    from pyttd.models.io_events import IOEvent
    initialize_schema([Runs, ExecutionFrames, Checkpoint, IOEvent])
    try:
        result = _evict_old_runs_internal(keep, dry_run)
        if not dry_run and result:
            db.execute_sql('VACUUM')
        return result
    finally:
        close_db()


def _evict_old_runs_internal(keep: int, dry_run: bool = False) -> list:
    """Evict old runs assuming DB is already connected. Returns list of evicted run_ids."""
    from pyttd.models.runs import Runs
    from pyttd.models.frames import ExecutionFrames
    from pyttd.models.checkpoints import Checkpoint
    from pyttd.models.io_events import IOEvent

    all_runs = list(Runs.select().order_by(Runs.timestamp_start.desc()))
    if len(all_runs) <= keep:
        return []

    to_evict = all_runs[keep:]
    evicted_ids = [r.run_id for r in to_evict]

    if dry_run:
        return evicted_ids

    with db.atomic():
        for run_id in evicted_ids:
            IOEvent.delete().where(IOEvent.run_id == run_id).execute()
            Checkpoint.delete().where(Checkpoint.run_id == run_id).execute()
            ExecutionFrames.delete().where(ExecutionFrames.run_id == run_id).execute()
            Runs.delete().where(Runs.run_id == run_id).execute()

    return evicted_ids
