import pytest
import pyttd_native


def test_import():
    assert pyttd_native is not None


def test_method_names():
    expected = [
        'start_recording', 'stop_recording', 'get_recording_stats',
        'set_ignore_patterns', 'request_stop', 'create_checkpoint',
        'restore_checkpoint', 'kill_all_checkpoints',
        'install_io_hooks', 'remove_io_hooks',
    ]
    members = dir(pyttd_native)
    for name in expected:
        assert name in members, f"{name} not found in pyttd_native"


# Phase 1 implemented: start_recording, stop_recording, get_recording_stats,
# set_ignore_patterns, request_stop — tested in test_recorder.py and test_ringbuf.py

# Still stubs (Phase 2+):

def test_create_checkpoint_raises():
    with pytest.raises(NotImplementedError):
        pyttd_native.create_checkpoint()


def test_restore_checkpoint_raises():
    with pytest.raises(NotImplementedError):
        pyttd_native.restore_checkpoint(0)


def test_kill_all_checkpoints_raises():
    with pytest.raises(NotImplementedError):
        pyttd_native.kill_all_checkpoints()


def test_install_io_hooks_raises():
    with pytest.raises(NotImplementedError):
        pyttd_native.install_io_hooks()


def test_remove_io_hooks_raises():
    with pytest.raises(NotImplementedError):
        pyttd_native.remove_io_hooks()
