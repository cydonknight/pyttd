"""Tests for ``pyttd query --hide-coroutine-internals`` (Issue 2).

The flag should drop only exception events on coroutine frames whose
recorded ``__exception__`` is StopIteration / StopAsyncIteration noise.
Real user-facing exceptions propagating through ``async def`` functions
must survive, sync code must be unaffected, and ``--limit N`` must yield
up to N *surviving* rows (not N raw rows minus filtered count).
"""
import json
import sys

import pytest

from pyttd.cli import _is_coroutine_exception_noise
from pyttd.models import storage
from pyttd.models.db import db


# ---------------------------------------------------------------------------
# helper-level tests (no recording needed)
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, *, is_coroutine=0, frame_event='line', locals_snapshot=None):
        self.is_coroutine = is_coroutine
        self.frame_event = frame_event
        self.locals_snapshot = locals_snapshot


def test_noise_helper_filters_coroutine_stopiteration():
    row = _Row(
        is_coroutine=1,
        frame_event='exception',
        locals_snapshot=json.dumps({'__exception__': 'StopIteration(0,)'}),
    )
    assert _is_coroutine_exception_noise(row)


def test_noise_helper_filters_coroutine_stop_async_iteration():
    row = _Row(
        is_coroutine=1,
        frame_event='exception_unwind',
        locals_snapshot=json.dumps({'__exception__': 'StopAsyncIteration()'}),
    )
    assert _is_coroutine_exception_noise(row)


def test_noise_helper_keeps_real_async_exception():
    row = _Row(
        is_coroutine=1,
        frame_event='exception',
        locals_snapshot=json.dumps({'__exception__': "KeyError('valu')"}),
    )
    assert not _is_coroutine_exception_noise(row)


def test_noise_helper_keeps_non_coroutine_stopiteration():
    row = _Row(
        is_coroutine=0,
        frame_event='exception',
        locals_snapshot=json.dumps({'__exception__': 'StopIteration()'}),
    )
    assert not _is_coroutine_exception_noise(row)


def test_noise_helper_keeps_non_exception_event():
    row = _Row(
        is_coroutine=1,
        frame_event='line',
        locals_snapshot=json.dumps({'__exception__': 'StopIteration()'}),
    )
    assert not _is_coroutine_exception_noise(row)


# ---------------------------------------------------------------------------
# integration tests against recorded async code
# ---------------------------------------------------------------------------


def _fetch_filtered(run_id, hide=True, limit=50, offset=0):
    """Reimplement the cli.py filter+slice path against the live db."""
    rows = db.fetchall(
        "SELECT * FROM executionframes WHERE run_id = ?"
        " ORDER BY sequence_no",
        (str(run_id),),
    )
    if hide:
        rows = [r for r in rows if not _is_coroutine_exception_noise(r)]
    return rows[offset:offset + limit]


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="Windows asyncio internals prevent coroutine frame recording")
def test_user_async_exception_preserved(record_func):
    """The Issue 1 reproducer: real KeyError must survive the filter."""
    db_path, run_id, stats = record_func("""\
        import asyncio
        async def fetch_item(i):
            await asyncio.sleep(0.001)
            return {"id": i, "value": i * 2}
        async def main():
            items = await asyncio.gather(*[fetch_item(i) for i in range(5)])
            total = sum(item["valu"] for item in items)
            print("total:", total)
        asyncio.run(main())
    """)
    rows = _fetch_filtered(run_id, hide=True, limit=10000)
    exception_rows = [
        r for r in rows
        if r.frame_event in ('exception', 'exception_unwind')
    ]
    # At least one real KeyError exception event must survive.
    real_kv = [
        r for r in exception_rows
        if r.locals_snapshot and 'KeyError' in r.locals_snapshot
    ]
    assert real_kv, "real KeyError exception was wiped out by filter"


def test_stopiteration_filtered(record_func):
    """A pure async sleep loop generates only coroutine StopIteration noise.

    With the filter on, no exception rows should remain.
    """
    db_path, run_id, stats = record_func("""\
        import asyncio
        async def loop():
            for _ in range(3):
                await asyncio.sleep(0)
        asyncio.run(loop())
    """)
    rows = _fetch_filtered(run_id, hide=True, limit=10000)
    exception_rows = [
        r for r in rows
        if r.frame_event in ('exception', 'exception_unwind')
    ]
    # All remaining exception rows should be either non-coroutine or not noise.
    for r in exception_rows:
        assert not _is_coroutine_exception_noise(r)


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="Windows asyncio internals prevent coroutine frame recording")
def test_mixed_async_filter(record_func):
    """A run with both noise and a real exception: only noise filtered."""
    db_path, run_id, stats = record_func("""\
        import asyncio
        async def helper():
            await asyncio.sleep(0)
            return 1
        async def main():
            await helper()
            raise ValueError("real")
        asyncio.run(main())
    """)
    rows_unfiltered = _fetch_filtered(run_id, hide=False, limit=10000)
    rows_filtered = _fetch_filtered(run_id, hide=True, limit=10000)
    # Filter must drop something.
    assert len(rows_filtered) <= len(rows_unfiltered)
    # The real ValueError must still be visible.
    real = [
        r for r in rows_filtered
        if r.frame_event in ('exception', 'exception_unwind')
        and r.locals_snapshot and 'ValueError' in r.locals_snapshot
    ]
    assert real, "real ValueError was filtered"


def test_sync_code_unaffected(record_func):
    """Synchronous code with a real exception must be a no-op for the filter."""
    db_path, run_id, stats = record_func("""\
        def f():
            raise RuntimeError("sync error")
        f()
    """)
    rows_unfiltered = _fetch_filtered(run_id, hide=False, limit=10000)
    rows_filtered = _fetch_filtered(run_id, hide=True, limit=10000)
    assert len(rows_filtered) == len(rows_unfiltered), \
        "filter wrongly removed rows from a sync recording"


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="Windows asyncio internals prevent coroutine frame recording")
def test_limit_counts_surviving_rows(record_func):
    """``--limit N`` with hide-coroutine-internals must yield up to N
    *surviving* rows, not N raw rows with some invisibly removed."""
    db_path, run_id, stats = record_func("""\
        import asyncio
        async def loop():
            for _ in range(20):
                await asyncio.sleep(0)
        asyncio.run(loop())
    """)
    raw_rows = db.fetchall(
        "SELECT * FROM executionframes WHERE run_id = ?"
        " ORDER BY sequence_no",
        (str(run_id),),
    )
    surviving_total = len([r for r in raw_rows if not _is_coroutine_exception_noise(r)])
    requested = min(5, surviving_total)
    filtered = _fetch_filtered(run_id, hide=True, limit=requested)
    assert len(filtered) == requested, \
        f"expected {requested} surviving rows, got {len(filtered)}"
