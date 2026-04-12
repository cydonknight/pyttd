"""Tests for trace diff mode (Feature 5)."""
import json
import os
import sys
import textwrap
import pytest
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.recorder import Recorder
from pyttd.models import storage
from pyttd.models.storage import delete_db_files, close_db
from pyttd.models.db import db as _db
from pyttd.diff import align_and_diff, DiffResult, format_diff_text, format_diff_json


@pytest.fixture
def diff_record(tmp_path):
    """Record two scripts into the same DB, returning (db_path, run_id_a, run_id_b).

    Unlike ``record_func``, this does NOT delete the DB between recordings,
    so both runs coexist for diffing.
    """
    db_path = str(tmp_path / "diff_test.pyttd.db")
    recorders = []

    def _record_one(script_content):
        script_file = tmp_path / "test_script.py"
        script_file.write_text(textwrap.dedent(script_content))

        config = PyttdConfig(checkpoint_interval=0)
        recorder = Recorder(config)
        recorder.start(db_path, script_path=str(script_file))
        recorders.append(recorder)

        import runpy
        old_argv = sys.argv[:]
        sys.argv = [str(script_file)]
        try:
            runpy.run_path(str(script_file), run_name='__main__')
        except BaseException:
            pass
        finally:
            sys.argv = old_argv
        recorder.stop()
        return recorder.run_id

    def _record_pair(script_a, script_b):
        delete_db_files(db_path)
        run_id_a = _record_one(script_a)
        run_id_b = _record_one(script_b)
        return db_path, run_id_a, run_id_b

    yield _record_pair

    for rec in recorders:
        try:
            pyttd_native.kill_all_checkpoints()
        except Exception:
            pass
    close_db()


class TestDiffIdentical:
    """Two identical runs should report no divergence."""

    def test_diff_identical_runs(self, diff_record):
        script = """
def compute(x):
    return x * 2

result = compute(5)
"""
        db_path, run_id_a, run_id_b = diff_record(script, script)

        storage.connect_to_db(db_path)
        storage.initialize_schema()
        result = align_and_diff(_db, str(run_id_a), str(run_id_b))
        storage.close_db()

        assert result.kind == "identical"


class TestDiffDataDivergence:
    """Same control flow, different variable values."""

    def test_data_divergence_detected(self, diff_record):
        script_a = """
def process(x):
    y = x + 1
    return y

process(10)
"""
        script_b = """
def process(x):
    y = x + 1
    return y

process(20)
"""
        db_path, run_id_a, run_id_b = diff_record(script_a, script_b)

        storage.connect_to_db(db_path)
        storage.initialize_schema()
        result = align_and_diff(_db, str(run_id_a), str(run_id_b))
        storage.close_db()

        assert result.kind == "data"
        assert result.seq_a is not None
        assert result.seq_b is not None
        assert len(result.diverging_vars) > 0
        # x should be the diverging variable
        var_names = [v[0] for v in result.diverging_vars]
        assert "x" in var_names


class TestDiffControlFlow:
    """Different branch paths = control-flow divergence."""

    def test_control_flow_divergence_detected(self, diff_record):
        script_a = """
def decide(x):
    if x > 5:
        result = "big"
    else:
        result = "small"
    return result

decide(10)
"""
        script_b = """
def decide(x):
    if x > 5:
        result = "big"
    else:
        result = "small"
    return result

decide(2)
"""
        db_path, run_id_a, run_id_b = diff_record(script_a, script_b)

        storage.connect_to_db(db_path)
        storage.initialize_schema()
        result = align_and_diff(_db, str(run_id_a), str(run_id_b))
        storage.close_db()

        # Should detect divergence (data or control-flow depending on
        # where the first difference appears)
        assert result.kind in ("data", "control_flow")


class TestDiffLengthMismatch:
    """One run is shorter than the other."""

    def test_length_mismatch_detected(self, diff_record):
        script_short = """
def f():
    return 1

f()
"""
        script_long = """
def f():
    return 1

def g():
    return 2

f()
g()
"""
        db_path, run_id_a, run_id_b = diff_record(script_short, script_long)

        storage.connect_to_db(db_path)
        storage.initialize_schema()
        result = align_and_diff(_db, str(run_id_a), str(run_id_b))
        storage.close_db()

        # Should detect either length mismatch or data divergence
        assert result.kind in ("length_mismatch", "data", "control_flow")


class TestDiffIgnoreVars:
    """--ignore-vars should skip specified variables."""

    def test_ignoring_variable_skips_it(self, diff_record):
        script_a = """
def f(x, timestamp):
    return x + timestamp

f(10, 1000)
"""
        script_b = """
def f(x, timestamp):
    return x + timestamp

f(10, 2000)
"""
        db_path, run_id_a, run_id_b = diff_record(script_a, script_b)

        storage.connect_to_db(db_path)
        storage.initialize_schema()

        # Without ignore: should find divergence on timestamp
        result_all = align_and_diff(_db, str(run_id_a), str(run_id_b))
        assert result_all.kind == "data"

        # With ignore: should still find divergence but not on timestamp
        result_ignore = align_and_diff(
            _db, str(run_id_a), str(run_id_b),
            ignore_vars={"timestamp"}
        )
        if result_ignore.kind == "data":
            var_names = [v[0] for v in result_ignore.diverging_vars]
            assert "timestamp" not in var_names

        storage.close_db()


class TestDiffFormatting:
    """Test text and JSON output formatters."""

    def test_text_format_data(self):
        result = DiffResult(
            kind="data",
            seq_a=42, seq_b=42,
            frame_a={"seq": 42, "function": "f", "filename": "a.py", "line": 10},
            frame_b={"seq": 42, "function": "f", "filename": "a.py", "line": 10},
            diverging_vars=[("x", 1, 2)],
            message="Data divergence",
        )
        text = format_diff_text(result, db_path="test.db")
        assert "Divergence at frame" in text
        assert "x:" in text
        assert "A: 1" in text
        assert "B: 2" in text

    def test_text_format_identical(self):
        result = DiffResult(kind="identical", message="Runs are identical.")
        text = format_diff_text(result)
        assert "identical" in text

    def test_json_format(self):
        result = DiffResult(
            kind="control_flow",
            seq_a=10, seq_b=12,
            frame_a={"seq": 10, "function": "f", "filename": "a.py", "line": 5},
            frame_b={"seq": 12, "function": "g", "filename": "a.py", "line": 8},
            message="Control flow diverges",
        )
        j = json.loads(format_diff_json(result))
        assert j["kind"] == "control_flow"
        assert j["seq_a"] == 10
        assert j["seq_b"] == 12


class TestDiffContext:
    """Context frames before divergence."""

    def test_context_frames_preserved(self, diff_record):
        script_a = """
def setup():
    a = 1
    b = 2
    return a + b

def compute(x):
    return x * 10

r = setup()
compute(r)
"""
        script_b = """
def setup():
    a = 1
    b = 2
    return a + b

def compute(x):
    return x * 10

r = setup()
compute(r + 1)
"""
        db_path, run_id_a, run_id_b = diff_record(script_a, script_b)

        storage.connect_to_db(db_path)
        storage.initialize_schema()
        result = align_and_diff(_db, str(run_id_a), str(run_id_b), context=5)
        storage.close_db()

        # There should be context frames if any frames matched before divergence
        if result.kind != "identical":
            assert isinstance(result.context_before, list)
