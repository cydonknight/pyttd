import functools
import os
import signal
import sys
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


class ArmContext:
    """Context manager for arm()/disarm() pattern."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if _active_recorder is not None and _active_recorder._recording:
            disarm()
        return False


def arm(db_path: str | None = None, **kwargs) -> ArmContext:
    """Install recording hooks on demand from within a running process.

    Synthesizes the existing call stack so navigation works correctly
    after recording starts mid-execution. Checkpoints are force-disabled.

    Args:
        db_path: Path to save the .pyttd.db file. Default: <caller_script>.pyttd.db
        **kwargs: Passed to PyttdConfig (max_frames, etc.)

    Returns:
        ArmContext that can be used as a context manager.
    """
    global _active_recorder
    if _active_recorder is not None and _active_recorder._recording:
        raise RuntimeError("Recording is already active. Call disarm() first.")

    if db_path is None:
        import inspect
        caller_frame = inspect.stack()[1]
        source_file = os.path.realpath(caller_frame.filename)
        db_path = compute_db_path(source_file)

    # Force-disable checkpoints in attach mode
    kwargs['checkpoint_interval'] = 0
    config = PyttdConfig(**kwargs)
    _active_recorder = Recorder(config)
    _active_recorder.start(db_path, attach=True)
    # Install trace function on the current thread so line events fire
    # in the caller's already-entered frame (the eval hook only fires on
    # new frame entries, missing the caller's in-progress frame).
    import pyttd_native
    pyttd_native.trace_current_frame()
    return ArmContext()


def disarm() -> dict:
    """Stop recording started by arm() and return stats dict.

    Returns:
        dict with frame_count, elapsed_time, dropped_frames, etc.
    """
    global _active_recorder
    if _active_recorder is None or not _active_recorder._recording:
        raise RuntimeError("No active recording. Call arm() first.")

    stats = _active_recorder.stop()
    _active_recorder.cleanup()
    _active_recorder = None
    return stats


def install_signal_handler(sig=None, db_path=None, **kwargs):
    """Install a signal handler that toggles recording on/off.

    Usage: kill -USR1 <pid>
    First signal starts recording, second stops it.

    Args:
        sig: Signal number (default: SIGUSR1 on Unix).
        db_path: Path to save the .pyttd.db file.
        **kwargs: Passed to PyttdConfig.
    """
    if sig is None:
        if not hasattr(signal, 'SIGUSR1'):
            raise RuntimeError("Signal-based arming requires Unix (SIGUSR1)")
        sig = signal.SIGUSR1

    def _handler(signum, frame):
        try:
            if _active_recorder is not None and _active_recorder._recording:
                stats = disarm()
                print(f"pyttd: Recording stopped ({stats.get('frame_count', 0)} frames)",
                      file=sys.stderr)
            else:
                arm(db_path=db_path, **kwargs)
                print("pyttd: Recording started", file=sys.stderr)
        except Exception as e:
            print(f"pyttd: Signal handler error: {e}", file=sys.stderr)

    signal.signal(sig, _handler)
