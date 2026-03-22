import functools
import os
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.storage import compute_db_path


def ttdbg(func):
    """Decorator that records function execution with the C extension."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        import inspect
        source_file = os.path.realpath(inspect.getfile(func))
        db_path = compute_db_path(source_file)
        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=source_file)
        try:
            return func(*args, **kwargs)
        finally:
            recorder.stop()
            recorder.cleanup()
    return wrapper


# Module-level state for the public API
_active_recorder: Recorder | None = None


def start_recording(db_path: str | None = None, **kwargs):
    """Start recording execution. Call stop_recording() to stop.

    Args:
        db_path: Path to save the .pyttd.db file. Default: <caller_script>.pyttd.db
        **kwargs: Passed to PyttdConfig (checkpoint_interval, max_frames, etc.)
    """
    global _active_recorder
    if _active_recorder is not None and _active_recorder._recording:
        raise RuntimeError("Recording is already active. Call stop_recording() first.")

    if db_path is None:
        import inspect
        caller_frame = inspect.stack()[1]
        source_file = os.path.realpath(caller_frame.filename)
        db_path = compute_db_path(source_file)

    config = PyttdConfig(**kwargs)
    _active_recorder = Recorder(config)
    _active_recorder.start(db_path)


def stop_recording() -> dict:
    """Stop the active recording and return stats dict.

    Returns:
        dict with frame_count, elapsed_time, dropped_frames, etc.
    """
    global _active_recorder
    if _active_recorder is None or not _active_recorder._recording:
        raise RuntimeError("No active recording. Call start_recording() first.")

    stats = _active_recorder.stop()
    _active_recorder.cleanup()
    _active_recorder = None
    return stats
