import functools
import os
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.constants import DB_NAME_SUFFIX
from pyttd.models.storage import delete_db_files

def ttdbg(func):
    """Decorator that records function execution with the C extension."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        import inspect
        source_file = inspect.getfile(func)
        script_name = os.path.splitext(os.path.basename(source_file))[0]
        db_path = os.path.join(os.path.dirname(source_file) or '.', script_name + DB_NAME_SUFFIX)
        delete_db_files(db_path)
        config = PyttdConfig()
        recorder = Recorder(config)
        recorder.start(db_path, script_path=source_file)
        try:
            return func(*args, **kwargs)
        finally:
            recorder.stop()
            recorder.cleanup()
    return wrapper
