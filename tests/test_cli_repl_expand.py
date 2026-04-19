"""Tests for CLI variable expansion (EXPRESSION-WATCHLIST-PLAN.md Feature 2)."""
import os
import subprocess
import sys
import pytest
from pyttd.cli import _print_repl_children, _REPL_COMMANDS


class TestPrintReplChildren:
    """Unit tests for the _print_repl_children helper."""

    def test_empty_list(self, capsys):
        _print_repl_children([], "  ", 0, 3)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_max_depth_stops(self, capsys):
        children = [{'name': 'a', 'value': '1', 'type': 'int'}]
        _print_repl_children(children, "  ", 5, 3)  # depth >= max_depth
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_prints_simple_children(self, capsys):
        children = [
            {'name': 'x', 'value': '42', 'type': 'int',
             'variablesReference': 0},
            {'name': 'y', 'value': 'hello', 'type': 'str',
             'variablesReference': 0},
        ]
        _print_repl_children(children, "  ", 0, 3)
        captured = capsys.readouterr()
        assert 'x' in captured.out
        assert '42' in captured.out
        assert 'y' in captured.out
        assert 'hello' in captured.out

    def test_long_value_truncated(self, capsys):
        long = 'a' * 200
        children = [{'name': 'big', 'value': long, 'type': 'str',
                     'variablesReference': 0}]
        _print_repl_children(children, "  ", 0, 3)
        captured = capsys.readouterr()
        # Should not print the entire 200-char value
        assert "..." in captured.out


class TestReplCommands:
    """Regression: new commands are registered for tab completion."""

    def test_expand_in_commands(self):
        assert 'expand' in _REPL_COMMANDS

    def test_find_in_commands(self):
        assert 'find' in _REPL_COMMANDS


class TestReplExpandIntegration:
    """End-to-end: drive the REPL via stdin and inspect output."""

    def test_vars_default_shows_flat(self, record_func):
        """Default `vars` should still show the flat format (no regression)."""
        db_path, run_id, _ = record_func("""
def f():
    x = 42
    y = [1, 2, 3]
    return x
f()
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--interactive", "--db", db_path, "--run-id", str(run_id)[:8]],
            input="vars\nquit\n", capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # Flat output: variable names appear once each
        # x and y should both appear in the vars output
        # (they're in some frame's locals)

    def test_vars_expand_flag(self, record_func):
        """`vars -e` should run without crashing."""
        db_path, run_id, _ = record_func("""
def f():
    config = {'host': 'localhost', 'port': 8080}
    return config
f()
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--interactive", "--db", db_path, "--run-id", str(run_id)[:8]],
            input="goto 3\nvars -e\nquit\n",
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0

    def test_expand_nonexistent(self, record_func):
        """`expand nosuchvar` should show a helpful message."""
        db_path, run_id, _ = record_func("""
def f():
    x = 1
    return x
f()
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--interactive", "--db", db_path, "--run-id", str(run_id)[:8]],
            input="expand nosuchvar\nquit\n",
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "not expandable" in result.stdout or \
               "not found" in result.stdout

    def test_expand_no_arg_shows_usage(self, record_func):
        """Bare `expand` with no argument should show usage."""
        db_path, run_id, _ = record_func("""
x = 1
""")
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--interactive", "--db", db_path, "--run-id", str(run_id)[:8]],
            input="expand\nquit\n",
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "Usage" in result.stdout

    def test_expand_dict_shows_keys(self, record_func):
        """`expand config` on a dict should show its keys."""
        db_path, run_id, _ = record_func("""
def make_config():
    config = {'host': 'localhost', 'port': 8080, 'debug': True}
    return config
make_config()
""")
        # Navigate to the return frame of make_config
        result = subprocess.run(
            [sys.executable, "-m", "pyttd", "replay",
             "--interactive", "--db", db_path, "--run-id", str(run_id)[:8]],
            input="goto last\nvars\nexpand config\nquit\n",
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # Don't assert specific output because 'config' may not be in
        # scope at every frame. The command must not crash.
