"""Item #4 microbenchmark: locals serialization when all locals are primitives.

Records a tight loop whose frame holds 50 primitive locals, then reports the
per-event cost.  Compare us/event across builds to quantify the fast path.

Usage:
    .venv/bin/pytest benchmarks/bench_locals_primitives.py -v -s
"""
import json
import tempfile
import time

import pytest

from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db
from pyttd.recorder import Recorder


SCRIPT = """
def frame():
    a0, a1, a2, a3, a4 = 0, 1, 2, 3, 4
    b0, b1, b2, b3, b4 = 5, 6, 7, 8, 9
    c0, c1, c2, c3, c4 = 10.5, 20.5, 30.5, 40.5, 50.5
    d0, d1, d2, d3, d4 = True, False, None, True, False
    e0, e1, e2, e3, e4 = 100, 200, 300, 400, 500
    total = a0 + a1 + a2 + a3 + a4 + b0 + b1 + b2 + b3 + b4
    total += e0 + e1 + e2 + e3 + e4
    return total

for _ in range(2000):
    frame()
"""


def test_bench_locals_primitives(tmp_path):
    script = tmp_path / "w.py"
    script.write_text(SCRIPT)
    db_path = str(tmp_path / "w.pyttd.db")
    delete_db_files(db_path)

    config = PyttdConfig(checkpoint_interval=0)
    recorder = Recorder(config)
    recorder.start(db_path, script_path=str(script))

    import runpy
    t0 = time.perf_counter()
    runpy.run_path(str(script), run_name="__main__")
    elapsed = time.perf_counter() - t0
    stats = recorder.stop()

    frames = stats.get("frame_count", 0)
    us_per_event = (elapsed / max(frames, 1)) * 1e6
    print(f"\n  elapsed={elapsed:.3f}s frames={frames} us/event={us_per_event:.2f}")

    # Ensure locals actually contain the primitives we expect
    storage.connect_to_db(db_path)
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND locals_snapshot LIKE '%\"a0\"%'"
        " ORDER BY sequence_no LIMIT 1",
        (recorder.run_id,),
    )
    close_db()
    assert row is not None
    parsed = json.loads(row.locals_snapshot)
    assert parsed["a0"] == "0"
    assert parsed["d0"] == "True"
    assert parsed["d2"] == "None"
    assert parsed["c0"] == "10.5"
