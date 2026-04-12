"""Trace diff — find the earliest divergence between two recording runs.

Given two runs (typically one passing, one failing), aligns their
line-event sequences in lockstep and reports the first point where
execution diverges, either in control flow or data.
"""
import json
from dataclasses import dataclass, field


@dataclass
class DiffResult:
    """Result of comparing two runs."""
    kind: str  # "identical", "control_flow", "data", "length_mismatch"
    seq_a: int | None = None
    seq_b: int | None = None
    frame_a: dict | None = None
    frame_b: dict | None = None
    diverging_vars: list = field(default_factory=list)
    context_before: list = field(default_factory=list)
    message: str = ""


def _iter_line_events(db, run_id: str):
    """Yield line events for a run in sequence order."""
    cursor = db.iterate(
        "SELECT sequence_no, function_name, filename, line_no,"
        " locals_snapshot, call_depth, thread_id"
        " FROM executionframes"
        " WHERE run_id = ? AND frame_event = 'line'"
        " ORDER BY sequence_no",
        (str(run_id),)
    )
    for row in cursor:
        yield row


def _signature(row) -> tuple:
    """Control-flow signature: (function_name, line_no)."""
    return (row.function_name, row.line_no)


def _parse_locals(row) -> dict:
    """Parse locals_snapshot JSON, returning {} on failure."""
    raw = getattr(row, "locals_snapshot", None)
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
        return {}
    except (ValueError, TypeError):
        return {}


import re

# Matches memory addresses like "0x10abc1234" in repr strings
_ADDR_RE = re.compile(r' at 0x[0-9a-fA-F]+')

# Prefixes indicating ephemeral objects whose identity varies between runs
_EPHEMERAL_PREFIXES = ("<function ", "<module ", "<class ", "<built-in ")


def _flatten_value(v):
    """Flatten a structured local value for comparison.

    Structured values (with __type__, __repr__, __children__) are reduced
    to their __repr__ for diffing. Plain values pass through.
    """
    if isinstance(v, dict) and "__repr__" in v:
        return v["__repr__"]
    return v


def _normalize_for_comparison(v) -> str:
    """Normalize a value for comparison by stripping memory addresses."""
    s = str(v)
    return _ADDR_RE.sub(" at 0x...", s)


def _is_ephemeral_value(v) -> bool:
    """Return True if a value is an ephemeral object (function, module, etc.)."""
    s = str(v)
    return s.startswith(_EPHEMERAL_PREFIXES)


def _compare_locals(a_json: dict, b_json: dict, ignore_vars: set) -> list:
    """Compare two locals dicts; return list of (name, val_a, val_b) for diffs."""
    all_keys = set(a_json.keys()) | set(b_json.keys())
    diffs = []
    for k in sorted(all_keys):
        if k in ignore_vars:
            continue
        if k.startswith("__") and k.endswith("__"):
            continue  # Skip dunders for cleaner output
        va = _flatten_value(a_json.get(k, "<absent>"))
        vb = _flatten_value(b_json.get(k, "<absent>"))
        # Skip ephemeral objects whose identity varies between runs
        if _is_ephemeral_value(va) and _is_ephemeral_value(vb):
            continue
        # Normalize memory addresses before comparing
        if _normalize_for_comparison(va) != _normalize_for_comparison(vb):
            diffs.append((k, va, vb))
    return diffs


def _try_resync(iter_a, iter_b, a_current, b_current, lookahead: int = 20):
    """Attempt single-step resync after a control-flow mismatch.

    Looks ahead up to `lookahead` events in each trace to find a point
    where both traces' signatures match again. Returns (events_a, events_b,
    resync_a, resync_b) where events_a/b are the consumed lookahead events
    and resync_a/b are the matching events, or (None, None, None, None)
    if no resync found.
    """
    sig_b = _signature(b_current)
    sig_a = _signature(a_current)

    # Try advancing A to match B's current position
    buf_a = []
    for row_a in iter_a:
        buf_a.append(row_a)
        if _signature(row_a) == sig_b:
            return buf_a, [], row_a, b_current
        if len(buf_a) >= lookahead:
            break

    # Try advancing B to match A's current position
    buf_b = []
    for row_b in iter_b:
        buf_b.append(row_b)
        if _signature(row_b) == sig_a:
            return [], buf_b, a_current, row_b
        if len(buf_b) >= lookahead:
            break

    return None, None, None, None


