"""Tests for qualified name handling in filters and queries (Item 2)."""
import sys
import textwrap
import runpy

import pytest

import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.session import Session
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db


def _record_with_config(tmp_path, script_content, **config_kwargs):
    script_file = tmp_path / "test_script.py"
    script_file.write_text(textwrap.dedent(script_content))
    db_path = str(tmp_path / "test.pyttd.db")
    delete_db_files(db_path)

    config = PyttdConfig(checkpoint_interval=0, **config_kwargs)
    recorder = Recorder(config)
    recorder.start(db_path, script_path=str(script_file))

    old_argv = sys.argv[:]
    sys.argv = [str(script_file)]
    try:
        runpy.run_path(str(script_file), run_name='__main__')
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
    stats = recorder.stop()
    run_id = recorder.run_id
    return db_path, run_id, stats


def _enter_replay(session, run_id):
    first_line = db.fetchone(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no LIMIT 1",
        (str(run_id),))
    first_line_seq = first_line.sequence_no if first_line else 0
    session.enter_replay(run_id, first_line_seq)
    return first_line_seq


@pytest.mark.skipif(sys.platform == 'win32', reason="fnmatch bare-name matching is Unix only")
class TestIncludeMatchesBareQualname:
    def test_include_bare_name_matches_nested(self, tmp_path):
        """--include inner should record outer.<locals>.inner via bare-name match."""
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            def outer():
                def inner():
                    return 42
                return inner()
            outer()
        ''', include_functions=['inner'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            # The qualified name recorded by CPython is "outer.<locals>.inner"
            inner_funcs = [f for f in funcs if f.endswith('.inner') or f == 'inner']
            assert len(inner_funcs) >= 1, (
                f"Expected 'inner' (bare or qualified) in recording, got: {funcs}"
            )
        finally:
            close_db()
            db.init(None)

    def test_include_bare_name_matches_method(self, tmp_path):
        """--include method should record MyClass.method via bare-name match."""
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            class MyClass:
                def method(self):
                    return 99

            obj = MyClass()
            obj.method()
        ''', include_functions=['method'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            method_funcs = [f for f in funcs if f.endswith('.method') or f == 'method']
            assert len(method_funcs) >= 1, (
                f"Expected 'method' (bare or qualified) in recording, got: {funcs}"
            )
        finally:
            close_db()
            db.init(None)

    def test_include_bare_does_not_match_unrelated(self, tmp_path):
        """Bare-name glob 'exact_target' should not match 'other_func'."""
        # Use an exact glob (no wildcards) so auto-wrapping does not apply,
        # and the pattern only matches the bare name 'exact_target'.
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            def exact_target():
                return 1
            def other_func():
                return 2
            exact_target()
            other_func()
        ''', include_functions=['exact_target'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'exact_target' in funcs
            assert 'other_func' not in funcs
        finally:
            close_db()
            db.init(None)


@pytest.mark.skipif(sys.platform == 'win32', reason="fnmatch bare-name matching is Unix only")
class TestExcludeMatchesBareQualname:
    def test_exclude_bare_name_excludes_nested(self, tmp_path):
        """--exclude inner should exclude outer.<locals>.inner via bare-name match."""
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            def outer():
                def inner():
                    return 42
                return inner()
            def keeper():
                return 7
            outer()
            keeper()
        ''', exclude_functions=['inner'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            # Neither the bare name nor qualified form should be present
            inner_funcs = [f for f in funcs if f.endswith('.inner') or f == 'inner']
            assert len(inner_funcs) == 0, (
                f"Expected 'inner' to be excluded, but found: {inner_funcs}"
            )
            # keeper() should still be recorded
            assert 'keeper' in funcs
        finally:
            close_db()
            db.init(None)

    def test_exclude_bare_name_excludes_method(self, tmp_path):
        """--exclude method should exclude MyClass.method via bare-name match."""
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            class MyClass:
                def method(self):
                    return 99
                def other(self):
                    return 1
            obj = MyClass()
            obj.method()
            obj.other()
        ''', exclude_functions=['method'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            method_funcs = [f for f in funcs if f.endswith('.method') or f == 'method']
            assert len(method_funcs) == 0, (
                f"Expected 'method' to be excluded, but found: {method_funcs}"
            )
            other_funcs = [f for f in funcs if f.endswith('.other') or f == 'other']
            assert len(other_funcs) >= 1
        finally:
            close_db()
            db.init(None)


class TestExecutionStatsShortName:
    def test_short_name_field_present_simple(self, record_func):
        """shortName field is present for plain function names."""
        db_path, run_id, _ = record_func('''
            def foo():
                return 42
            foo()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        files = session.get_traced_files()
        script_file = [f for f in files if 'test_script.py' in f][0]
        result = session.get_execution_stats(script_file)

        foo_stats = [r for r in result if r['functionName'] == 'foo']
        assert len(foo_stats) == 1
        assert 'shortName' in foo_stats[0]
        assert foo_stats[0]['shortName'] == 'foo'

    def test_short_name_strips_qualname_prefix(self, tmp_path):
        """shortName strips the qualified name prefix for nested functions."""
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            def outer():
                def inner():
                    return 42
                return inner()
            outer()
        ''')
        try:
            session = Session()
            _enter_replay(session, run_id)

            files = session.get_traced_files()
            script_file = [f for f in files if 'test_script.py' in f][0]
            result = session.get_execution_stats(script_file)

            # Find the nested function entry (qualname: "outer.<locals>.inner")
            inner_stats = [r for r in result if 'inner' in r['functionName']]
            assert len(inner_stats) >= 1
            entry = inner_stats[0]
            assert 'shortName' in entry
            assert entry['shortName'] == 'inner'
            # Full qualname is preserved in functionName
            assert entry['functionName'] != entry['shortName']
        finally:
            close_db()
            db.init(None)

    def test_short_name_for_class_method(self, tmp_path):
        """shortName strips class prefix for methods."""
        db_path, run_id, _ = _record_with_config(tmp_path, '''
            class MyClass:
                def my_method(self):
                    return 1
            obj = MyClass()
            obj.my_method()
        ''')
        try:
            session = Session()
            _enter_replay(session, run_id)

            files = session.get_traced_files()
            script_file = [f for f in files if 'test_script.py' in f][0]
            result = session.get_execution_stats(script_file)

            method_stats = [r for r in result if 'my_method' in r['functionName']]
            assert len(method_stats) >= 1
            entry = method_stats[0]
            assert 'shortName' in entry
            assert entry['shortName'] == 'my_method'
        finally:
            close_db()
            db.init(None)

    def test_short_name_equals_function_name_for_plain(self, record_func):
        """shortName equals functionName when there is no dot in the name."""
        db_path, run_id, _ = record_func('''
            def plain():
                return 1
            plain()
        ''')
        session = Session()
        _enter_replay(session, run_id)

        files = session.get_traced_files()
        script_file = [f for f in files if 'test_script.py' in f][0]
        result = session.get_execution_stats(script_file)

        plain_stats = [r for r in result if r['functionName'] == 'plain']
        assert len(plain_stats) == 1
        assert plain_stats[0]['shortName'] == plain_stats[0]['functionName']
