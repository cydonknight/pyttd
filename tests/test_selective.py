"""Tests for selective function recording (Phase 9B)."""
import json
import sys
import textwrap
import runpy

import pytest

import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db


def _record_with_file_filters(tmp_path, files: dict, main_script: str,
                              include_files=None, exclude_files=None,
                              exclude_functions=None):
    """Record a multi-file project with file-level filters.

    Args:
        files: dict of {filename: content} to write into tmp_path
        main_script: key in `files` that is the entry point
        include_files: list of glob patterns for --include-file
        exclude_files: list of glob patterns for --exclude-file
        exclude_functions: list of glob patterns for --exclude
    """
    for name, content in files.items():
        (tmp_path / name).write_text(textwrap.dedent(content))
    script_file = tmp_path / main_script
    db_path = str(tmp_path / "test.pyttd.db")
    delete_db_files(db_path)

    config = PyttdConfig(
        checkpoint_interval=0,
        include_files=include_files or [],
        exclude_files=exclude_files or [],
        exclude_functions=exclude_functions or [],
    )
    recorder = Recorder(config)
    recorder.start(db_path, script_path=str(script_file))

    old_argv = sys.argv[:]
    old_path = sys.path[:]
    sys.argv = [str(script_file)]
    sys.path.insert(0, str(tmp_path))
    try:
        runpy.run_path(str(script_file), run_name='__main__')
    except BaseException:
        pass
    finally:
        sys.argv = old_argv
        sys.path[:] = old_path
    stats = recorder.stop()
    run_id = recorder.run_id
    return db_path, run_id, stats


def _record_with_include(tmp_path, script_content, include_functions):
    script_file = tmp_path / "test_script.py"
    script_file.write_text(textwrap.dedent(script_content))
    db_path = str(tmp_path / "test.pyttd.db")
    delete_db_files(db_path)

    config = PyttdConfig(checkpoint_interval=0, include_functions=include_functions)
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


