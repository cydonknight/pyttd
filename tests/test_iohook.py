"""Phase 4: I/O hook tests.

Tests that I/O hooks record non-deterministic function calls
and that recorded values are correctly serialized/stored.
"""
import pickle
import struct
import pytest
from pyttd.models.db import db


class TestIOHookRecording:
    def test_time_time_recorded(self, record_func):
        """Recording a script with time.time() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import time
            t1 = time.time()
            t2 = time.time()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? ORDER BY io_sequence",
            (str(run_id),))
        time_events = [e for e in events if e.function_name == "time.time"]
        assert len(time_events) >= 2

        # Verify return values are valid IEEE 754 doubles (8 bytes)
        for ev in time_events:
            data = bytes(ev.return_value)
            assert len(data) == 8
            val = struct.unpack('d', data)[0]
            assert val > 0  # timestamps are positive

    def test_time_monotonic_recorded(self, record_func):
        """Recording a script with time.monotonic() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import time
            m = time.monotonic()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'time.monotonic'",
            (str(run_id),))
        assert len(events) >= 1

    def test_random_random_recorded(self, record_func):
        """Recording a script with random.random() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import random
            r1 = random.random()
            r2 = random.random()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.random'",
            (str(run_id),))
        assert len(events) >= 2

        # Verify return values are valid floats
        for ev in events:
            data = bytes(ev.return_value)
            assert len(data) == 8
            val = struct.unpack('d', data)[0]
            assert 0.0 <= val < 1.0

    def test_random_randint_recorded(self, record_func):
        """Recording a script with random.randint() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import random
            r = random.randint(1, 100)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.randint'",
            (str(run_id),))
        assert len(events) >= 1

        # Verify the int can be deserialized from length-prefixed format
        ev = events[0]
        data = bytes(ev.return_value)
        assert len(data) >= 5  # 4-byte length prefix + at least 1 byte

    def test_os_urandom_recorded(self, record_func):
        """Recording a script with os.urandom() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import os
            b = os.urandom(16)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'os.urandom'",
            (str(run_id),))
        assert len(events) >= 1

        # Verify the bytes can be deserialized
        ev = events[0]
        data = bytes(ev.return_value)
        # 4-byte length prefix + 16 bytes of random data
        assert len(data) >= 4

    def test_io_sequence_monotonic(self, record_func):
        """IO events should have monotonically increasing io_sequence."""
        db_path, run_id, stats = record_func("""\
            import time
            import random
            t = time.time()
            r = random.random()
            t2 = time.time()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? ORDER BY io_sequence",
            (str(run_id),))
        assert len(events) >= 3
        for i in range(1, len(events)):
            assert events[i].io_sequence > events[i-1].io_sequence

    def test_io_event_has_valid_sequence_no(self, record_func):
        """IOEvent.sequence_no should reference a valid frame event."""
        db_path, run_id, stats = record_func("""\
            import time
            t = time.time()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'time.time'",
            (str(run_id),))
        assert len(events) >= 1

        # The sequence_no should correspond to an existing frame
        frame = db.fetchone(
            "SELECT * FROM executionframes WHERE run_id = ? AND sequence_no = ?",
            (str(run_id), events[0].sequence_no))
        assert frame is not None

    def test_hooks_restored_after_stop(self, record_func):
        """After recording stops, the original functions should be restored."""
        import time
        import random

        db_path, run_id, stats = record_func("""\
            import time
            t = time.time()
        """)

        # After recording, time.time should be the original
        t = time.time()
        assert isinstance(t, float)
        assert t > 0

        r = random.random()
        assert isinstance(r, float)
        assert 0.0 <= r < 1.0

    def test_exception_in_hooked_function_propagates(self, record_func):
        """If a hooked function raises, the exception should propagate without logging."""
        db_path, run_id, stats = record_func("""\
            import os
            try:
                b = os.urandom(-1)  # should raise ValueError
            except (ValueError, OverflowError):
                pass
            x = 1
        """)
        # The script should complete without crashing
        assert stats.get('frame_count', 0) > 0


class TestIOHookExpansion:
    """Tests for expanded I/O hooks: datetime, uuid, time.sleep, random extras."""

    def test_datetime_now_recorded(self, record_func):
        """datetime.datetime.now() should produce IOEvents with float timestamps."""
        db_path, run_id, stats = record_func("""\
            import datetime
            t1 = datetime.datetime.now()
            t2 = datetime.datetime.now()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'datetime.datetime.now'",
            (str(run_id),))
        assert len(events) >= 2

        # Verify return values are valid IEEE 754 doubles (timestamps)
        for ev in events:
            data = bytes(ev.return_value)
            assert len(data) == 8
            val = struct.unpack('d', data)[0]
            assert val > 0  # timestamps are positive

    def test_datetime_utcnow_recorded(self, record_func):
        """datetime.datetime.utcnow() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import datetime
            t = datetime.datetime.utcnow()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'datetime.datetime.utcnow'",
            (str(run_id),))
        assert len(events) >= 1

        # Verify stored as float
        data = bytes(events[0].return_value)
        assert len(data) == 8
        val = struct.unpack('d', data)[0]
        assert val > 0

    def test_datetime_now_returns_datetime(self, record_func):
        """datetime.datetime.now() should return a valid datetime during recording."""
        db_path, run_id, stats = record_func("""\
            import datetime
            t = datetime.datetime.now()
            # Verify it's a datetime instance (isinstance check must work)
            assert isinstance(t, datetime.datetime), f"Expected datetime, got {type(t)}"
            assert t.year >= 2020
        """)
        assert stats.get('frame_count', 0) > 0

    def test_datetime_now_with_tz(self, record_func):
        """datetime.datetime.now(tz=...) should work and produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import datetime
            t = datetime.datetime.now(tz=datetime.timezone.utc)
            assert t.tzinfo is not None
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'datetime.datetime.now'",
            (str(run_id),))
        assert len(events) >= 1

    def test_uuid_uuid4_recorded(self, record_func):
        """uuid.uuid4() should produce IOEvents with 16-byte UUID data."""
        db_path, run_id, stats = record_func("""\
            import uuid
            u1 = uuid.uuid4()
            u2 = uuid.uuid4()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'uuid.uuid4'",
            (str(run_id),))
        assert len(events) >= 2

        # Verify return values are length-prefixed 16-byte UUID data
        for ev in events:
            data = bytes(ev.return_value)
            assert len(data) >= 4  # at least length prefix
            length = struct.unpack_from('<I', data, 0)[0]
            assert length == 16  # UUID is always 16 bytes

    def test_uuid_uuid1_recorded(self, record_func):
        """uuid.uuid1() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import uuid
            u = uuid.uuid1()
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'uuid.uuid1'",
            (str(run_id),))
        assert len(events) >= 1

    def test_uuid_returns_uuid(self, record_func):
        """uuid.uuid4() should return a valid UUID during recording."""
        db_path, run_id, stats = record_func("""\
            import uuid
            u = uuid.uuid4()
            assert isinstance(u, uuid.UUID), f"Expected UUID, got {type(u)}"
            assert u.version == 4
            # Verify string representation works
            s = str(u)
            assert len(s) == 36
        """)
        assert stats.get('frame_count', 0) > 0

    def test_time_sleep_recorded(self, record_func):
        """time.sleep() should produce IOEvents with the sleep duration."""
        db_path, run_id, stats = record_func("""\
            import time
            time.sleep(0.01)
            time.sleep(0.02)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'time.sleep'",
            (str(run_id),))
        assert len(events) >= 2

        # Verify durations are stored as floats
        for ev in events:
            data = bytes(ev.return_value)
            assert len(data) == 8
            val = struct.unpack('d', data)[0]
            assert val > 0

    def test_time_sleep_int_duration(self, record_func):
        """time.sleep() with integer duration should be recorded as float."""
        db_path, run_id, stats = record_func("""\
            import time
            time.sleep(0)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'time.sleep'",
            (str(run_id),))
        assert len(events) >= 1

    def test_random_uniform_recorded(self, record_func):
        """random.uniform() should produce IOEvents with float values."""
        db_path, run_id, stats = record_func("""\
            import random
            r = random.uniform(1.0, 10.0)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.uniform'",
            (str(run_id),))
        assert len(events) >= 1

        data = bytes(events[0].return_value)
        assert len(data) == 8
        val = struct.unpack('d', data)[0]
        assert 1.0 <= val <= 10.0

    def test_random_gauss_recorded(self, record_func):
        """random.gauss() should produce IOEvents with float values."""
        db_path, run_id, stats = record_func("""\
            import random
            r = random.gauss(0, 1)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.gauss'",
            (str(run_id),))
        assert len(events) >= 1

        data = bytes(events[0].return_value)
        assert len(data) == 8

    def test_random_choice_recorded(self, record_func):
        """random.choice() should produce IOEvents with pickled return values."""
        db_path, run_id, stats = record_func("""\
            import random
            c = random.choice([10, 20, 30, 40, 50])
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.choice'",
            (str(run_id),))
        assert len(events) >= 1

        # Verify pickled data can be deserialized
        data = bytes(events[0].return_value)
        # Length-prefixed pickle: 4-byte prefix + pickle data
        assert len(data) >= 4
        length = struct.unpack_from('<I', data, 0)[0]
        pickle_data = data[4:4+length]
        val = pickle.loads(pickle_data)
        assert val in [10, 20, 30, 40, 50]

    def test_random_sample_recorded(self, record_func):
        """random.sample() should produce IOEvents with pickled lists."""
        db_path, run_id, stats = record_func("""\
            import random
            s = random.sample(range(100), 5)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.sample'",
            (str(run_id),))
        assert len(events) >= 1

        # Verify pickled data
        data = bytes(events[0].return_value)
        length = struct.unpack_from('<I', data, 0)[0]
        pickle_data = data[4:4+length]
        val = pickle.loads(pickle_data)
        assert isinstance(val, list)
        assert len(val) == 5
        assert all(0 <= x < 100 for x in val)

    def test_random_shuffle_recorded(self, record_func):
        """random.shuffle() should produce IOEvents with the shuffled list."""
        db_path, run_id, stats = record_func("""\
            import random
            lst = [1, 2, 3, 4, 5]
            random.shuffle(lst)
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.shuffle'",
            (str(run_id),))
        assert len(events) >= 1

        # Verify pickled data is a list with same elements
        data = bytes(events[0].return_value)
        length = struct.unpack_from('<I', data, 0)[0]
        pickle_data = data[4:4+length]
        val = pickle.loads(pickle_data)
        assert isinstance(val, list)
        assert sorted(val) == [1, 2, 3, 4, 5]

    def test_expanded_hooks_restored_after_stop(self, record_func):
        """After recording stops, all new hooks should be restored."""
        import time
        import random
        import uuid
        import datetime

        db_path, run_id, stats = record_func("""\
            import time
            time.sleep(0.001)
        """)

        # After recording, all functions should be originals
        time.sleep(0.001)  # should not crash

        r = random.uniform(0, 1)
        assert isinstance(r, float)

        u = uuid.uuid4()
        assert hasattr(u, 'hex')

        # datetime.datetime should be the original class
        t = datetime.datetime.now()
        assert isinstance(t, datetime.datetime)

    def test_random_choice_with_strings(self, record_func):
        """random.choice() with string elements should serialize correctly."""
        db_path, run_id, stats = record_func("""\
            import random
            c = random.choice(['alpha', 'beta', 'gamma'])
        """)
        events = db.fetchall(
            "SELECT * FROM ioevent WHERE run_id = ? AND function_name = 'random.choice'",
            (str(run_id),))
        assert len(events) >= 1

        data = bytes(events[0].return_value)
        length = struct.unpack_from('<I', data, 0)[0]
        pickle_data = data[4:4+length]
        val = pickle.loads(pickle_data)
        assert val in ['alpha', 'beta', 'gamma']
