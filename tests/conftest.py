import pytest
from pyttd.models import storage
from pyttd.models.base import db
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs

@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary DB path for test isolation."""
    return str(tmp_path / "test.pyttd.db")

@pytest.fixture
def db_setup(db_path):
    """Connect to a temp DB, create tables, and close after test."""
    storage.connect_to_db(db_path)
    storage.initialize_schema([Runs, ExecutionFrames])
    yield db_path
    storage.close_db()
    db.init(None)
