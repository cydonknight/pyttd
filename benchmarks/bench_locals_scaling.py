"""Locals serialization scaling benchmarks.

Parametric tests showing how recording overhead scales with locals count,
variable type, container size, and return-only optimization effectiveness.

Run: .venv/bin/pytest benchmarks/bench_locals_scaling.py -v -s
"""
import os

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report(stats, db_path, label=""):
    fc = stats.get('frame_count', 0)
    elapsed = stats.get('elapsed_time', 0.001)
    us = elapsed / fc * 1_000_000 if fc else 0
    db_size = os.path.getsize(db_path)
    bpf = db_size / fc if fc else 0
    print(f"\n  {label}: {fc:,} events, {us:.1f} us/event, {bpf:.0f} bytes/event")
    return us, bpf


def _make_count_workload(n):
    """Generate a workload with n local variables, called 1000 times."""
    assigns = "\n    ".join(f"v{i} = {i}" for i in range(n))
    total = " + ".join(f"v{i}" for i in range(n))
    return f"def work():\n    {assigns}\n    return {total}\n\nfor _ in range(1000):\n    work()\n"


def _make_container_workload(n):
    """Generate a workload with one dict of size n, called 500 times."""
    items = ", ".join(f'"{i}": {i}' for i in range(n))
    return f"def work():\n    d = {{{items}}}\n    return len(d)\n\nfor _ in range(500):\n    work()\n"


# ---------------------------------------------------------------------------
# Type workloads
# ---------------------------------------------------------------------------

TYPE_WORKLOADS = {
    'int': "a = 1; b = 2; c = 3; d = 4; e = 5",
    'float': "a = 1.1; b = 2.2; c = 3.3; d = 4.4; e = 5.5",
    'str': "a = 'hello'; b = 'world'; c = 'foo'; d = 'bar'; e = 'baz'",
    'bool': "a = True; b = False; c = True; d = False; e = True",
    'mixed': "a = 1; b = 2.5; c = 'hi'; d = True; e = None",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLocalsCountScaling:
    """How does overhead scale with the number of local variables?"""

    @pytest.mark.parametrize("n_locals", [1, 5, 10, 20, 50])
    def test_locals_count(self, bench_record, n_locals):
        script = _make_count_workload(n_locals)
        db_path, _, stats = bench_record(script)
        us, bpf = _report(stats, db_path, f"{n_locals} locals")
        assert stats.get('dropped_frames', 0) == 0


class TestTypeScaling:
    """How does variable type affect recording throughput?

    int/float/bool use fast_repr (no PyObject_Repr); str/mixed are slower.
    """

    @pytest.mark.parametrize("type_name", list(TYPE_WORKLOADS.keys()))
    def test_type(self, bench_record, type_name):
        assigns = TYPE_WORKLOADS[type_name]
        script = (
            f"def work():\n"
            f"    {assigns}\n"
            f"    return a\n"
            f"\n"
            f"for _ in range(2000):\n"
            f"    work()\n"
        )
        db_path, _, stats = bench_record(script)
        us, bpf = _report(stats, db_path, f"type={type_name}")
        assert stats.get('dropped_frames', 0) == 0


class TestContainerSizeScaling:
    """How does container size affect expandable serialization?"""

    @pytest.mark.parametrize("dict_size", [1, 10, 50, 100])
    def test_container_size(self, bench_record, dict_size):
        script = _make_container_workload(dict_size)
        db_path, _, stats = bench_record(script)
        us, bpf = _report(stats, db_path, f"dict size={dict_size}")
        assert stats.get('dropped_frames', 0) == 0


class TestReturnOnlyEffectiveness:
    """Compare single-line vs multi-line functions.

    Both should trigger return-only optimization (Opt 2), but single-line
    functions have the RETURN do full serialization (no prior LINE captured
    locals), while multi-line functions have LINE capture locals first.
    """

    def test_single_line_function(self, bench_record):
        """Single-line: RETURN does full serialize (no prior LINE locals)."""
        script = "def f(x):\n    return x + 1\n\nfor i in range(5000):\n    f(i)\n"
        db_path, _, stats = bench_record(script)
        us, bpf = _report(stats, db_path, "Single-line fn")

    def test_multi_line_function(self, bench_record):
        """Multi-line: LINE captures locals, RETURN is return-only."""
        script = "def f(x):\n    y = x + 1\n    return y\n\nfor i in range(5000):\n    f(i)\n"
        db_path, _, stats = bench_record(script)
        us, bpf = _report(stats, db_path, "Multi-line fn")
