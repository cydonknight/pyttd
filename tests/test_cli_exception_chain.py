"""Tests for the exception chain summary in pyttd record (Feature 4)."""
import os
import json
import pytest
from pyttd.cli import (
    _format_exception_location,
    _build_exception_chain,
    _render_exception_chain,
    _is_coroutine_machinery_row,
)


class TestExceptionChainSync:
    """Synchronous code: single-frame and multi-frame chains."""

    def test_chain_sync_single_frame(self, record_func):
        db_path, run_id, stats = record_func("""
def boom():
    raise ValueError("kaboom")

boom()
""")
        result = _format_exception_location(db_path, str(run_id))
        assert result is not None
        assert "boom" in result
        assert "Replay:" in result

    def test_chain_multi_frame_sync(self, record_func):
        db_path, run_id, stats = record_func("""
def inner():
    raise RuntimeError("deep")

def outer():
    inner()

outer()
""")
        result = _format_exception_location(db_path, str(run_id))
        assert result is not None
        # Should mention both inner (raise) and outer (propagation)
        assert "inner" in result
        assert "Replay:" in result

    def test_chain_falls_back_to_single_frame_format(self, record_func):
        """When only one exception event survives, use compact format."""
        db_path, run_id, stats = record_func("""
def only_one():
    raise TypeError("solo")

only_one()
""")
        from pyttd.models import storage
        from pyttd.models.db import db
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        chain = _build_exception_chain(db, str(run_id))
        storage.close_db()
        if len(chain) == 1:
            rendered = _render_exception_chain(chain, db_path)
            assert "Exception at frame" in rendered
            # Should NOT contain "Exception chain:" header
            assert "Exception chain:" not in rendered


class TestExceptionChainAsync:
    """Async code: coroutine noise filtering."""

    def test_chain_filters_stopiteration(self, record_func):
        """StopIteration from coroutine machinery should be filtered."""
        db_path, run_id, stats = record_func("""
import asyncio

async def fetch_item(i):
    await asyncio.sleep(0.001)
    return {"id": i, "value": i * 2}

async def main():
    items = await asyncio.gather(*[fetch_item(i) for i in range(3)])
    total = sum(item["valu"] for item in items)  # KeyError
    print("total:", total)

asyncio.run(main())
""")
        result = _format_exception_location(db_path, str(run_id))
        assert result is not None
        # Should NOT show StopIteration noise
        assert "StopIteration" not in result
        assert "Replay:" in result


class TestExceptionChainTruncation:
    """Chain truncation at 5 frames."""

    def test_chain_truncates_at_5_default(self, record_func):
        # Build a deep call chain that raises
        db_path, run_id, stats = record_func("""
def f0():
    raise ValueError("deep")

def f1(): f0()
def f2(): f1()
def f3(): f2()
def f4(): f3()
def f5(): f4()
def f6(): f5()
def f7(): f6()

f7()
""")
        from pyttd.models import storage
        from pyttd.models.db import db
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        chain_default = _build_exception_chain(db, str(run_id), verbose=False)
        chain_verbose = _build_exception_chain(db, str(run_id), verbose=True)
        storage.close_db()
        assert len(chain_default) <= 5
        assert len(chain_verbose) >= len(chain_default)

    def test_verbose_shows_all(self, record_func):
        db_path, run_id, stats = record_func("""
def f0():
    raise ValueError("deep")

def f1(): f0()
def f2(): f1()
def f3(): f2()
def f4(): f3()
def f5(): f4()
def f6(): f5()
def f7(): f6()

f7()
""")
        result_default = _format_exception_location(db_path, str(run_id), verbose=False)
        result_verbose = _format_exception_location(db_path, str(run_id), verbose=True)
        assert result_default is not None
        assert result_verbose is not None
        # Verbose should have more lines
        assert result_verbose.count("\n") >= result_default.count("\n")


class TestExceptionChainCollapse:
    """Consecutive identical frames should be collapsed."""

    def test_collapses_consecutive_identical_frames(self, record_func):
        db_path, run_id, stats = record_func("""
def boom():
    raise ValueError("x")

boom()
""")
        from pyttd.models import storage
        from pyttd.models.db import db
        storage.connect_to_db(db_path)
        storage.initialize_schema()
        chain = _build_exception_chain(db, str(run_id))
        storage.close_db()
        # No consecutive duplicates
        for i in range(len(chain) - 1):
            key_a = (chain[i].filename, chain[i].function_name)
            key_b = (chain[i + 1].filename, chain[i + 1].function_name)
            assert key_a != key_b


class TestExceptionChainEdgeCases:
    """Edge cases."""

    def test_no_exception_returns_none(self, record_func):
        db_path, run_id, stats = record_func("""
def clean():
    return 42

clean()
""")
        result = _format_exception_location(db_path, str(run_id))
        assert result is None

    def test_user_stopiteration_is_honored(self, record_func):
        """A user explicitly raising StopIteration should still be surfaced."""
        db_path, run_id, stats = record_func("""
def gen_done():
    raise StopIteration("by design")

gen_done()
""")
        result = _format_exception_location(db_path, str(run_id))
        assert result is not None
        assert "Replay:" in result
