"""Tests for timeline summary queries."""
import uuid
import pytest
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.timeline import get_timeline_summary


@pytest.fixture
def db_setup(tmp_path):
    db_path = str(tmp_path / "test.pyttd.db")
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    yield db_path
    storage.close_db()
    db.init(None)


@pytest.fixture
def run_with_frames(db_setup):
    """Create a run with known frame data for testing."""
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO runs (run_id, script_path, timestamp_start, timestamp_end, total_frames)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, "test.py", 1000.0, 1001.0, 0),
    )
    db.commit()
    return run_id


def _insert_frames(run_id, frames):
    """Insert frames as dicts with keys: seq, line, file, func, event, depth."""
    rid = str(run_id)
    sql = (
        "INSERT INTO executionframes"
        " (run_id, sequence_no, timestamp, line_no, filename,"
        "  function_name, frame_event, call_depth, locals_snapshot,"
        "  thread_id, is_coroutine)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = []
    for f in frames:
        params.append((
            rid,
            f['seq'],
            f['seq'] * 0.001,
            f.get('line', 1),
            f.get('file', 'test.py'),
            f.get('func', 'main'),
            f.get('event', 'line'),
            f.get('depth', 0),
            '{}',
            0,
            0,
        ))
    db.executemany(sql, params)
    db.commit()


class TestGetTimelineSummary:

    def test_empty_range(self, run_with_frames):
        """end_seq <= start_seq returns empty list."""
        result = get_timeline_summary(run_with_frames, 10, 5)
        assert result == []

    def test_zero_bucket_count(self, run_with_frames):
        """bucket_count=0 returns empty list."""
        result = get_timeline_summary(run_with_frames, 0, 100, bucket_count=0)
        assert result == []

    def test_single_bucket(self, run_with_frames):
        """All frames in one bucket when bucket_count=1."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'call', 'depth': 0, 'func': 'main'},
            {'seq': 1, 'event': 'line', 'depth': 0, 'func': 'main'},
            {'seq': 2, 'event': 'line', 'depth': 0, 'func': 'main'},
            {'seq': 3, 'event': 'return', 'depth': 0, 'func': 'main'},
        ])
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=1)
        assert len(result) == 1
        assert result[0]['startSeq'] == 0
        assert result[0]['endSeq'] == 3
        assert result[0]['maxCallDepth'] == 0
        assert result[0]['hasException'] is False

    def test_multiple_buckets(self, run_with_frames):
        """Frames split across multiple buckets."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'depth': 0, 'func': 'main'},
            {'seq': 1, 'event': 'line', 'depth': 0, 'func': 'main'},
            {'seq': 2, 'event': 'line', 'depth': 1, 'func': 'helper'},
            {'seq': 3, 'event': 'line', 'depth': 1, 'func': 'helper'},
        ])
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=2)
        assert len(result) == 2
        assert result[0]['startSeq'] == 0
        assert result[0]['endSeq'] == 1
        assert result[1]['startSeq'] == 2
        assert result[1]['endSeq'] == 3

    def test_exception_detection(self, run_with_frames):
        """Buckets with exception events have hasException=True."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'depth': 0},
            {'seq': 1, 'event': 'exception', 'depth': 0},
            {'seq': 2, 'event': 'line', 'depth': 0},
            {'seq': 3, 'event': 'line', 'depth': 0},
        ])
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=2)
        assert len(result) == 2
        assert result[0]['hasException'] is True
        assert result[1]['hasException'] is False

    def test_exception_unwind_detection(self, run_with_frames):
        """exception_unwind events also set hasException."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'depth': 0},
            {'seq': 1, 'event': 'exception_unwind', 'depth': 0},
        ])
        result = get_timeline_summary(run_with_frames, 0, 2, bucket_count=1)
        assert result[0]['hasException'] is True

    def test_max_call_depth(self, run_with_frames):
        """maxCallDepth reflects the deepest frame in each bucket."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'depth': 0},
            {'seq': 1, 'event': 'line', 'depth': 3},
            {'seq': 2, 'event': 'line', 'depth': 1},
            {'seq': 3, 'event': 'line', 'depth': 5},
        ])
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=2)
        assert result[0]['maxCallDepth'] == 3
        assert result[1]['maxCallDepth'] == 5

    def test_dominant_function(self, run_with_frames):
        """dominantFunction is a representative function name from the bucket."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'func': 'alpha'},
            {'seq': 1, 'event': 'line', 'func': 'beta'},
            {'seq': 2, 'event': 'line', 'func': 'gamma'},
            {'seq': 3, 'event': 'line', 'func': 'delta'},
        ])
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=2)
        assert len(result) == 2
        # dominantFunction should be a non-empty string from the bucket
        assert result[0]['dominantFunction'] != ''
        assert result[1]['dominantFunction'] != ''

    def test_breakpoint_matching(self, run_with_frames):
        """hasBreakpoint is True when a line event matches a breakpoint."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'file': 'test.py', 'line': 10},
            {'seq': 1, 'event': 'line', 'file': 'test.py', 'line': 11},
            {'seq': 2, 'event': 'line', 'file': 'test.py', 'line': 20},
            {'seq': 3, 'event': 'line', 'file': 'test.py', 'line': 21},
        ])
        breakpoints = [{'file': 'test.py', 'line': 10}]
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=2,
                                      breakpoints=breakpoints)
        assert result[0]['hasBreakpoint'] is True
        assert result[1]['hasBreakpoint'] is False

    def test_breakpoint_no_match(self, run_with_frames):
        """hasBreakpoint is False when no line events match breakpoints."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'file': 'test.py', 'line': 10},
            {'seq': 1, 'event': 'line', 'file': 'test.py', 'line': 11},
        ])
        breakpoints = [{'file': 'other.py', 'line': 10}]
        result = get_timeline_summary(run_with_frames, 0, 2, bucket_count=1,
                                      breakpoints=breakpoints)
        assert result[0]['hasBreakpoint'] is False

    def test_no_breakpoints_param(self, run_with_frames):
        """No breakpoints passed means hasBreakpoint is always False."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'file': 'test.py', 'line': 10},
        ])
        result = get_timeline_summary(run_with_frames, 0, 1, bucket_count=1)
        assert result[0]['hasBreakpoint'] is False

    def test_zoom_subrange(self, run_with_frames):
        """Sub-range queries return buckets only for the queried range."""
        _insert_frames(run_with_frames, [
            {'seq': i, 'event': 'line', 'depth': i % 3}
            for i in range(100)
        ])
        # Query only the middle range
        result = get_timeline_summary(run_with_frames, 25, 75, bucket_count=10)
        assert len(result) > 0
        for bucket in result:
            assert bucket['startSeq'] >= 25
            assert bucket['endSeq'] <= 75

    def test_no_frames_in_range(self, run_with_frames):
        """Range with no frames returns empty list."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line'},
        ])
        result = get_timeline_summary(run_with_frames, 100, 200, bucket_count=10)
        assert result == []

    def test_multiple_breakpoints(self, run_with_frames):
        """Multiple breakpoints can match different buckets."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'file': 'a.py', 'line': 5},
            {'seq': 1, 'event': 'line', 'file': 'a.py', 'line': 6},
            {'seq': 2, 'event': 'line', 'file': 'b.py', 'line': 10},
            {'seq': 3, 'event': 'line', 'file': 'b.py', 'line': 11},
        ])
        breakpoints = [
            {'file': 'a.py', 'line': 5},
            {'file': 'b.py', 'line': 10},
        ]
        result = get_timeline_summary(run_with_frames, 0, 4, bucket_count=2,
                                      breakpoints=breakpoints)
        assert result[0]['hasBreakpoint'] is True
        assert result[1]['hasBreakpoint'] is True

    def test_single_event_bucket(self, run_with_frames):
        """Bucket with exactly one event works correctly."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'line', 'depth': 2, 'func': 'solo'},
        ])
        result = get_timeline_summary(run_with_frames, 0, 1, bucket_count=1)
        assert len(result) == 1
        assert result[0]['startSeq'] == 0
        assert result[0]['endSeq'] == 0
        assert result[0]['maxCallDepth'] == 2
        assert result[0]['dominantFunction'] == 'solo'

    def test_breakpoint_only_matches_line_events(self, run_with_frames):
        """Breakpoints only match 'line' events, not call/return/exception."""
        _insert_frames(run_with_frames, [
            {'seq': 0, 'event': 'call', 'file': 'test.py', 'line': 10},
            {'seq': 1, 'event': 'return', 'file': 'test.py', 'line': 10},
        ])
        breakpoints = [{'file': 'test.py', 'line': 10}]
        result = get_timeline_summary(run_with_frames, 0, 2, bucket_count=1,
                                      breakpoints=breakpoints)
        assert result[0]['hasBreakpoint'] is False
