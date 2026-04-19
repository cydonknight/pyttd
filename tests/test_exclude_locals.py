"""Item #5: --exclude-locals suppresses locals capture while keeping events.

Also verifies locals_max_depth behavior: user-code frames have locals, deeply
nested helpers do not."""
import os
import subprocess
import sys

from pyttd.models import storage
from pyttd.models.db import db


def _query_all(run_id):
    rows = db.fetchall(
        "SELECT filename, frame_event, locals_snapshot FROM executionframes"
        " WHERE run_id = ? ORDER BY sequence_no",
        (str(run_id),))
    return rows


def test_exclude_locals_flag_suppresses_locals(tmp_path):
    noisy = tmp_path / "noisy.py"
    noisy.write_text("def work():\n    x = 1\n    y = 2\n    return x + y\n")
    user = tmp_path / "user.py"
    user.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import noisy\n"
        "def main():\n"
        "    z = noisy.work()\n"
        "    return z\n"
        "main()\n"
    )
    db_path = tmp_path / "run.pyttd.db"

    result = subprocess.run(
        [sys.executable, "-m", "pyttd", "record",
         "--db-path", str(db_path),
         "--checkpoint-interval", "0",
         "--exclude-locals", "*noisy*",
         str(user)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    storage.connect_to_db(str(db_path))
    try:
        run = db.fetchone("SELECT run_id FROM runs ORDER BY timestamp_start DESC LIMIT 1")
        assert run is not None
        rows = _query_all(run.run_id)
        # Events from noisy.py should have NULL locals_snapshot.
        noisy_rows = [r for r in rows if r.filename.endswith("noisy.py")
                      and r.frame_event in ("line", "return")]
        user_rows = [r for r in rows if r.filename.endswith("user.py")
                     and r.frame_event == "line"]
        assert noisy_rows, "noisy.py events should still be recorded"
        assert user_rows, "user.py events should still be recorded"
        # At least one user-code frame had locals captured
        assert any(r.locals_snapshot for r in user_rows), \
            "user.py line events should have locals"
        # No noisy.py event leaked locals
        for r in noisy_rows:
            assert not r.locals_snapshot, \
                f"noisy.py event should have NULL locals, got: {r.locals_snapshot!r}"
    finally:
        storage.close_db()


def test_locals_max_depth_suppresses_deep_frames(tmp_path):
    script = tmp_path / "s.py"
    script.write_text(
        "def d1():\n    return d2()\n"
        "def d2():\n    return d3()\n"
        "def d3():\n    return d4()\n"
        "def d4():\n    return d5()\n"
        "def d5():\n    x = 1\n    return x\n"
        "d1()\n"
    )
    db_path = tmp_path / "r.pyttd.db"

    # depth = 2 means frames with call_depth > 2 have locals suppressed
    result = subprocess.run(
        [sys.executable, "-m", "pyttd", "record",
         "--db-path", str(db_path),
         "--checkpoint-interval", "0",
         "--locals-max-depth", "2",
         str(script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    storage.connect_to_db(str(db_path))
    try:
        run = db.fetchone("SELECT run_id FROM runs ORDER BY timestamp_start DESC LIMIT 1")
        rows = db.fetchall(
            "SELECT function_name, call_depth, locals_snapshot, frame_event"
            " FROM executionframes WHERE run_id = ?"
            " AND frame_event = 'line'"
            " ORDER BY sequence_no",
            (run.run_id,))
        shallow_with_locals = [r for r in rows if r.call_depth <= 2 and r.locals_snapshot]
        deep_without_locals = [r for r in rows if r.call_depth > 2 and not r.locals_snapshot]
        deep_with_locals = [r for r in rows if r.call_depth > 2 and r.locals_snapshot]

        assert shallow_with_locals, "shallow frames should have locals"
        assert deep_without_locals, "deep frames should be suppressed"
        assert not deep_with_locals, (
            f"deep frames should not have locals: "
            f"{[(r.function_name, r.call_depth) for r in deep_with_locals]}")
    finally:
        storage.close_db()
