"""Recorder microbenchmarks: isolating specific recording subsystems.

Tests that measure per-event cost slopes, verify adaptive sampling behavior,
confirm return-only snapshot content, compare type repr throughput, and
measure true in-process overhead without subprocess startup.

Run: .venv/bin/pytest benchmarks/bench_recorder_micro.py -v -s
"""
import json
import os
import runpy
import sys
import time

import pytest

from pyttd.models.db import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _us_per_event(stats):
    fc = stats.get('frame_count', 0)
    elapsed = stats.get('elapsed_time', 0.001)
    return elapsed / fc * 1_000_000 if fc else 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPerEventCost:
    """Estimate per-event cost via linear regression over loop sizes."""

    def test_per_event_cost_slope(self, bench_record):
        """Record tight loops at 1K/10K/100K, compute slope = per-event cost."""
        points = []
        for n in [1000, 10000, 100000]:
            script = f"total = 0\nfor i in range({n}):\n    total += i\n"
            _, _, stats = bench_record(script)
            points.append((stats['frame_count'], stats['elapsed_time']))

        # Slope from smallest to largest (most stable estimate)
        d_events = points[2][0] - points[0][0]
        d_time = points[2][1] - points[0][1]
        slope_us = (d_time / d_events) * 1_000_000 if d_events else 0

        print(f"\n  Per-event cost (slope): {slope_us:.2f} us/event")
        print(f"    1K:   {points[0][0]:>7,} events in {points[0][1]:.3f}s")
        print(f"    10K:  {points[1][0]:>7,} events in {points[1][1]:.3f}s")
        print(f"    100K: {points[2][0]:>7,} events in {points[2][1]:.3f}s")

        assert slope_us > 0, "Slope should be positive"
        assert slope_us < 100, f"Per-event cost {slope_us:.1f} us seems too high"


class TestAdaptiveSampling:
    """Verify that adaptive sampling reduces locals capture rate."""

    def test_adaptive_sampling_verification(self, bench_record):
        """50K loop: <15% of LINE events should have locals (adaptive kicks in)."""
        script = "total = 0\nfor i in range(50000):\n    total += i\n"
        _, run_id, stats = bench_record(script)

        total_lines = db.fetchval(
            "SELECT COUNT(*) FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'",
            (str(run_id),))
        with_locals = db.fetchval(
            "SELECT COUNT(*) FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'line'"
            " AND locals_snapshot IS NOT NULL",
            (str(run_id),))

        ratio = with_locals / total_lines if total_lines else 0
        print(f"\n  Adaptive sampling: {with_locals:,}/{total_lines:,} "
              f"LINE events have locals ({ratio:.1%})")
        assert ratio < 0.15, f"Expected <15% sampling rate, got {ratio:.1%}"


class TestReturnOnlyContent:
    """Verify return-only optimization produces minimal snapshots."""

    def test_return_only_snapshot_content(self, bench_record):
        """Multi-line function: >80% of return frames should be return-only."""
        script = (
            "def f(x):\n"
            "    y = x + 1\n"
            "    return y\n"
            "\n"
            "for i in range(2000):\n"
            "    f(i)\n"
        )
        _, run_id, stats = bench_record(script)

        returns = db.fetchall(
            "SELECT locals_snapshot FROM executionframes"
            " WHERE run_id = ? AND frame_event = 'return'"
            " AND function_name = 'f'"
            " AND locals_snapshot IS NOT NULL AND locals_snapshot != ''",
            (str(run_id),))

        return_only = 0
        full = 0
        for r in returns:
            try:
                keys = set(json.loads(r.locals_snapshot).keys())
            except (json.JSONDecodeError, TypeError):
                continue
            if keys <= {'__return__'}:
                return_only += 1
            else:
                full += 1

        total = return_only + full
        ratio = return_only / total if total else 0
        print(f"\n  Return-only snapshots: {return_only}/{total} ({ratio:.0%})")
        print(f"    return-only: {return_only}, full: {full}")
        assert ratio > 0.80, f"Expected >80% return-only, got {ratio:.0%}"


class TestTypeReprThroughput:
    """Compare recording throughput across variable types."""

    @pytest.mark.parametrize("type_name,assignment", [
        ("int", "x = 42; y = 99"),
        ("float", "x = 3.14; y = 2.72"),
        ("str", "x = 'hello world'; y = 'foo bar'"),
        ("dict", "x = {'a': 1, 'b': 2}; y = {'c': 3}"),
    ])
    def test_type_repr(self, bench_record, type_name, assignment):
        """Record 3K calls with typed locals, report us/event."""
        script = (
            f"def work():\n"
            f"    {assignment}\n"
            f"    return 0\n"
            f"\n"
            f"for _ in range(3000):\n"
            f"    work()\n"
        )
        db_path, _, stats = bench_record(script)
        us = _us_per_event(stats)
        fc = stats.get('frame_count', 0)
        print(f"\n  type={type_name}: {fc:,} events, {us:.1f} us/event")
        assert stats.get('dropped_frames', 0) == 0


class TestInProcessOverhead:
    """Measure true recording overhead without subprocess startup."""

    def test_in_process_true_overhead(self, bench_record, tmp_path):
        """Compare runpy baseline vs recorded. Reports pure overhead."""
        script_text = (
            "def f(x):\n"
            "    return x + 1\n"
            "\n"
            "for i in range(5000):\n"
            "    f(i)\n"
        )
        script_file = tmp_path / "overhead_test.py"
        script_file.write_text(script_text)

        # Baseline: unrecorded runpy execution
        times = []
        for _ in range(5):
            old_argv = sys.argv[:]
            sys.argv = [str(script_file)]
            t0 = time.perf_counter()
            try:
                runpy.run_path(str(script_file), run_name='__main__')
            except BaseException:
                pass
            finally:
                sys.argv = old_argv
            times.append(time.perf_counter() - t0)
        baseline = sum(times) / len(times)

        # Recorded
        _, _, stats = bench_record(script_text)
        recorded = stats.get('elapsed_time', 0)

        overhead = recorded - baseline
        ratio = recorded / baseline if baseline > 0.0001 else float('inf')
        print(f"\n  Baseline (unrecorded): {baseline*1000:.1f}ms")
        print(f"  Recorded:              {recorded*1000:.1f}ms")
        print(f"  Pure overhead:         {overhead*1000:.1f}ms ({ratio:.1f}x)")

        assert overhead > 0, "Recording should add some overhead"


class TestSerializationCostDelta:
    """Estimate serialization cost by comparing workloads with/without locals."""

    def test_serialization_cost_delta(self, bench_record):
        """Compare 'for i in range(N): pass' vs 'x=0; for i in range(N): x+=i'.

        Both generate similar event counts, but the second has changing locals
        that trigger serialization. The delta approximates serialization cost.
        """
        n = 20000

        # Minimal locals (pass statement, no meaningful state)
        script_minimal = f"for i in range({n}):\n    pass\n"
        _, _, stats_min = bench_record(script_minimal)
        us_min = _us_per_event(stats_min)

        # Active locals (x changes every iteration, triggers serialization)
        script_active = f"x = 0\nfor i in range({n}):\n    x += i\n"
        _, _, stats_act = bench_record(script_active)
        us_act = _us_per_event(stats_act)

        delta = us_act - us_min
        print(f"\n  Minimal locals: {stats_min['frame_count']:,} events, {us_min:.1f} us/event")
        print(f"  Active locals:  {stats_act['frame_count']:,} events, {us_act:.1f} us/event")
        print(f"  Serialization delta: {delta:.1f} us/event")
