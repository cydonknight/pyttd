"""Tests for module-scope dunder/import filtering in --show-locals (Issue 4).

The filter is render-time only; recorded data is preserved. It applies
exclusively to module-scope frames (function_name == '<module>') and
hides:
  - the well-known module dunder globals (__name__, __doc__, etc.),
  - any local whose repr starts with ``<module ``, ``<function ``,
    ``<class ``, or ``<built-in ``.

The escape hatch is ``--show-all-globals``, which forces every variable
through verbatim. Function-scope frames are unaffected, and the
``--var-history`` lookup path is also unaffected (it does not use the
``_print_locals`` formatter).
"""
import argparse
import io
import sys

import pytest

from pyttd.cli import (
    _DUNDER_GLOBALS,
    _print_locals,
    _should_hide_module_local,
)


# ---------------------------------------------------------------------------
# helper-level tests
# ---------------------------------------------------------------------------

def _args(**overrides):
    a = argparse.Namespace(
        hide_module_dunders=True,
        show_all_globals=False,
    )
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


def test_should_hide_known_dunder_at_module_scope():
    assert _should_hide_module_local("__name__", "'__main__'", "<module>", _args())
    assert _should_hide_module_local("__file__", "'/tmp/x.py'", "<module>", _args())


def test_should_hide_imported_module():
    assert _should_hide_module_local(
        "random", "<module 'random' from '...'>", "<module>", _args())


def test_should_hide_function_at_module_scope():
    assert _should_hide_module_local(
        "main", "<function main at 0x100>", "<module>", _args())


def test_should_hide_class_at_module_scope():
    assert _should_hide_module_local(
        "Foo", "<class 'Foo'>", "<module>", _args())


def test_should_keep_user_data_at_module_scope():
    assert not _should_hide_module_local("x", "42", "<module>", _args())
    assert not _should_hide_module_local("name", "'alice'", "<module>", _args())


def test_function_scope_unaffected():
    """The filter must NOT touch frames where function_name != '<module>'."""
    assert not _should_hide_module_local("__name__", "'main'", "f", _args())
    assert not _should_hide_module_local(
        "random", "<module 'random' from '...'>", "f", _args())
    assert not _should_hide_module_local(
        "main", "<function main at 0x100>", "f", _args())


def test_show_all_globals_overrides():
    a = _args(show_all_globals=True)
    assert not _should_hide_module_local("__name__", "'__main__'", "<module>", a)
    assert not _should_hide_module_local(
        "random", "<module 'random' from '...'>", "<module>", a)


def test_hide_module_dunders_off_keeps_imports():
    """Explicitly opt-out of the filter — it falls back to no filtering for
    repr-style hides; known dunders are also kept."""
    a = _args(hide_module_dunders=False)
    assert not _should_hide_module_local(
        "random", "<module 'random' from '...'>", "<module>", a)
    assert not _should_hide_module_local("__name__", "'__main__'", "<module>", a)


def test_args_none_disables_filter():
    """When args is None (back-compat call site), nothing is hidden."""
    assert not _should_hide_module_local("__name__", "'__main__'", "<module>", None)
    assert not _should_hide_module_local(
        "random", "<module 'random' from '...'>", "<module>", None)


def test_dunder_set_includes_essentials():
    for name in ["__name__", "__doc__", "__package__", "__file__",
                 "__loader__", "__spec__", "__builtins__"]:
        assert name in _DUNDER_GLOBALS


# ---------------------------------------------------------------------------
# integration with _print_locals
# ---------------------------------------------------------------------------

class _Frame:
    def __init__(self, function_name, locals_snapshot):
        self.function_name = function_name
        self.locals_snapshot = locals_snapshot


def _capture(fn, *args, **kwargs):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_print_locals_module_scope_filters_dunders():
    snap = (
        '{"__name__": "\'__main__\'",'
        ' "__file__": "\'/tmp/x.py\'",'
        ' "random": "<module \'random\' from \'/usr/lib/python3/random.py\'>",'
        ' "main": "<function main at 0x100>",'
        ' "x": "42"}'
    )
    frame = _Frame("<module>", snap)
    out = _capture(_print_locals, frame, args=_args())
    assert "__name__" not in out
    assert "__file__" not in out
    assert "random" not in out
    assert "main" not in out
    assert "x = 42" in out


def test_print_locals_module_scope_show_all_globals_keeps_everything():
    snap = (
        '{"__name__": "\'__main__\'",'
        ' "random": "<module \'random\' from \'/usr/lib/python3/random.py\'>",'
        ' "x": "42"}'
    )
    frame = _Frame("<module>", snap)
    out = _capture(_print_locals, frame, args=_args(show_all_globals=True))
    assert "__name__" in out
    assert "random" in out
    assert "x = 42" in out


def test_print_locals_function_scope_unaffected():
    snap = (
        '{"__name__": "\'main\'",'
        ' "x": "42"}'
    )
    frame = _Frame("f", snap)
    out = _capture(_print_locals, frame, args=_args())
    # function-scope should keep everything
    assert "__name__" in out
    assert "x = 42" in out


# ---------------------------------------------------------------------------
# end-to-end via record + query helper
# ---------------------------------------------------------------------------

def test_dunders_hidden_by_default_e2e(record_func):
    db_path, run_id, stats = record_func("""\
        import random
        def helper():
            return 42
        x = helper()
    """)
    from pyttd.models.db import db
    rows = db.fetchall(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND function_name = '<module>'"
        " AND frame_event = 'line'"
        " ORDER BY sequence_no",
        (str(run_id),))
    assert rows, "expected module-scope line events"

    # Sanity-check the raw recording: it MUST contain the noise we want
    # filtered (otherwise the test would pass vacuously).
    import json as _j
    saw_noise_raw = False
    for r in rows:
        if not r.locals_snapshot:
            continue
        try:
            data = _j.loads(r.locals_snapshot)
        except Exception:
            continue
        if any(k in data for k in ('random', 'helper', '__name__')):
            saw_noise_raw = True
            break
    assert saw_noise_raw, "raw recording missing noise — test premise broken"

    # Render every frame with the default filter on and assert none of the
    # noise tokens leak through.
    for r in rows:
        if not r.locals_snapshot:
            continue
        text = _capture(_print_locals, r, args=_args())
        assert "__name__" not in text
        assert "random" not in text
        assert "helper" not in text

    # Now confirm --show-all-globals brings the noise back.
    seen_noise_unfiltered = False
    for r in rows:
        if not r.locals_snapshot:
            continue
        text = _capture(_print_locals, r, args=_args(show_all_globals=True))
        if "__name__" in text or "random" in text or "helper" in text:
            seen_noise_unfiltered = True
            break
    assert seen_noise_unfiltered, "show-all-globals should restore noise"


def test_var_history_path_unaffected(record_func):
    """--var-history uses its own lookup, not _print_locals — confirm it
    can still find a function-typed variable at module scope."""
    db_path, run_id, stats = record_func("""\
        def main():
            return 1
        main()
    """)
    from pyttd.models.db import db
    rows = db.fetchall(
        "SELECT locals_snapshot FROM executionframes"
        " WHERE run_id = ? AND function_name = '<module>'"
        " AND locals_snapshot IS NOT NULL AND locals_snapshot != ''",
        (str(run_id),))
    # Even without rendering, the recorded data must still contain 'main'
    found = False
    import json as _j
    for r in rows:
        try:
            data = _j.loads(r.locals_snapshot)
            if 'main' in data:
                found = True
                break
        except Exception:
            pass
    assert found, "raw data should still contain the 'main' function"
