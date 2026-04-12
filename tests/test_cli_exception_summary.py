"""Tests for _format_exception_location (Issue 1).

Verifies the post-recording exception summary picks the user-facing raise
site rather than coroutine-internal StopIteration noise emitted by CPython
when async code resumes from an await.
"""
import json
import sys

import pytest

from pyttd.cli import (
    _format_exception_location,
    _is_coroutine_machinery_row,
    _pick_user_exception,
)
from pyttd.models.db import db


def test_sync_exception_summary_points_at_raise_site(record_func):
    """For synchronous code, the summary should point at the raise line."""
    db_path, run_id, stats = record_func("""\
        def f():
            x = 1
            y = 2
            raise ValueError("boom")
        f()
    """)
    summary = _format_exception_location(db_path, run_id)
    assert summary is not None
    assert "ValueError" not in summary  # the message itself isn't printed
    assert ":4" in summary or ":5" in summary  # line of the raise (script body offset)
    # The function name should reference f — accept with or without ()
    assert "f" in summary


def test_summary_picks_deepest_frame_in_chain(record_func):
    """Polish: when an exception propagates through several frames, the
    summary should show the exception chain with deepest frame first."""
    db_path, run_id, stats = record_func("""\
        def deepest():
            raise RuntimeError("at the bottom")
        def middle():
            deepest()
        def outer():
            middle()
        outer()
    """)
    summary = _format_exception_location(db_path, run_id)
    assert summary is not None
    # Chain format: deepest should be labeled as raise site
    assert "raise ->" in summary
    assert "deepest" in summary
    # The raise -> line should come before propagated lines
    raise_pos = summary.index("raise ->")
    deepest_pos = summary.index("deepest")
    assert deepest_pos > raise_pos, \
        f"deepest should appear after 'raise ->' label, got: {summary}"


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="Windows asyncio internals prevent coroutine frame recording")
def test_async_exception_skips_coroutine_noise(record_func):
    """For async code with coroutine-internal StopIteration noise, the
    summary must point at the user-facing exception (the genexpr/main
    KeyError) — not at the StopIteration emitted by ``await asyncio.sleep``.
    """
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
    summary = _format_exception_location(db_path, run_id)
    assert summary is not None, "should produce a summary"
    # Must NOT point at fetch_item / await line
    assert "fetch_item" not in summary
    # Must point at main or the genexpr
    assert ("main" in summary) or ("genexpr" in summary) or ("<module>" in summary)


def test_user_stopiteration_is_honored(record_func):
    """If user code explicitly raises StopIteration, the fallback path
    should still surface it (rather than returning None)."""
    db_path, run_id, stats = record_func("""\
        def f():
            raise StopIteration("by design")
        try:
            f()
        except StopIteration:
            raise
    """)
    summary = _format_exception_location(db_path, run_id)
    # We just need a summary; the exact filtering may or may not honor it
    # depending on whether locals_snapshot is captured for that frame.
    assert summary is not None


def test_no_exception_returns_none(record_func, tmp_path):
    """A clean run should produce None from the summary helper."""
    db_path, run_id, stats = record_func("""\
        def f():
            return 42
        f()
    """)
    summary = _format_exception_location(db_path, run_id)
    assert summary is None


def test_malformed_locals_snapshot_is_tolerated(record_func):
    """Manually inserting a malformed locals_snapshot must not crash."""
    db_path, run_id, stats = record_func("""\
        def f():
            raise ValueError("oops")
        f()
    """)
    # Corrupt the most recent exception row's locals
    from pyttd.models import storage
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    db.execute(
        "UPDATE executionframes SET locals_snapshot = ?"
        " WHERE run_id = ? AND frame_event IN ('exception', 'exception_unwind')",
        ("{not json", str(run_id)),
    )
    db.commit()
    storage.close_db()

    # Should still return a string and not raise
    summary = _format_exception_location(db_path, run_id)
    assert summary is not None


# ---------------------------------------------------------------------------
# helper-level tests
# ---------------------------------------------------------------------------

class _Row:
    """Lightweight row stand-in for unit-testing the helper."""
    def __init__(self, locals_snapshot):
        self.locals_snapshot = locals_snapshot


def test_is_coroutine_machinery_row_stop_iteration():
    row = _Row(json.dumps({"__exception__": "StopIteration(0,)"}))
    assert _is_coroutine_machinery_row(row)


def test_is_coroutine_machinery_row_stop_async_iteration():
    row = _Row(json.dumps({"__exception__": "StopAsyncIteration()"}))
    assert _is_coroutine_machinery_row(row)


def test_is_coroutine_machinery_row_real_exception():
    row = _Row(json.dumps({"__exception__": "ValueError('boom')"}))
    assert not _is_coroutine_machinery_row(row)


def test_is_coroutine_machinery_row_no_locals():
    assert not _is_coroutine_machinery_row(_Row(None))
    assert not _is_coroutine_machinery_row(_Row(""))


def test_is_coroutine_machinery_row_malformed_json():
    assert not _is_coroutine_machinery_row(_Row("{not json"))


def test_is_coroutine_machinery_row_no_exception_key():
    row = _Row(json.dumps({"x": 1}))
    assert not _is_coroutine_machinery_row(row)
