"""Item #4 correctness: confirm the write_primitive_json fast path falls back
cleanly for Decimal, datetime, custom objects, and bignums.  The recorded
locals_snapshot must contain the PyObject_Repr for these values."""
import json

from pyttd.models.db import db


def test_decimal_falls_back(record_func):
    _, run_id, _ = record_func('''
        from decimal import Decimal
        x = Decimal("3.14")
        sentinel = 1
    ''')
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND locals_snapshot LIKE '%\"x\"%'"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id),))
    assert row is not None
    data = json.loads(row.locals_snapshot)
    assert "Decimal" in data["x"]


def test_datetime_falls_back(record_func):
    _, run_id, _ = record_func('''
        import datetime
        d = datetime.date(2024, 1, 15)
        sentinel = 1
    ''')
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND locals_snapshot LIKE '%\"d\"%'"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id),))
    assert row is not None
    data = json.loads(row.locals_snapshot)
    assert "datetime.date" in data["d"]


def test_custom_object_falls_back(record_func):
    _, run_id, _ = record_func('''
        class Widget:
            def __init__(self, n):
                self.n = n
            def __repr__(self):
                return f"Widget({self.n!r})"
        obj = Widget("hello")
        sentinel = 1
    ''')
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND locals_snapshot LIKE '%\"obj\"%'"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id),))
    assert row is not None
    data = json.loads(row.locals_snapshot)
    # Custom classes with __dict__ go through the expandable path —
    # the payload is a dict containing __repr__.
    val = data["obj"]
    if isinstance(val, dict):
        assert "Widget" in val.get("__repr__", "")
    else:
        assert "Widget" in val


def test_bignum_falls_back(record_func):
    _, run_id, _ = record_func('''
        big = 2 ** 200
        sentinel = 1
    ''')
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND locals_snapshot LIKE '%\"big\"%'"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id),))
    assert row is not None
    data = json.loads(row.locals_snapshot)
    # 2**200 is a specific number — just check it's a huge digit string
    assert len(data["big"]) > 50
    assert data["big"].lstrip("-").isdigit()


def test_primitives_match_repr(record_func):
    _, run_id, _ = record_func('''
        def f():
            a = 42
            b = -17
            c = True
            d = False
            e = None
            f = 3.14
            g = float('inf')
            h = float('-inf')
            return (a, b, c, d, e, f, g, h)
        f()
    ''')
    # Fetch the last line snapshot in f() that has all locals
    row = db.fetchone(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " AND locals_snapshot LIKE '%\"a\"%\"h\"%'"
        " ORDER BY sequence_no DESC LIMIT 1",
        (str(run_id),))
    assert row is not None
    data = json.loads(row.locals_snapshot)
    assert data["a"] == "42"
    assert data["b"] == "-17"
    assert data["c"] == "True"
    assert data["d"] == "False"
    assert data["e"] == "None"
    assert data["f"] == "3.14"
    assert data["g"] == "inf"
    assert data["h"] == "-inf"
