"""Item #8 microbenchmark: secret-pattern matching throughput.

Records a workload with many unique local names drawn from a realistic mix
(a few secrets among dozens of non-secrets), so the should_redact path is
exercised heavily.  Compare us/event across builds to quantify the trie.
"""
import json
import tempfile
import time

from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models.storage import delete_db_files, close_db
from pyttd.recorder import Recorder


SCRIPT = """
def frame():
    # Mix of innocuous and secret-named locals; the default filter has 13
    # patterns, so the old code would run 13 substring scans per variable.
    user_id = 1
    email = 'a@b.c'
    name = 'alice'
    address = 'x'
    phone = 'y'
    postcode = 'z'
    country = 'uk'
    city = 'l'
    district = 'w'
    street = 's'
    language = 'en'
    timezone = 'utc'
    locale = 'en_GB'
    dob = '2000'
    gender = 'x'
    display_name = 'alice'
    handle = 'al'
    password = 'p'
    api_key = 'k'
    auth_token = 't'
    total = (user_id + 1)
    return total

for _ in range(2000):
    frame()
"""


def test_bench_secret_filter(tmp_path):
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

    # Confirm redaction still fires correctly.
    storage.connect_to_db(db_path)
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND locals_snapshot LIKE '%auth_token%'"
        " AND locals_snapshot LIKE '%api_key%'"
        " ORDER BY sequence_no LIMIT 1",
        (recorder.run_id,),
    )
    close_db()
    assert row is not None
    data = json.loads(row.locals_snapshot)
    assert data["password"] == "<redacted>"
    assert data["api_key"] == "<redacted>"
    assert data["auth_token"] == "<redacted>"
    # Non-secrets preserved
    assert data["user_id"] == "1"
    assert data["email"] == "'a@b.c'"
