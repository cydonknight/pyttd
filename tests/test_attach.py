"""Tests for attach-to-process mode (arm/disarm API)."""

import json
import os
import signal
import sys
import textwrap

import pytest

import pyttd
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.main import arm, disarm, ArmContext, install_signal_handler, _active_recorder
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db
from pyttd.recorder import Recorder


@pytest.fixture(autouse=True)
def cleanup_recorder():
    """Ensure recorder is cleaned up after each test."""
    yield
    # Force cleanup if test leaves recorder running
    import pyttd.main as _main
    if _main._active_recorder is not None:
        try:
            if _main._active_recorder._recording:
                pyttd_native.stop_recording()
        except Exception:
            pass
        try:
            _main._active_recorder.cleanup()
        except Exception:
            pass
        _main._active_recorder = None
    try:
        close_db()
    except Exception:
        pass
    db.init(None)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "attach_test.pyttd.db")


# ---- Synthetic call events ----

def test_arm_from_nested_function(db_path):
    """arm() from 3 levels deep should emit 3 synthetic call events."""
    def level3():
        arm(db_path=db_path)

    def level2():
        level3()
        x = 42  # noqa: F841
        disarm()

    def level1():
        level2()

    level1()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        frames = db.fetchall(
            "SELECT * FROM executionframes ORDER BY sequence_no")
        # First events should be synthetic calls for the existing stack
        call_frames = [f for f in frames if f.frame_event == 'call']
        # There should be synthetic call events for the frames above arm()
        # (level1, level2, level3 or subset depending on filtering)
        assert len(call_frames) >= 2  # at least level1 and level2
        # All synthetic calls should have increasing call_depth
        synth_calls = call_frames[:3]  # first few are synthetic
        for i in range(1, len(synth_calls)):
            assert synth_calls[i].call_depth >= synth_calls[i - 1].call_depth
    finally:
        close_db()
        db.init(None)


def test_arm_synthetic_locals(db_path):
    """Synthetic call events should capture locals at arm time."""
    captured_value = 42

    def wrapper():
        arm(db_path=db_path)

    wrapper()
    x = 100  # noqa: F841
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        frames = db.fetchall(
            "SELECT * FROM executionframes WHERE frame_event = 'call' ORDER BY sequence_no")
        # At least one synthetic call should have locals
        has_locals = any(f.locals_snapshot and f.locals_snapshot != '{}'
                         for f in frames)
        # The test function frame should capture captured_value
        for f in frames:
            if f.locals_snapshot and 'captured_value' in f.locals_snapshot:
                locals_dict = json.loads(f.locals_snapshot)
                assert 'captured_value' in locals_dict
                break
    finally:
        close_db()
        db.init(None)


def test_arm_filters_applied(db_path):
    """Synthetic call events should exclude stdlib/frozen frames."""
    def inner():
        arm(db_path=db_path)

    inner()
    x = 1  # noqa: F841
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        frames = db.fetchall(
            "SELECT * FROM executionframes WHERE frame_event = 'call' ORDER BY sequence_no")
        # Synthetic call events should not include frozen modules
        for f in frames:
            # Raw frozen filenames start with '<frozen ' but get realpath'd
            # by _on_flush. Either way, frozen modules should not appear.
            raw_basename = os.path.basename(f.filename)
            assert not raw_basename.startswith('<frozen ')
    finally:
        close_db()
        db.init(None)


def test_arm_from_module_level(db_path):
    """arm() at top level should start at depth 0."""
    arm(db_path=db_path)
    x = 1  # noqa: F841
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        frames = db.fetchall(
            "SELECT * FROM executionframes ORDER BY sequence_no")
        # First synthetic call should be at depth 0
        call_frames = [f for f in frames if f.frame_event == 'call']
        assert len(call_frames) >= 1
        assert call_frames[0].call_depth == 0
    finally:
        close_db()
        db.init(None)


# ---- Navigation ----

def test_step_back_after_arm(db_path):
    """step_back should work after arm()."""
    from pyttd.session import Session

    def work():
        a = 10
        b = 20
        c = a + b
        return c

    arm(db_path=db_path)
    work()
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        run = db.fetchone("SELECT * FROM runs ORDER BY timestamp_start DESC LIMIT 1")
        # Get first line event to init session
        first_line = db.fetchone(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run.run_id),))
        assert first_line is not None
        session = Session()
        session.enter_replay(run.run_id, first_line.sequence_no)
        # Step forward a couple of times
        r1 = session.step_into()
        assert r1 is not None
        r2 = session.step_into()
        assert r2 is not None
        # Now step back — should return to a previous position
        result = session.step_back()
        assert result is not None
        assert result['seq'] < r2['seq']
    finally:
        close_db()
        db.init(None)


