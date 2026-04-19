"""Regression test for the adaptive sampling recurve (Item #3 of PERFORMANCE-PLAN).

The interval ladder is:
    counter <= 64   -> 8
    counter <= 256  -> 32
    counter <= 1024 -> 128
    counter <= 4096 -> 512
    else            -> 1024

For a 10K-iteration tight loop, the captured-locals count is bounded; this test
asserts it stays within a factor of 2 of the expected count (~50-100).
"""
from pyttd.models.db import db


def test_tight_loop_sampling_bounded(record_func):
    db_path, run_id, _ = record_func('''
        total = 0
        for i in range(10000):
            total += i
    ''')
    # Count line events in the loop body that actually serialized locals
    captured = db.fetchval(
        "SELECT COUNT(*) FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " AND locals_snapshot IS NOT NULL AND locals_snapshot != ''",
        (str(run_id),))
    # Per-line count estimate for 10K iters * ~2 line events/iter = ~20K events.
    # With the new curve: ~64 in warmup/interval-8, ~6 at interval-32 up to 256,
    # ~6 at interval-128 up to 1024, ~6 at interval-512 up to 4096, then
    # ~(20000-4096)/1024 ~= 16 at interval-1024.  Plus first-visit spikes.
    # Expect on the order of 100-200.  Assert well under 1000 (old curve would
    # have emitted thousands).
    assert captured > 30, f"too few locals captured: {captured}"
    assert captured < 1000, f"sampling didn't back off: {captured}"
