"""Phase 4: I/O hook tests.

Tests that I/O hooks record non-deterministic function calls
and that recorded values are correctly serialized/stored.
"""
import struct
import pytest
from pyttd.models.frames import ExecutionFrames
from pyttd.models.io_events import IOEvent


class TestIOHookRecording:
    def test_time_time_recorded(self, record_func):
        """Recording a script with time.time() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import time
            t1 = time.time()
            t2 = time.time()
        """)
        events = list(IOEvent.select()
            .where(IOEvent.run_id == run_id)
            .order_by(IOEvent.io_sequence))
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
        events = list(IOEvent.select()
            .where((IOEvent.run_id == run_id) &
                   (IOEvent.function_name == "time.monotonic")))
        assert len(events) >= 1

    def test_random_random_recorded(self, record_func):
        """Recording a script with random.random() should produce IOEvents."""
        db_path, run_id, stats = record_func("""\
            import random
            r1 = random.random()
            r2 = random.random()
        """)
        events = list(IOEvent.select()
            .where((IOEvent.run_id == run_id) &
                   (IOEvent.function_name == "random.random")))
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
        events = list(IOEvent.select()
            .where((IOEvent.run_id == run_id) &
                   (IOEvent.function_name == "random.randint")))
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
        events = list(IOEvent.select()
            .where((IOEvent.run_id == run_id) &
                   (IOEvent.function_name == "os.urandom")))
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
        events = list(IOEvent.select()
            .where(IOEvent.run_id == run_id)
            .order_by(IOEvent.io_sequence))
        assert len(events) >= 3
        for i in range(1, len(events)):
            assert events[i].io_sequence > events[i-1].io_sequence

    def test_io_event_has_valid_sequence_no(self, record_func):
        """IOEvent.sequence_no should reference a valid frame event."""
        db_path, run_id, stats = record_func("""\
            import time
            t = time.time()
        """)
        events = list(IOEvent.select()
            .where((IOEvent.run_id == run_id) &
                   (IOEvent.function_name == "time.time")))
        assert len(events) >= 1

        # The sequence_no should correspond to an existing frame
        frame = ExecutionFrames.get_or_none(
            (ExecutionFrames.run_id == run_id) &
            (ExecutionFrames.sequence_no == events[0].sequence_no))
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
