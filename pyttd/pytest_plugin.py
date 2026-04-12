"""pytest plugin for pyttd — record time-travel traces for test runs.

Registered via pyproject.toml:
    [project.entry-points.pytest11]
    pyttd = "pyttd.pytest_plugin"

Inactive unless a ``--pyttd*`` flag is passed to pytest.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest


def pytest_addoption(parser):
    group = parser.getgroup("pyttd", "pyttd time-travel debugger")
    group.addoption(
        "--pyttd", action="store_true", default=False,
        help="Record every test with pyttd",
    )
    group.addoption(
        "--pyttd-on-fail", action="store_true", default=False,
        help="Record every test; keep only failing recordings",
    )
    group.addoption(
        "--pyttd-replay", action="store_true", default=False,
        help="Open interactive replay for the most recent pyttd-recorded failure",
    )
    group.addoption(
        "--pyttd-artifact-dir", default=".pyttd-artifacts",
        help="Directory for .pyttd.db files (default: .pyttd-artifacts/)",
    )
    group.addoption(
        "--pyttd-keep", type=int, default=10,
        help="Retain last N test recordings; evict older (default: 10)",
    )
    group.addoption(
        "--pyttd-max-db-size", type=int, default=100,
        help="Per-test max DB size in MB (default: 100)",
    )
    group.addoption(
        "--pyttd-include", action="append", default=[],
        help="Restrict recording to functions matching this pattern (repeatable)",
    )
    group.addoption(
        "--pyttd-exclude", action="append", default=[],
        help="Exclude matching functions (repeatable)",
    )


# ---- helpers ----

def _nodeid_to_hash(nodeid: str) -> str:
    """Short (6-char) hash of a test nodeid for unique filenames."""
    return hashlib.sha256(nodeid.encode()).hexdigest()[:6]


def _nodeid_to_stem(nodeid: str) -> str:
    """Convert a nodeid like tests/test_foo.py::test_bar[param] to a filename stem."""
    stem = nodeid.replace("::", "__").replace("/", "_").replace("\\", "_")
    # Remove characters unsafe for filenames
    stem = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in stem)
    return stem


def _db_name_for_nodeid(nodeid: str) -> str:
    h = _nodeid_to_hash(nodeid)
    stem = _nodeid_to_stem(nodeid)
    if len(stem) > 120:
        stem = stem[:120]
    return f"{stem}__{h}.pyttd.db"


def _manifest_path(artifact_dir: str) -> str:
    return os.path.join(artifact_dir, "MANIFEST.json")


def _load_manifest(artifact_dir: str) -> dict:
    mp = _manifest_path(artifact_dir)
    if os.path.isfile(mp):
        with open(mp) as f:
            return json.load(f)
    return {"version": 1, "tests": []}


def _save_manifest(artifact_dir: str, manifest: dict):
    mp = _manifest_path(artifact_dir)
    os.makedirs(artifact_dir, exist_ok=True)
    with open(mp, "w") as f:
        json.dump(manifest, f, indent=2)


def _evict_old_artifacts(artifact_dir: str, keep: int):
    """Remove oldest test DBs beyond the keep limit."""
    manifest = _load_manifest(artifact_dir)
    tests = manifest.get("tests", [])
    if len(tests) <= keep:
        return
    tests.sort(key=lambda t: t.get("timestamp", 0))
    to_remove = tests[:-keep] if keep > 0 else tests
    for entry in to_remove:
        _remove_db_files(entry.get("db_path", ""))
    manifest["tests"] = tests[-keep:] if keep > 0 else []
    _save_manifest(artifact_dir, manifest)


def _remove_db_files(db_path: str):
    """Remove a .pyttd.db and its WAL/SHM companions."""
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.isfile(p):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---- plugin state ----

class PyttdPluginState:
    """Carried on config._pyttd_state to share data across hooks."""

    def __init__(self, mode, artifact_dir, keep, max_db_size_mb,
                 include, exclude):
        self.mode = mode  # "all", "on_fail", "replay"
        self.artifact_dir = os.path.abspath(artifact_dir)
        self.keep = keep
        self.max_db_size_mb = max_db_size_mb
        self.include = include or []
        self.exclude = exclude or []
        self.manifest = {
            "version": 1,
            "session_id": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tests": [],
        }
        self.recorded_count = 0
        self.failed_count = 0


# ---- hooks ----

def pytest_configure(config):
    pyttd_all = config.getoption("--pyttd", default=False)
    pyttd_on_fail = config.getoption("--pyttd-on-fail", default=False)
    pyttd_replay = config.getoption("--pyttd-replay", default=False)

    if not any([pyttd_all, pyttd_on_fail, pyttd_replay]):
        return

    if pyttd_replay:
        mode = "replay"
    elif pyttd_on_fail:
        mode = "on_fail"
    else:
        mode = "all"

    state = PyttdPluginState(
        mode=mode,
        artifact_dir=config.getoption("--pyttd-artifact-dir"),
        keep=config.getoption("--pyttd-keep"),
        max_db_size_mb=config.getoption("--pyttd-max-db-size"),
        include=config.getoption("--pyttd-include"),
        exclude=config.getoption("--pyttd-exclude"),
    )
    config._pyttd_state = state

    if mode != "replay":
        os.makedirs(state.artifact_dir, exist_ok=True)
        _evict_old_artifacts(state.artifact_dir, state.keep)


def pytest_collection_modifyitems(session, config, items):
    state = getattr(config, "_pyttd_state", None)
    if state is None or state.mode != "replay":
        return

    manifest = _load_manifest(state.artifact_dir)
    failures = [t for t in manifest.get("tests", []) if t.get("status") == "failed"]
    if not failures:
        config.warn(
            pytest.PytestWarning(
                "pyttd: No failed recordings found in manifest. Nothing to replay."
            )
        )
        items.clear()
        return

    failures.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
    db_path = failures[0]["db_path"]
    if not os.path.isfile(db_path):
        config.warn(
            pytest.PytestWarning(f"pyttd: Recording DB not found: {db_path}")
        )
        items.clear()
        return

    state._replay_db = db_path
    import subprocess
    import sys
    print(f"\npyttd: Launching interactive replay of: {db_path}")
    subprocess.run(
        [sys.executable, "-m", "pyttd", "replay", "--last-run",
         "--interactive", "--db", db_path],
    )
    items.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    state = getattr(item.config, "_pyttd_state", None)
    if state is None or state.mode not in ("all", "on_fail"):
        return

    nodeid = item.nodeid
    db_name = _db_name_for_nodeid(nodeid)
    db_path = os.path.join(state.artifact_dir, db_name)
    _remove_db_files(db_path)

    try:
        from pyttd.main import _active_recorder, arm
        # Guard against nested arm (e.g., test itself calls arm())
        if _active_recorder is not None and _active_recorder._recording:
            return

        kwargs = {}
        if state.max_db_size_mb:
            kwargs["max_db_size_mb"] = state.max_db_size_mb
        if state.include:
            kwargs["include_functions"] = state.include
        if state.exclude:
            kwargs["exclude_functions"] = state.exclude

        arm(db_path=db_path, **kwargs)
        item._pyttd_db_path = db_path
        item._pyttd_start_time = time.time()
    except Exception as exc:
        import warnings
        warnings.warn(f"pyttd: Failed to arm recording for {nodeid}: {exc}")


@pytest.hookimpl(trylast=True, wrapper=True)
def pytest_runtest_makereport(item, call):
    """Stash test outcome on the item for use in teardown."""
    report = yield
    if call.when == "call" and hasattr(item, "_pyttd_db_path"):
        item._pyttd_status = (
            "passed" if report.passed else
            "failed" if report.failed else "skipped"
        )
        item._pyttd_exc_str = ""
        if report.failed and call.excinfo:
            try:
                item._pyttd_exc_str = str(call.excinfo.getrepr(style="short"))[:200]
            except Exception:
                item._pyttd_exc_str = str(call.excinfo.value)[:200]
    return report


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    state = getattr(item.config, "_pyttd_state", None)
    if state is None or not hasattr(item, "_pyttd_db_path"):
        return

    # Disarm first — must happen before any DB file cleanup
    try:
        from pyttd.main import _active_recorder, disarm
        if _active_recorder is not None and _active_recorder._recording:
            disarm()
    except Exception:
        pass

    db_path = item._pyttd_db_path
    status = getattr(item, "_pyttd_status", "skipped")
    start_time = getattr(item, "_pyttd_start_time", time.time())
    duration = time.time() - start_time

    entry = {
        "nodeid": item.nodeid,
        "hash": _nodeid_to_hash(item.nodeid),
        "db_path": db_path,
        "status": status,
        "duration_s": round(duration, 3),
        "exception": getattr(item, "_pyttd_exc_str", ""),
        "timestamp": time.time(),
    }

    if state.mode == "on_fail" and status != "failed":
        # Test passed — discard the recording
        _remove_db_files(db_path)
    else:
        state.manifest["tests"].append(entry)
        state.recorded_count += 1

    if status == "failed":
        state.failed_count += 1


def pytest_sessionfinish(session, exitstatus):
    state = getattr(session.config, "_pyttd_state", None)
    if state is None or state.mode == "replay":
        return
    _save_manifest(state.artifact_dir, state.manifest)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    state = getattr(config, "_pyttd_state", None)
    if state is None or state.mode == "replay":
        return

    if state.recorded_count > 0 or state.failed_count > 0:
        terminalreporter.write_sep("=", "pyttd recording summary")
        terminalreporter.write_line(
            f"pyttd: recorded {state.recorded_count} test(s), "
            f"{state.failed_count} failure(s)"
        )
        if state.failed_count > 0:
            terminalreporter.write_line(
                f"Replay failures: pytest --pyttd-replay "
                f"--pyttd-artifact-dir {state.artifact_dir}"
            )
        terminalreporter.write_line(f"Artifacts: {state.artifact_dir}")
