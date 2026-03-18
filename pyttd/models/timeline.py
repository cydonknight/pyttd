"""Timeline summary queries for the scrubber webview.

Aggregates ExecutionFrames data into buckets for canvas rendering.
This is a query module, not a Peewee model.
"""
import operator
from functools import reduce

from peewee import fn, SQL
from pyttd.models.frames import ExecutionFrames


def get_timeline_summary(run_id, start_seq, end_seq, bucket_count=500,
                         breakpoints=None) -> list[dict]:
    """Return downsampled timeline data for rendering.

    Each bucket: {startSeq, endSeq, maxCallDepth, hasException,
                  hasBreakpoint, dominantFunction}
    """
    total_range = end_seq - start_seq
    if total_range <= 0 or bucket_count <= 0:
        return []
    bucket_size = max(1, total_range // bucket_count)

    bucket_expr = SQL(
        '(("sequence_no" - ?) / ?)', [start_seq, bucket_size]
    )

    rows = (ExecutionFrames.select(
                fn.MIN(ExecutionFrames.sequence_no).alias('start_seq'),
                fn.MAX(ExecutionFrames.sequence_no).alias('end_seq'),
                fn.MAX(ExecutionFrames.call_depth).alias('max_depth'),
                fn.SUM(SQL("CASE WHEN frame_event IN ('exception', 'exception_unwind') "
                           "THEN 1 ELSE 0 END")).alias('exc_count'),
                ExecutionFrames.function_name,
            )
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.sequence_no >= start_seq) &
                   (ExecutionFrames.sequence_no <= end_seq))
            .group_by(bucket_expr)
            .order_by(bucket_expr)
            .dicts())

    # Build breakpoint lookup set for O(1) matching
    bp_set = set()
    if breakpoints:
        bp_set = {(bp['file'], bp['line']) for bp in breakpoints
                  if 'file' in bp and 'line' in bp}

    # Single-query optimization: find all breakpoint-matching seqs in range
    bp_buckets = set()
    if bp_set:
        conditions = [
            ((ExecutionFrames.filename == f) & (ExecutionFrames.line_no == l))
            for f, l in bp_set
        ]
        bp_hits = (
            row.sequence_no for row in
            ExecutionFrames.select(ExecutionFrames.sequence_no)
            .where((ExecutionFrames.run_id == run_id) &
                   (ExecutionFrames.sequence_no >= start_seq) &
                   (ExecutionFrames.sequence_no <= end_seq) &
                   (ExecutionFrames.frame_event == 'line') &
                   reduce(operator.or_, conditions))
        )
        bp_buckets = {(seq - start_seq) // bucket_size for seq in bp_hits}

    buckets = []
    for row in rows:
        bucket_idx = (row['start_seq'] - start_seq) // bucket_size
        buckets.append({
            'startSeq': row['start_seq'],
            'endSeq': row['end_seq'],
            'maxCallDepth': row['max_depth'] or 0,
            'hasException': (row['exc_count'] or 0) > 0,
            'hasBreakpoint': bucket_idx in bp_buckets,
            'dominantFunction': row['function_name'] or '',
        })

    return buckets
