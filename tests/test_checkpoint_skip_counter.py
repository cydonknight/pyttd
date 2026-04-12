"""Test for Issue 5 Phase 1 diagnostics: checkpoint skip counter.

When the checkpoint trigger fires while multiple recording threads are
active, the fork is unsafe and is skipped. The C recorder bumps a global
counter and surfaces it in ``get_recording_stats()`` so the CLI / UI can
warn that cold navigation will be limited for this run.
"""
import sys

import pytest

from pyttd.cli import _format_stats


def test_format_stats_includes_skip_warning():
    """The CLI summary should print a warning when the skip counter is non-zero."""
    stats = {
        'frame_count': 1000,
        'dropped_frames': 0,
        'elapsed_time': 1.0,
        'pool_overflows': 0,
        'checkpoint_count': 0,
        'checkpoint_memory_bytes': 0,
        'checkpoints_skipped_threads': 7,
    }
    msg = _format_stats(stats)
    assert "cold navigation is limited" in msg
    assert "7 checkpoint" in msg
    assert "troubleshooting" in msg


def test_format_stats_no_warning_when_zero():
    stats = {
        'frame_count': 1000,
        'dropped_frames': 0,
        'elapsed_time': 1.0,
        'pool_overflows': 0,
        'checkpoint_count': 0,
        'checkpoint_memory_bytes': 0,
        'checkpoints_skipped_threads': 0,
    }
    msg = _format_stats(stats)
    assert "cold navigation is limited" not in msg


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="checkpoints require fork() — Windows skips trigger entirely")
def test_skip_counter_bumps_on_multithread(record_func):
    """A multi-thread workload with a non-zero checkpoint interval should
    record at least one skipped checkpoint and surface it in stats."""
    db_path, run_id, stats = record_func("""\
        import threading

        def worker():
            for i in range(2000):
                x = i * 2
                y = x + 1

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for i in range(2000):
            z = i * 3
        for t in threads:
            t.join()
    """, checkpoint_interval=200)
    skipped = stats.get('checkpoints_skipped_threads', 0)
    assert skipped > 0, (
        f"expected non-zero checkpoints_skipped_threads, "
        f"got stats={stats}"
    )


def test_stats_field_present_even_when_unused(record_func):
    """The field should always be present (defaults to 0)."""
    db_path, run_id, stats = record_func("""\
        x = 1
    """)
    assert 'checkpoints_skipped_threads' in stats
    assert stats['checkpoints_skipped_threads'] == 0
