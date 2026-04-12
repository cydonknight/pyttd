"""Tests for Issue 3: exception_unwind line is the raise site, not entry line.

The eval hook used to record ``exception_unwind.line_no`` as the entry
line of the frame (the ``def`` line or module top), making it useless for
locating user errors. The trace function's ``PyTrace_EXCEPTION`` handler
already had the raise-site line via ``PyFrame_GetLineNumber``; the fix
threads that value through TLS so the eval hook can use it when emitting
the unwind event.
"""
import sys

import pytest

from pyttd.models.db import db


def _unwind_rows(run_id, function_name=None):
    sql = ("SELECT sequence_no, function_name, filename, line_no"
           " FROM executionframes"
           " WHERE run_id = ? AND frame_event = 'exception_unwind'")
    params = [str(run_id)]
    if function_name is not None:
        sql += " AND function_name = ?"
        params.append(function_name)
    sql += " ORDER BY sequence_no"
    return db.fetchall(sql, tuple(params))


def test_unwind_line_is_raise_site_sync(record_func):
    """Synchronous code: the unwind line for f() should be the raise line,
    not the def line."""
    db_path, run_id, stats = record_func("""\
        def f():
            x = 1
            y = 2
            raise ValueError("boom")
        f()
    """)
    rows = _unwind_rows(run_id, function_name='f')
    assert rows, "expected at least one exception_unwind for f"
    # The recorded script body starts at line 1, so:
    #   line 1 = def f():
    #   line 2 = x = 1
    #   line 3 = y = 2
    #   line 4 = raise ValueError(...)
    # The raise site is line 4. Old behavior recorded line 1 (the def).
    raise_lines = {r.line_no for r in rows}
    assert 4 in raise_lines, f"expected line 4 in {raise_lines}"
    assert 1 not in raise_lines, \
        f"unwind line should NOT be def line, got {raise_lines}"


def test_unwind_line_nested(record_func):
    """Two-level call where the inner raises. The inner's unwind line
    should be the raise site; the outer's should be the call site (NOT
    the outer's def line)."""
    db_path, run_id, stats = record_func("""\
        def inner():
            raise RuntimeError("nested")
        def outer():
            inner()
        outer()
    """)
    inner_rows = _unwind_rows(run_id, function_name='inner')
    outer_rows = _unwind_rows(run_id, function_name='outer')
    assert inner_rows, "expected unwind for inner"
    assert outer_rows, "expected unwind for outer"
    inner_lines = {r.line_no for r in inner_rows}
    outer_lines = {r.line_no for r in outer_rows}
    # inner's raise is on line 2 of the script body
    assert 2 in inner_lines, f"inner unwind expected at line 2, got {inner_lines}"
    # outer's call site is on line 4
    assert 4 in outer_lines, f"outer unwind expected at line 4, got {outer_lines}"
    # outer's def is line 3 — should NOT appear
    assert 3 not in outer_lines


@pytest.mark.skipif(sys.platform == 'win32',
                    reason="Windows asyncio internals prevent coroutine frame recording")
def test_unwind_line_async(record_func):
    """The Issue 1 reproducer: main()'s unwind line should be the sum
    expression line, not the def line."""
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
    main_rows = _unwind_rows(run_id, function_name='main')
    assert main_rows, "expected unwind events for main"
    main_lines = {r.line_no for r in main_rows}
    # script line numbers (textwrap.dedent leaves 1-indexed):
    #   1: import asyncio
    #   2: async def fetch_item(i):
    #   3:     await asyncio.sleep(0.001)
    #   4:     return {...}
    #   5: async def main():
    #   6:     items = await asyncio.gather(...)
    #   7:     total = sum(item["valu"] ...)   # raise site
    #   8:     print("total:", total)
    #   9: asyncio.run(main())
    # main's def is line 5; raise site is line 7.
    assert 7 in main_lines, f"main unwind expected at raise site (line 7), got {main_lines}"
    assert 5 not in main_lines, \
        f"main unwind should not be the def line (5), got {main_lines}"


def test_unwind_line_resets_across_runs(record_func):
    """Recording A then recording B: B's events must not carry A's
    stale line. Run B is exception-free, so it should have no unwind
    events at all."""
    db_path_a, run_a, _ = record_func("""\
        def f():
            raise ValueError("a")
        f()
    """)
    rows_a = _unwind_rows(run_a)
    assert rows_a, "run A should have unwind events"

    db_path_b, run_b, _ = record_func("""\
        def g():
            return 42
        g()
    """)
    rows_b = _unwind_rows(run_b)
    assert not rows_b, f"run B should be exception-free, got {rows_b}"