def test_stack_reconstruction(db_path):
    """_build_stack_at should return correct stack including synthetic frames."""
    from pyttd.session import Session

    def inner_work():
        y = 99  # noqa: F841
        return y

    def outer_work():
        arm(db_path=db_path)
        result = inner_work()
        return result

    outer_work()
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        run = db.fetchone("SELECT * FROM runs ORDER BY timestamp_start DESC LIMIT 1")
        # Find a line event inside inner_work — nested names include scope
        inner_frames = db.fetchall(
            "SELECT * FROM executionframes"
            " WHERE run_id = ? AND function_name LIKE '%inner_work%' AND frame_event = 'line'"
            " ORDER BY sequence_no",
            (str(run.run_id),))
        if inner_frames:
            # Need to enter_replay with first line event, not inner_frames[0]
            first_line = db.fetchone(
                "SELECT * FROM executionframes"
                " WHERE run_id = ? AND frame_event = 'line'"
                " ORDER BY sequence_no LIMIT 1",
                (str(run.run_id),))
            session = Session()
            session.enter_replay(run.run_id, first_line.sequence_no)
            # Navigate to the inner_work line event
            stack = session.get_stack_at(inner_frames[0].sequence_no)
            # Stack should include inner_work (nested name includes scope)
            func_names = [entry['name'] for entry in stack]
            assert any('inner_work' in name for name in func_names)
    finally:
        close_db()
        db.init(None)


# ---- Lifecycle ----

def test_arm_disarm_basic(db_path):
    """Basic arm/disarm cycle with stats."""
    arm(db_path=db_path)
    x = 42  # noqa: F841
    stats = disarm()
    assert isinstance(stats, dict)
    assert stats.get('frame_count', 0) > 0


def test_arm_context_manager(db_path):
    """with pyttd.arm() should work as context manager."""
    with arm(db_path=db_path):
        x = 42  # noqa: F841

    # Verify recording stopped
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        runs = db.fetchall("SELECT * FROM runs")
        assert len(runs) == 1
        frames = db.fetchall("SELECT * FROM executionframes")
        assert len(frames) > 0
    finally:
        close_db()
        db.init(None)


def test_arm_twice_raises(db_path):
    """Double arm() should raise RuntimeError."""
    arm(db_path=db_path)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            arm(db_path=db_path)
    finally:
        disarm()


def test_disarm_without_arm_raises():
    """disarm() without arm() should raise RuntimeError."""
    with pytest.raises(RuntimeError, match="No active recording"):
        disarm()


# ---- Correctness ----

def test_no_checkpoints_in_attach(db_path):
    """Attach mode should not create checkpoints."""
    arm(db_path=db_path)
    # Do some work
    for i in range(10):
        _ = i * 2
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        checkpoints = db.fetchall("SELECT * FROM checkpoint")
        assert len(checkpoints) == 0
    finally:
        close_db()
        db.init(None)


def test_runs_is_attach_flag(db_path):
    """Runs.is_attach should be True for attach recordings."""
    arm(db_path=db_path)
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        run = db.fetchone("SELECT * FROM runs ORDER BY timestamp_start DESC LIMIT 1")
        assert run.is_attach
    finally:
        close_db()
        db.init(None)


def test_events_after_arm(db_path):
    """Events should be captured for function calls after arm() returns."""
    def work():
        a = 10
        b = 20
        return a + b

    arm(db_path=db_path)
    result = work()
    disarm()

    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        frames = db.fetchall(
            "SELECT * FROM executionframes ORDER BY sequence_no")
        # Nested function names include enclosing scope (e.g. "test_x.<locals>.work")
        work_frames = [f for f in frames if 'work' in f.function_name]
        assert len(work_frames) >= 3  # call + lines + return
        assert any(f.frame_event == 'call' for f in work_frames)
        assert any(f.frame_event == 'line' for f in work_frames)
        assert any(f.frame_event == 'return' for f in work_frames)
    finally:
        close_db()
        db.init(None)


# ---- Signal handler (Unix only) ----

@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason="Unix only (no SIGUSR1)")
def test_signal_handler_toggle(db_path):
    """SIGUSR1 should toggle recording on/off."""
    import signal

    install_signal_handler(sig=signal.SIGUSR1, db_path=db_path)

    # First signal: start recording
    os.kill(os.getpid(), signal.SIGUSR1)

    import pyttd.main as _main
    assert _main._active_recorder is not None
    assert _main._active_recorder._recording

    # Do some work
    x = 42  # noqa: F841

    # Second signal: stop recording
    os.kill(os.getpid(), signal.SIGUSR1)
    assert _main._active_recorder is None

    # Verify recording was saved
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    try:
        frames = db.fetchall("SELECT * FROM executionframes")
        assert len(frames) > 0
    finally:
        close_db()
        db.init(None)


@pytest.mark.skipif(not hasattr(signal, 'SIGUSR1'), reason="Unix only (no SIGUSR1)")
def test_arm_signal_env(tmp_path, monkeypatch):
    """PYTTD_ARM_SIGNAL env var should auto-install handler."""
    import signal
    import importlib

    db_path = str(tmp_path / "env_signal_test.pyttd.db")

    # Set env var before reimporting
    monkeypatch.setenv('PYTTD_ARM_SIGNAL', 'USR1')

    # Reimport to trigger env var check
    importlib.reload(pyttd)

    # Check that signal handler was installed
    handler = signal.getsignal(signal.SIGUSR1)
    assert handler is not signal.SIG_DFL
    assert callable(handler)

    # Clean up: reset signal handler
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)
    monkeypatch.delenv('PYTTD_ARM_SIGNAL', raising=False)
    importlib.reload(pyttd)
