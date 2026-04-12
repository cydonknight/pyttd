"""Tests for the pyttd pytest plugin (Feature 1).

Uses the pytester fixture for integration tests.
"""
import json
import os
import pytest
from pyttd.pytest_plugin import (
    _nodeid_to_hash,
    _nodeid_to_stem,
    _db_name_for_nodeid,
    _load_manifest,
    _save_manifest,
    _evict_old_artifacts,
    PyttdPluginState,
)

# ---- Unit tests for helpers ----

class TestNodeidHelpers:
    def test_hash_is_6_chars(self):
        h = _nodeid_to_hash("tests/test_foo.py::test_bar")
        assert len(h) == 6
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_deterministic(self):
        a = _nodeid_to_hash("tests/test_foo.py::test_bar")
        b = _nodeid_to_hash("tests/test_foo.py::test_bar")
        assert a == b

    def test_hash_differs_for_different_nodeids(self):
        a = _nodeid_to_hash("tests/test_foo.py::test_bar")
        b = _nodeid_to_hash("tests/test_foo.py::test_baz")
        assert a != b

    def test_stem_converts_separators(self):
        stem = _nodeid_to_stem("tests/test_foo.py::test_bar")
        assert "::" not in stem
        assert "/" not in stem
        assert stem == "tests_test_foo.py__test_bar"

    def test_stem_handles_parametrize(self):
        stem = _nodeid_to_stem("tests/test_foo.py::test_bar[param1-param2]")
        # [ and ] are replaced with _
        assert "[" not in stem
        assert "]" not in stem

    def test_db_name_includes_hash(self):
        name = _db_name_for_nodeid("tests/test_foo.py::test_bar")
        assert name.endswith(".pyttd.db")
        h = _nodeid_to_hash("tests/test_foo.py::test_bar")
        assert h in name


class TestManifest:
    def test_load_missing_returns_empty(self, tmp_path):
        m = _load_manifest(str(tmp_path / "nonexistent"))
        assert m["version"] == 1
        assert m["tests"] == []

    def test_save_and_load_roundtrip(self, tmp_path):
        d = str(tmp_path / "artifacts")
        manifest = {
            "version": 1,
            "tests": [{"nodeid": "test_a", "status": "passed"}],
        }
        _save_manifest(d, manifest)
        loaded = _load_manifest(d)
        assert loaded["tests"][0]["nodeid"] == "test_a"


class TestEviction:
    def test_evict_removes_oldest(self, tmp_path):
        d = str(tmp_path / "artifacts")
        os.makedirs(d, exist_ok=True)
        tests = []
        for i in range(5):
            db_path = os.path.join(d, f"test_{i}.pyttd.db")
            with open(db_path, "w") as f:
                f.write("x")
            tests.append({
                "nodeid": f"test_{i}",
                "db_path": db_path,
                "timestamp": float(i),
            })
        _save_manifest(d, {"version": 1, "tests": tests})
        _evict_old_artifacts(d, keep=2)
        m = _load_manifest(d)
        assert len(m["tests"]) == 2
        # Oldest (0,1,2) should be removed
        assert not os.path.isfile(os.path.join(d, "test_0.pyttd.db"))
        assert not os.path.isfile(os.path.join(d, "test_1.pyttd.db"))
        assert not os.path.isfile(os.path.join(d, "test_2.pyttd.db"))
        # Newest should remain
        assert os.path.isfile(os.path.join(d, "test_3.pyttd.db"))
        assert os.path.isfile(os.path.join(d, "test_4.pyttd.db"))


# ---- Integration tests using pytester ----

# pytester requires the pytester plugin (shipped with pytest)
pytest_plugins = ["pytester"]

# Common args to avoid pytest-benchmark issues in temp dirs without git
_NO_BENCH = ["-p", "no:benchmark"]