def align_and_diff(db, run_id_a: str, run_id_b: str,
                   ignore_vars: set | None = None,
                   context: int = 3) -> DiffResult:
    """Compare two runs and find the earliest divergence.

    Args:
        db: Database instance (already connected).
        run_id_a: First run ID.
        run_id_b: Second run ID.
        ignore_vars: Variable names to skip when comparing locals.
        context: Number of matching frames to keep as context before divergence.

    Returns:
        DiffResult describing the first divergence found.
    """
    if ignore_vars is None:
        ignore_vars = set()

    iter_a = _iter_line_events(db, run_id_a)
    iter_b = _iter_line_events(db, run_id_b)

    context_buf = []  # ring buffer of last N matching frames

    while True:
        row_a = next(iter_a, None)
        row_b = next(iter_b, None)

        # Both exhausted — identical
        if row_a is None and row_b is None:
            return DiffResult(kind="identical", message="Runs are identical.")

        # One shorter than the other
        if row_a is None:
            return DiffResult(
                kind="length_mismatch",
                seq_b=row_b.sequence_no,
                frame_b=_row_to_dict(row_b),
                context_before=list(context_buf),
                message=f"Run A ended at frame {context_buf[-1]['seq_a'] if context_buf else '?'}; "
                        f"Run B continues at seq {row_b.sequence_no}.",
            )
        if row_b is None:
            return DiffResult(
                kind="length_mismatch",
                seq_a=row_a.sequence_no,
                frame_a=_row_to_dict(row_a),
                context_before=list(context_buf),
                message=f"Run B ended at frame {context_buf[-1]['seq_b'] if context_buf else '?'}; "
                        f"Run A continues at seq {row_a.sequence_no}.",
            )

        sig_a = _signature(row_a)
        sig_b = _signature(row_b)

        if sig_a != sig_b:
            # Control-flow divergence — try resync
            skip_a, skip_b, sync_a, sync_b = _try_resync(
                iter_a, iter_b, row_a, row_b
            )
            return DiffResult(
                kind="control_flow",
                seq_a=row_a.sequence_no,
                seq_b=row_b.sequence_no,
                frame_a=_row_to_dict(row_a),
                frame_b=_row_to_dict(row_b),
                context_before=list(context_buf),
                message=(
                    f"Control flow diverges: "
                    f"A={sig_a[0]}:{sig_a[1]}, B={sig_b[0]}:{sig_b[1]}"
                ),
            )

        # Signatures match — compare locals
        locals_a = _parse_locals(row_a)
        locals_b = _parse_locals(row_b)
        diffs = _compare_locals(locals_a, locals_b, ignore_vars)

        if diffs:
            return DiffResult(
                kind="data",
                seq_a=row_a.sequence_no,
                seq_b=row_b.sequence_no,
                frame_a=_row_to_dict(row_a),
                frame_b=_row_to_dict(row_b),
                diverging_vars=diffs,
                context_before=list(context_buf),
                message=f"Data divergence at {sig_a[0]}:{sig_a[1]}",
            )

        # Match — add to context buffer
        ctx_entry = {
            "seq_a": row_a.sequence_no,
            "seq_b": row_b.sequence_no,
            "function": row_a.function_name,
            "line": row_a.line_no,
        }
        context_buf.append(ctx_entry)
        if len(context_buf) > context:
            context_buf.pop(0)


def _row_to_dict(row) -> dict:
    """Convert a DB row to a plain dict for the result."""
    return {
        "seq": row.sequence_no,
        "function": row.function_name,
        "filename": row.filename,
        "line": row.line_no,
    }


# ---- Formatters ----

def format_diff_text(result: DiffResult, db_path: str = "") -> str:
    """Render a DiffResult as human-readable text."""
    lines = []

    if result.kind == "identical":
        lines.append("No divergence found — runs are identical.")
        return "\n".join(lines)

    if result.kind == "length_mismatch":
        lines.append(f"Length mismatch: {result.message}")
        if result.context_before:
            lines.append("\nLast matching frames:")
            for ctx in result.context_before:
                lines.append(f"  A#{ctx['seq_a']:>6}  B#{ctx['seq_b']:>6}  "
                             f"{ctx['function']}:{ctx['line']}")
        return "\n".join(lines)

    # control_flow or data
    lines.append(f"Divergence at frame #{result.seq_a} (A) / #{result.seq_b} (B):")

    if result.frame_a and result.frame_b:
        import os
        fn_a = os.path.basename(result.frame_a.get("filename", "?"))
        fn_b = os.path.basename(result.frame_b.get("filename", "?"))
        lines.append(
            f"  A: {result.frame_a['function']}  at {fn_a}:{result.frame_a['line']}"
        )
        lines.append(
            f"  B: {result.frame_b['function']}  at {fn_b}:{result.frame_b['line']}"
        )

    if result.kind == "data" and result.diverging_vars:
        lines.append("  Diverging variables:")
        for name, va, vb in result.diverging_vars:
            lines.append(f"    {name}:")
            lines.append(f"      A: {va}")
            lines.append(f"      B: {vb}")

    if result.context_before:
        lines.append("\n  Context (matching frames before divergence):")
        for ctx in result.context_before:
            lines.append(f"    A#{ctx['seq_a']:>6}  B#{ctx['seq_b']:>6}  "
                         f"{ctx['function']}:{ctx['line']}")

    if result.seq_b and db_path:
        lines.append(
            f"\n  Replay B: pyttd replay --goto-frame {result.seq_b} --db {db_path}"
        )

    return "\n".join(lines)


def format_diff_json(result: DiffResult) -> str:
    """Render a DiffResult as JSON."""
    d = {
        "kind": result.kind,
        "seq_a": result.seq_a,
        "seq_b": result.seq_b,
        "frame_a": result.frame_a,
        "frame_b": result.frame_b,
        "diverging_vars": [
            {"name": n, "value_a": str(a), "value_b": str(b)}
            for n, a, b in result.diverging_vars
        ],
        "message": result.message,
    }
    return json.dumps(d, indent=2)
