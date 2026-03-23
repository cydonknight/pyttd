"""Timeline summary queries for the scrubber webview.

Aggregates ExecutionFrames data into buckets for canvas rendering.
This is a query module, not a model.
"""
from pyttd.models.db import db


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

    # Coerce run_id to str for raw sqlite3 binding (may be UUID)
    rid = str(run_id)

    rows = db.fetchdicts(
        "SELECT"
        "  MIN(sequence_no) AS start_seq,"
        "  MAX(sequence_no) AS end_seq,"
        "  MAX(call_depth) AS max_depth,"
        "  SUM(CASE WHEN frame_event IN ('exception', 'exception_unwind')"
        "       THEN 1 ELSE 0 END) AS exc_count,"
        "  function_name"
        " FROM executionframes"
        " WHERE run_id = ? AND sequence_no >= ? AND sequence_no <= ?"
        " GROUP BY ((sequence_no - ?) / ?)"
        " ORDER BY ((sequence_no - ?) / ?)",
        (rid, start_seq, end_seq, start_seq, bucket_size,
         start_seq, bucket_size),
    )

    # Build breakpoint lookup set for O(1) matching
    bp_set = set()
    if breakpoints:
        bp_set = {(bp['file'], bp['line']) for bp in breakpoints
                  if 'file' in bp and 'line' in bp}

    # Single-query optimization: find all breakpoint-matching seqs in range
    bp_buckets = set()
    if bp_set:
        bp_list = list(bp_set)
        conditions = " OR ".join(
            "(filename = ? AND line_no = ?)" for _ in bp_list
        )
        params = [rid, start_seq, end_seq]
        for f, l in bp_list:
            params.append(f)
            params.append(l)

        bp_hits = db.fetchall(
            "SELECT sequence_no FROM executionframes"
            " WHERE run_id = ? AND sequence_no >= ? AND sequence_no <= ?"
            "   AND frame_event = 'line'"
            "   AND (" + conditions + ")",
            params,
        )
        bp_buckets = {(row.sequence_no - start_seq) // bucket_size
                      for row in bp_hits}

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