class TestPyttdAlwaysRecord:
    """--pyttd: record every test."""

    def test_creates_dbs_for_all_tests(self, pytester):
        pytester.makepyfile("""
            def test_pass():
                x = 1
                y = x + 1
                assert y == 2

            def test_also_pass():
                a = [1, 2, 3]
                assert sum(a) == 6
        """)
        result = pytester.runpytest_subprocess("--pyttd", "-v", *_NO_BENCH)
        result.assert_outcomes(passed=2)

        art_dir = pytester.path / ".pyttd-artifacts"
        assert art_dir.is_dir()
        dbs = list(art_dir.glob("*.pyttd.db"))
        assert len(dbs) >= 2

        # Check manifest
        manifest_file = art_dir / "MANIFEST.json"
        assert manifest_file.is_file()
        manifest = json.loads(manifest_file.read_text())
        assert len(manifest["tests"]) >= 2
        assert all(t["status"] == "passed" for t in manifest["tests"])

    def test_records_failing_test(self, pytester):
        pytester.makepyfile("""
            def test_fail():
                x = 42
                assert x == 99, "wrong value"
        """)
        result = pytester.runpytest_subprocess("--pyttd", *_NO_BENCH)
        result.assert_outcomes(failed=1)

        art_dir = pytester.path / ".pyttd-artifacts"
        manifest = json.loads((art_dir / "MANIFEST.json").read_text())
        failed_tests = [t for t in manifest["tests"] if t["status"] == "failed"]
        assert len(failed_tests) == 1
        assert os.path.isfile(failed_tests[0]["db_path"])

    def test_parametrized_tests_get_distinct_dbs(self, pytester):
        pytester.makepyfile("""
            import pytest

            @pytest.mark.parametrize("x", [1, 2, 3])
            def test_param(x):
                assert x > 0
        """)
        result = pytester.runpytest_subprocess("--pyttd", *_NO_BENCH)
        result.assert_outcomes(passed=3)

        art_dir = pytester.path / ".pyttd-artifacts"
        dbs = list(art_dir.glob("*.pyttd.db"))
        # Each parametrized variant should get its own DB
        assert len(dbs) >= 3


class TestPyttdOnFail:
    """--pyttd-on-fail: record all, keep only failures."""

    def test_passing_test_leaves_no_db(self, pytester):
        pytester.makepyfile("""
            def test_pass():
                assert 1 + 1 == 2
        """)
        result = pytester.runpytest_subprocess("--pyttd-on-fail", *_NO_BENCH)
        result.assert_outcomes(passed=1)

        art_dir = pytester.path / ".pyttd-artifacts"
        dbs = list(art_dir.glob("*.pyttd.db"))
        assert len(dbs) == 0

    def test_failing_test_keeps_db(self, pytester):
        pytester.makepyfile("""
            def test_fail():
                assert False, "boom"

            def test_pass():
                assert True
        """)
        result = pytester.runpytest_subprocess("--pyttd-on-fail", *_NO_BENCH)
        result.assert_outcomes(passed=1, failed=1)

        art_dir = pytester.path / ".pyttd-artifacts"
        manifest = json.loads((art_dir / "MANIFEST.json").read_text())
        # Only the failing test should be in the manifest
        assert len(manifest["tests"]) == 1
        assert manifest["tests"][0]["status"] == "failed"
        assert os.path.isfile(manifest["tests"][0]["db_path"])


class TestPyttdReplay:
    """--pyttd-replay: open interactive replay (mocked subprocess)."""

    def test_replay_skips_tests(self, pytester):
        # Pre-populate a manifest with a failure
        art_dir = pytester.path / ".pyttd-artifacts"
        art_dir.mkdir()
        fake_db = art_dir / "fake.pyttd.db"
        fake_db.write_text("fake")
        manifest = {
            "version": 1,
            "tests": [{
                "nodeid": "test_x.py::test_fail",
                "hash": "abc123",
                "db_path": str(fake_db),
                "status": "failed",
                "timestamp": 1000.0,
            }],
        }
        (art_dir / "MANIFEST.json").write_text(json.dumps(manifest))

        pytester.makepyfile("""
            def test_should_not_run():
                assert False
        """)
        # The subprocess that pytester launches will try to exec the
        # replay, which will fail (fake DB). The key assertion is that
        # no tests actually run (0 outcomes).
        result = pytester.runpytest_subprocess(
            "--pyttd-replay",
            f"--pyttd-artifact-dir={art_dir}",
            *_NO_BENCH,
        )
        result.assert_outcomes()


class TestPluginInactive:
    """Plugin should be a no-op when no --pyttd flag is passed."""

    def test_no_flag_no_artifacts(self, pytester):
        pytester.makepyfile("""
            def test_basic():
                assert True
        """)
        result = pytester.runpytest_subprocess(*_NO_BENCH)
        result.assert_outcomes(passed=1)

        art_dir = pytester.path / ".pyttd-artifacts"
        assert not art_dir.exists()
