import pytest
import pyttd_native


def test_import():
    assert pyttd_native is not None


def test_method_names():
    expected = [
        'start_recording', 'stop_recording', 'get_recording_stats',
        'set_ignore_patterns', 'request_stop', 'set_recording_thread',
        'create_checkpoint', 'restore_checkpoint', 'kill_all_checkpoints',
        'get_checkpoint_count',
    ]
    members = dir(pyttd_native)
    for name in expected:
        assert name in members, f"{name} not found in pyttd_native"

    # Phase 4: install_io_hooks/remove_io_hooks are now internal C functions
    assert 'install_io_hooks' not in members
    assert 'remove_io_hooks' not in members


# Phase 2 basic smoke tests (not stubs anymore):

def test_kill_all_checkpoints_no_error():
    """kill_all_checkpoints should work even with no active checkpoints."""
    pyttd_native.kill_all_checkpoints()


def test_get_checkpoint_count():
    """get_checkpoint_count should return 0 when no checkpoints exist."""
    count = pyttd_native.get_checkpoint_count()
    assert count == 0