class TestSelectiveRecording:
    def test_include_filter_basic(self, tmp_path):
        """Only matching functions are recorded."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            def target_func():
                return 42

            def other_func():
                return 99

            target_func()
            other_func()
        ''', include_functions=['target_func'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'target_func' in funcs
            assert 'other_func' not in funcs
            assert '<module>' in funcs  # module-level always recorded
        finally:
            close_db()
            db.init(None)

    def test_include_filter_substring(self, tmp_path):
        """Substring matching: 'process' matches 'process_data'."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            def process_data():
                return 1

            def unrelated():
                return 2

            process_data()
            unrelated()
        ''', include_functions=['process'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'process_data' in funcs
            assert 'unrelated' not in funcs
        finally:
            close_db()
            db.init(None)

    def test_include_filter_empty_records_all(self, record_func):
        """Default behavior: empty include list records everything."""
        db_path, run_id, _ = record_func('''
            def foo():
                return 1
            def bar():
                return 2
            foo()
            bar()
        ''')
        rows = db.fetchall(
            "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
            (str(run_id),))
        funcs = set(r.function_name for r in rows)
        assert 'foo' in funcs
        assert 'bar' in funcs

    def test_include_filter_module_level(self, tmp_path):
        """<module> is always recorded even in include mode."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            x = 42
            def target():
                return x
            target()
        ''', include_functions=['target'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert '<module>' in funcs
            assert 'target' in funcs
        finally:
            close_db()
            db.init(None)

    def test_include_filter_cli(self):
        """Verify --include flag is accepted by CLI argument parser."""
        import argparse
        parser = argparse.ArgumentParser(prog='pyttd')
        subparsers = parser.add_subparsers(dest='command')
        record_parser = subparsers.add_parser('record')
        record_parser.add_argument('script')
        record_parser.add_argument('--include', nargs='*', default=None)
        args = parser.parse_args(['record', 'script.py', '--include', 'my_func'])
        assert args.include == ['my_func']

    def test_include_filter_does_not_affect_ignore(self, tmp_path):
        """Ignore patterns (stdlib exclusion) still work with include mode."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            import os
            def target():
                return os.getcwd()
            target()
        ''', include_functions=['target'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'target' in funcs
            # stdlib functions should not appear
            assert 'getcwd' not in funcs
        finally:
            close_db()
            db.init(None)

    @pytest.mark.skipif(sys.platform == 'win32', reason="Glob patterns use fnmatch (Unix only)")
    def test_include_glob_star(self, tmp_path):
        """Glob pattern 'process_*' matches 'process_data' but not 'my_process'."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            def process_data():
                return 1

            def my_process():
                return 2

            process_data()
            my_process()
        ''', include_functions=['process_*'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'process_data' in funcs
            assert 'my_process' not in funcs
        finally:
            close_db()
            db.init(None)

    @pytest.mark.skipif(sys.platform == 'win32', reason="Glob patterns use fnmatch (Unix only)")
    def test_include_glob_question(self, tmp_path):
        """Glob pattern 'test_?' matches 'test_a' but not 'test_ab'."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            def test_a():
                return 1

            def test_ab():
                return 2

            test_a()
            test_ab()
        ''', include_functions=['test_?'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'test_a' in funcs
            assert 'test_ab' not in funcs
        finally:
            close_db()
            db.init(None)

    def test_include_backward_compat(self, tmp_path):
        """Plain pattern (no glob chars) still matches via auto-wrapping as *pattern*."""
        db_path, run_id, _ = _record_with_include(tmp_path, '''
            def process_data():
                return 1

            def unrelated():
                return 2

            process_data()
            unrelated()
        ''', include_functions=['process'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            assert 'process_data' in funcs
            assert 'unrelated' not in funcs
        finally:
            close_db()
            db.init(None)

    def test_include_filter_resets_between_recordings(self, tmp_path):
        """Include filter doesn't leak between sessions."""
        # First recording with include
        db_path1, run_id1, _ = _record_with_include(tmp_path, '''
            def foo():
                return 1
            def bar():
                return 2
            foo()
            bar()
        ''', include_functions=['foo'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id1),))
            funcs1 = set(r.function_name for r in rows)
            assert 'foo' in funcs1
            assert 'bar' not in funcs1
        finally:
            close_db()
            db.init(None)

        # Second recording WITHOUT include — should record all
        script_file = tmp_path / "test_script2.py"
        script_file.write_text(textwrap.dedent('''
            def foo():
                return 1
            def bar():
                return 2
            foo()
            bar()
        '''))
        db_path2 = str(tmp_path / "test2.pyttd.db")
        delete_db_files(db_path2)
        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path2, script_path=str(script_file))
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        run_id2 = recorder.run_id
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes WHERE run_id = ?",
                (str(run_id2),))
            funcs2 = set(r.function_name for r in rows)
            assert 'foo' in funcs2
            assert 'bar' in funcs2
        finally:
            close_db()
            db.init(None)


@pytest.mark.skipif(sys.platform == 'win32', reason="File glob patterns use fnmatch (Unix only)")
class TestFileFilters:
    def test_exclude_file_glob_matches_full_path(self, tmp_path):
        """--exclude-file '*helper*' excludes non-module frames from helper.py."""
        db_path, run_id, _ = _record_with_file_filters(tmp_path,
            files={
                'helper.py': '''\
                    def helper_func():
                        return 42
                ''',
                'main.py': '''\
                    from helper import helper_func
                    def main_func():
                        return helper_func()
                    main_func()
                ''',
            },
            main_script='main.py',
            exclude_files=['*helper*'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes"
                " WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            # helper_func should be excluded (file pattern match)
            assert 'helper_func' not in funcs, \
                f"helper_func should be excluded but found in: {funcs}"
            # main_func should still be present
            assert 'main_func' in funcs
            # <module> is always recorded (even for excluded files)
            assert '<module>' in funcs
        finally:
            close_db()
            db.init(None)

    def test_include_file_glob_matches_full_path(self, tmp_path):
        """--include-file '*main*' only records non-module frames from main.py."""
        db_path, run_id, _ = _record_with_file_filters(tmp_path,
            files={
                'libmod.py': '''\
                    def lib_func():
                        return 99
                ''',
                'main.py': '''\
                    from libmod import lib_func
                    def main_func():
                        return lib_func()
                    main_func()
                ''',
            },
            main_script='main.py',
            include_files=['*main*'])
        try:
            rows = db.fetchall(
                "SELECT DISTINCT function_name FROM executionframes"
                " WHERE run_id = ?",
                (str(run_id),))
            funcs = set(r.function_name for r in rows)
            # lib_func should be excluded (file not in include list)
            assert 'lib_func' not in funcs, \
                f"lib_func should not appear but found in: {funcs}"
            # main_func should be present
            assert 'main_func' in funcs
        finally:
            close_db()
            db.init(None)
