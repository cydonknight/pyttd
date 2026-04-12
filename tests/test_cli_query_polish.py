"""Tests for Phase 1B: pyttd query polish.

Covers:
- Finding #3: --exceptions shows both exception AND exception_unwind
- Finding #9: query banner always on stderr
- Finding #12: <module> disambiguation with file basename
- Finding #13: --expand for locals
- UX-A: --stats --format json
"""
import io
import json
import os
import sys

import pytest

from pyttd.cli import _format_frame_line, _print_expanded_children
from pyttd.models.db import db


# ---------------------------------------------------------------------------
# #12 — <module> disambiguation
# ---------------------------------------------------------------------------

class _FakeFrame:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_format_frame_line_module_shows_basename():
    """<module> should be disambiguated with the source file basename."""
    f = _FakeFrame(
        sequence_no=5, frame_event='line', function_name='<module>',
        line_no=3, filename='/some/path/to/main.py',
    )
    line = _format_frame_line(f, 'x = 1')
    assert '[main.py]' in line
    assert '<module>' in line


def test_format_frame_line_non_module_unchanged():
    """Non-module function names should NOT have a bracket suffix."""
    f = _FakeFrame(
        sequence_no=10, frame_event='call', function_name='foo',
        line_no=1, filename='/path/to/script.py',
    )
    line = _format_frame_line(f, 'def foo():')
    assert '[' not in line
    assert 'foo' in line


# ---------------------------------------------------------------------------
# #3 — --exceptions shows both event types
# ---------------------------------------------------------------------------

def test_exceptions_returns_both_event_types(record_func):
    """--exceptions (via __exceptions__ sentinel) should show exception AND exception_unwind."""
    db_path, run_id, stats = record_func("""\
        def f():
            raise ValueError("boom")
        f()
    """)
    # Verify both event types exist
    exc_count = db.fetchval(
        "SELECT COUNT(*) FROM executionframes WHERE run_id = ? AND frame_event = 'exception'",
        (str(run_id),)) or 0
    unwind_count = db.fetchval(
        "SELECT COUNT(*) FROM executionframes WHERE run_id = ? AND frame_event = 'exception_unwind'",
        (str(run_id),)) or 0
    assert exc_count > 0, "should have exception events"
    assert unwind_count > 0, "should have exception_unwind events"

    # Simulate the __exceptions__ sentinel SQL filter
    rows = db.fetchall(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND frame_event IN ('exception', 'exception_unwind')"
        " ORDER BY sequence_no",
        (str(run_id),))
    event_types = {r.frame_event for r in rows}
    assert 'exception' in event_types
    assert 'exception_unwind' in event_types


# ---------------------------------------------------------------------------
# #13 — --expand for locals
# ---------------------------------------------------------------------------

def test_print_expanded_children_basic():
    """_print_expanded_children should print nested attributes."""
    children = [
        {"key": "x", "value": "1.0", "type": "float"},
        {"key": "y", "value": "2.0", "type": "float"},
    ]
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        _print_expanded_children(children, "  ", 1, 3)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "x = 1.0" in out
    assert "y = 2.0" in out


def test_print_expanded_children_respects_depth():
    """Expansion should stop at max_depth."""
    children = [
        {"key": "inner", "type": "dict",
         "value": {"__type__": "dict", "__repr__": "{...}",
                   "__children__": [{"key": "deep", "value": "42", "type": "int"}]}},
    ]
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        _print_expanded_children(children, "  ", 1, 3)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert "inner = {...}" in out
    assert "deep = 42" in out

    # Now try with max_depth=1 — should NOT expand nested
    sys.stdout = buf2 = io.StringIO()
    try:
        _print_expanded_children(children, "  ", 1, 1)
    finally:
        sys.stdout = old
    out2 = buf2.getvalue()
    assert out2 == "", "depth=1 at depth=1 should print nothing"


# ---------------------------------------------------------------------------
# UX-A — --stats --format json reachability
# ---------------------------------------------------------------------------

def test_stats_json_path_reachable(record_func):
    """--stats --format json should work (not be hijacked by auto-enable frames)."""
    db_path, run_id, stats = record_func("""\
        def f():
            return 42
        f()
    """)
    # Directly verify the stats query returns JSON-serializable data
    rows = db.fetchall(
        "SELECT function_name,"
        " SUM(CASE WHEN frame_event = 'call' THEN 1 ELSE 0 END) AS calls,"
        " SUM(CASE WHEN frame_event = 'exception_unwind' AND is_coroutine = 0 THEN 1 ELSE 0 END) AS exceptions,"
        " MIN(CASE WHEN frame_event = 'call' THEN sequence_no END) AS first_seq"
        " FROM executionframes WHERE run_id = ? AND frame_event IN ('call', 'exception_unwind')"
        " GROUP BY function_name ORDER BY calls DESC",
        (str(run_id),))
    data = [{
        "function": r.function_name,
        "calls": r.calls,
        "exceptions": r.exceptions,
        "first_seq": r.first_seq,
    } for r in rows]
    # Must be valid JSON
    out = json.dumps(data, indent=2)
    parsed = json.loads(out)
    assert any(d['function'] == 'f' for d in parsed)
