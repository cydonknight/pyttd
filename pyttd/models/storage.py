import logging
from typing import List, Type

from peewee import Model

from pyttd.models.base import db
from pyttd.models.constants import PRAGMAS

logger = logging.getLogger(__name__)

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
