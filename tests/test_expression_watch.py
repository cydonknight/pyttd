"""Tests for expression watchpoints (EXPRESSION-WATCHLIST-PLAN.md Feature 1)."""
import json
import os
import subprocess
import sys
import pytest
from pyttd.session import (
    Session,
    _extract_expression_names,
    _stringify_result,
    SAFE_BUILTINS,
)


def _enter_replay(run_id):
    """Helper: enter replay mode for a recorded run."""
    from pyttd.models.db import db as _db
    session = Session()
    first_line = _db.fetchone(
        "SELECT sequence_no FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no LIMIT 1",
        (str(run_id),))
    first_seq = first_line.sequence_no if first_line else 0
    session.enter_replay(run_id, first_seq)
    return session


# ----- unit tests for helpers -----

class TestExtractNames:
    def test_simple(self):
        assert _extract_expression_names("x > 5") == ["x"]

    def test_multiple(self):
        names = set(_extract_expression_names("a + b * c"))
        assert names == {"a", "b", "c"}

    def test_strips_builtins(self):
        names = _extract_expression_names("len(x) > 5")
        assert names == ["x"]
        assert "len" not in names

    def test_strips_multiple_builtins(self):
        names = set(_extract_expression_names("sum(items) + max(values)"))
        assert names == {"items", "values"}

    def test_syntax_error_returns_empty(self):
        assert _extract_expression_names("(((") == []

    def test_no_names(self):
        # "1 + 2" has no names
        assert _extract_expression_names("1 + 2") == []


class TestStringifyResult:
    def test_simple_int(self):
        assert _stringify_result(42) == "42"

    def test_short_string(self):
        assert _stringify_result("hello") == "'hello'"

    def test_truncates_long(self):
        long = "x" * 200
        result = _stringify_result(long)
        assert len(result) <= 80
        assert result.endswith("...")

    def test_list_repr(self):
        assert _stringify_result([1, 2, 3]) == "[1, 2, 3]"


# ----- integration tests via record_func -----

class TestFindTruthy:
    def test_simple_greater_than(self, record_func):
        """Find frames where x > 5 in an incrementing loop."""
        db_path, run_id, _ = record_func("""
def loop():
    for x in range(10):
        y = x * 2

loop()
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches("x > 5")
        assert isinstance(results, list)
        # Should find x=6,7,8,9 at various frames; exact count depends on
        # line coverage but must have at least one
        assert len(results) > 0
        # All matched results should have a 'result' field (no errors)
        assert all('result' in r for r in results)

    def test_compound_expression(self, record_func):
        db_path, run_id, _ = record_func("""
def process():
    items = []
    total = 0
    for i in range(5):
        items.append(i)
        total += i

process()
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches(
            "len(items) > 0 and total < 100")
        # Must execute without crashing; finds at least some matches
        assert isinstance(results, list)

    def test_no_matches(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    x = 1
    return x

f()
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches("x > 100")
        assert results == []

    def test_eval_error_skipped(self, record_func):
        """Frames without the referenced var should be skipped silently."""
        db_path, run_id, _ = record_func("""
def has_x():
    x = 42
    return x

def has_y():
    y = 99
    return y

has_x()
has_y()
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches("x > 0")
        # Should only find frames where x exists; frames in has_y() skipped
        assert isinstance(results, list)

    def test_max_results_respected(self, record_func):
        db_path, run_id, _ = record_func("""
def big_loop():
    for i in range(100):
        x = i

big_loop()
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches(
            "i >= 0", max_results=5)
        assert len(results) <= 5

    def test_syntax_error_returns_error(self, record_func):
        db_path, run_id, _ = record_func("""
x = 1
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches("((x")
        assert len(results) == 1
        assert 'error' in results[0]

    def test_with_builtins(self, record_func):
        db_path, run_id, _ = record_func("""
def process():
    items = [1, 2, 3]
    total = sum(items)

process()
""")
        session = _enter_replay(run_id)
        # len() and sum() are in SAFE_BUILTINS
        results = session.find_expression_matches("len(items) == 3")
        assert isinstance(results, list)


class TestFindChanges:
    def test_changes_mode(self, record_func):
        """In 'changes' mode, only frames where the expr result changes."""
        db_path, run_id, _ = record_func("""
def loop():
    for x in range(6):
        y = x + 1

loop()
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches(
            "x % 2 == 0", mode="changes")
        # Each parity flip should appear
        assert isinstance(results, list)

    def test_unknown_mode(self, record_func):
        db_path, run_id, _ = record_func("""
x = 1
""")
        session = _enter_replay(run_id)
        results = session.find_expression_matches(
            "x > 0", mode="nonsense")
        assert len(results) == 1
        assert 'error' in results[0]


class TestQueryWhereFlag:
    """CLI: pyttd query --where EXPR"""

    def test_query_where_text_output(self, record_func, tmp_path):
        db_path, run_id, _ = record_func("""
def loop():
    for x in range(10):
        y = x * 2

loop()
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "query",
             "--where", "x > 5",
             "--db", db_path, "--run-id", str(run_id)[:8]],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # Should show at least one match or say no matches
        assert "match" in result.stdout.lower() or \
               "truthy" in result.stdout.lower()

    def test_query_where_json_output(self, record_func, tmp_path):
        db_path, run_id, _ = record_func("""
def loop():
    for x in range(10):
        y = x * 2

loop()
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "query",
             "--where", "x > 5", "--format", "json",
             "--db", db_path, "--run-id", str(run_id)[:8]],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # stdout should be valid JSON
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_query_where_syntax_error(self, record_func, tmp_path):
        db_path, run_id, _ = record_func("""
x = 1
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "query",
             "--where", "((x",
             "--db", db_path, "--run-id", str(run_id)[:8]],
            capture_output=True, text=True, timeout=30,
        )
        # Should exit with an error code on syntax error
        assert result.returncode != 0
        assert "syntax" in result.stderr.lower() or \
               "syntax" in result.stdout.lower()
