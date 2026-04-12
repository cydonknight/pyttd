"""Tests for interactive REPL readline polish (Feature 3)."""
import os
import pytest
from unittest.mock import MagicMock
from pyttd.cli import (
    _InteractiveCompleter,
    _REPL_COMMANDS,
    _setup_interactive_readline,
    _save_readline_history,
    _HISTORY_FILE,
)


class _MockSession:
    """Minimal session mock for completer tests."""

    def __init__(self, functions=None, filenames=None, variables=None):
        self._functions = functions or []
        self._filenames = filenames or []
        self._variables = variables or []

    def list_function_names(self):
        return self._functions

    def list_filenames(self):
        return self._filenames

    def list_variable_names(self):
        return self._variables


class TestCommandCompletion:
    """Tab-completing the first token should match commands."""

    def test_step_prefix_returns_step(self):
        c = _InteractiveCompleter(_MockSession())
        c._matches = list(c._candidates("st", "st"))
        matches = [m.strip() for m in c._matches]
        assert "step" in matches
        assert "step_into" in matches
        assert "stack" in matches

    def test_empty_returns_all_commands(self):
        c = _InteractiveCompleter(_MockSession())
        c._matches = list(c._candidates("", ""))
        assert len(c._matches) == len(_REPL_COMMANDS)

    def test_q_returns_quit(self):
        c = _InteractiveCompleter(_MockSession())
        c._matches = list(c._candidates("q", "q"))
        matches = [m.strip() for m in c._matches]
        assert "quit" in matches
        assert "q" in matches

    def test_no_match(self):
        c = _InteractiveCompleter(_MockSession())
        c._matches = list(c._candidates("zzz", "zzz"))
        assert c._matches == []


class TestFunctionCompletion:
    """search <text> should complete function names."""

    def test_search_completes_functions(self):
        session = _MockSession(functions=["normalize", "process", "main"])
        c = _InteractiveCompleter(session)
        c._matches = list(c._candidates("search n", "n"))
        assert "normalize" in c._matches

    def test_search_caches_results(self):
        session = _MockSession(functions=["foo", "bar"])
        c = _InteractiveCompleter(session)
        # First call populates cache
        list(c._candidates("search f", "f"))
        assert c._cache_function_names is not None
        # Second call uses cache (same list object)
        list(c._candidates("search b", "b"))
        assert set(c._cache_function_names) == {"foo", "bar"}


class TestVariableCompletion:
    """watch <text> should complete variable names."""

    def test_watch_completes_variables(self):
        session = _MockSession(variables=["count", "total", "items"])
        c = _InteractiveCompleter(session)
        c._matches = list(c._candidates("watch c", "c"))
        assert "count" in c._matches

    def test_watch_no_match(self):
        session = _MockSession(variables=["count", "total"])
        c = _InteractiveCompleter(session)
        c._matches = list(c._candidates("watch z", "z"))
        assert c._matches == []


class TestBreakCompletion:
    """break <text> should complete filenames."""

    def test_break_completes_filenames(self):
        session = _MockSession(filenames=["/path/to/script.py", "/path/to/helper.py"])
        c = _InteractiveCompleter(session)
        c._matches = list(c._candidates("break s", "s"))
        assert "script.py:" in c._matches

    def test_break_after_colon_returns_nothing(self):
        session = _MockSession(filenames=["/path/to/script.py"])
        c = _InteractiveCompleter(session)
        c._matches = list(c._candidates("break script.py:1", "script.py:1"))
        assert c._matches == []


class TestCompleteMethod:
    """The complete(text, state) API used by readline."""

    def test_complete_returns_matches_by_state(self):
        c = _InteractiveCompleter(_MockSession(functions=["normalize", "noop"]))
        # First call with state=0 initializes matches
        result0 = c.complete("n", 0)
        assert result0 is not None
        result1 = c.complete("n", 1)
        # result1 could be another match or None
        # Ensure state beyond matches returns None
        result_end = c.complete("n", 100)
        assert result_end is None


class TestHistoryPersistence:
    """History file read/write roundtrip."""

    def test_history_roundtrip(self, tmp_path, monkeypatch):
        histfile = str(tmp_path / "test_history")
        try:
            import readline
        except ImportError:
            pytest.skip("readline not available")

        # Write some history
        readline.clear_history()
        readline.add_history("goto 42")
        readline.add_history("step")
        readline.write_history_file(histfile)

        # Read it back
        readline.clear_history()
        readline.read_history_file(histfile)
        n = readline.get_current_history_length()
        assert n >= 2
        items = [readline.get_history_item(i) for i in range(1, n + 1)]
        assert "goto 42" in items
        assert "step" in items


class TestGracefulDegradation:
    """When readline is unavailable, REPL should still work."""

    def test_setup_without_readline(self, monkeypatch):
        """Simulate ImportError for readline — setup should not crash."""
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name in ('readline', 'pyreadline3'):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, '__import__', mock_import)

        session = _MockSession()
        # Should not raise
        _setup_interactive_readline(session)


class TestSessionListMethods:
    """Test the session helper methods used by the completer."""

    def test_list_function_names(self, record_func):
        db_path, run_id, _ = record_func("""
def foo():
    return 1

def bar():
    return 2

foo()
bar()
""")
        from pyttd.session import Session
        from pyttd.models.db import db
        session = Session()
        first_line = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        session.enter_replay(run_id, first_line.sequence_no if first_line else 0)

        names = session.list_function_names()
        assert "foo" in names
        assert "bar" in names

    def test_list_filenames(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    return 1
f()
""")
        from pyttd.session import Session
        from pyttd.models.db import db
        session = Session()
        first_line = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        session.enter_replay(run_id, first_line.sequence_no if first_line else 0)

        filenames = session.list_filenames()
        assert len(filenames) > 0
        assert any("test_script.py" in f for f in filenames)

    def test_list_variable_names(self, record_func):
        db_path, run_id, _ = record_func("""
def f():
    x = 42
    y = "hello"
    return x

f()
""")
        from pyttd.session import Session
        from pyttd.models.db import db
        session = Session()
        first_line = db.fetchone(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " ORDER BY sequence_no LIMIT 1",
            (str(run_id),))
        session.enter_replay(run_id, first_line.sequence_no if first_line else 0)

        var_names = session.list_variable_names()
        assert "x" in var_names
        assert "y" in var_names
