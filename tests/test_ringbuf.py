from pyttd.models.db import db


def test_frames_in_db_after_stop(record_func):
    db_path, run_id, stats = record_func("x = 1\ny = 2\nz = x + y\n")
    count = db.fetchval("SELECT COUNT(*) FROM executionframes WHERE run_id = ?", (str(run_id),))
    assert count > 0
    assert count == stats['frame_count']


def test_flush_dict_keys(record_func):
    db_path, run_id, stats = record_func("x = 1\ny = 2\nz = x + y\n")
    frame = db.fetchone("SELECT * FROM executionframes WHERE run_id = ? LIMIT 1", (str(run_id),))
    assert frame is not None
    assert frame.sequence_no is not None
    assert frame.timestamp is not None
    assert frame.line_no is not None
    assert frame.filename is not None
    assert frame.function_name is not None
    assert frame.frame_event is not None
    assert frame.call_depth is not None


def test_recording_stats(record_func):
    db_path, run_id, stats = record_func("x = 1\ny = 2\nz = x + y\n")
    assert 'frame_count' in stats
    assert 'dropped_frames' in stats
    assert 'elapsed_time' in stats
    assert 'flush_count' in stats
    assert 'pool_overflows' in stats
    assert stats['frame_count'] > 0
    assert stats['dropped_frames'] == 0
