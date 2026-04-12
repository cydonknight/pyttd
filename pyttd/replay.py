import json
import logging

import pyttd_native
from pyttd.models.db import db

logger = logging.getLogger(__name__)

_FRAME_BY_SEQ_SQL = """
    SELECT filename, line_no, function_name, call_depth, locals_snapshot,
           frame_event, thread_id
    FROM executionframes
    WHERE run_id = ? AND sequence_no = ?
"""


class ReplayController:
    def goto_frame(self, run_id, target_seq) -> dict:
        """Cold navigation: restore checkpoint, fast-forward, return frame state.
        Falls back to warm-only navigation if no usable checkpoint.
        Cold result merges DB metadata with child's live locals."""
        # Issue 6: in attach mode the synthesized-stack prefix has no
        # corresponding interpreter state to fork into. Refuse cold jumps
        # before the safe boundary and serve them from SQLite directly.
        try:
            run_row = db.fetchone(
                "SELECT attach_safe_seq FROM runs WHERE run_id = ?",
                (str(run_id),))
        except Exception:
            run_row = None
        attach_safe = getattr(run_row, 'attach_safe_seq', None) if run_row else None
        if attach_safe and target_seq < attach_safe:
            return self.warm_goto_frame(run_id, target_seq)

        try:
            cold_result = pyttd_native.restore_checkpoint(target_seq)
        except Exception:
            return self.warm_goto_frame(run_id, target_seq)

        if cold_result.get("status") == "error":
            return self.warm_goto_frame(run_id, target_seq)

        # Merge: metadata from DB (canonical), locals from child (live objects)
        db_frame = db.fetchone(_FRAME_BY_SEQ_SQL, (run_id, target_seq))
        if db_frame:
            return {
                "seq": target_seq,
                "file": db_frame.filename,
                "line": db_frame.line_no,
                "function_name": db_frame.function_name,
                "call_depth": db_frame.call_depth,
                "locals": cold_result.get("locals", {}),
            }
        # DB frame not found (shouldn't happen for valid target_seq)
        return cold_result

    def warm_goto_frame(self, run_id, target_seq) -> dict:
        """Warm-only navigation: read frame data directly from SQLite
        (repr snapshots only, no live objects)."""
        if target_seq < 0:
            return {"error": f"Frame number must be non-negative (got {target_seq})",
                    "target_seq": target_seq}
        frame = db.fetchone(_FRAME_BY_SEQ_SQL, (run_id, target_seq))
        if frame is None:
            # Try to give a helpful range by looking up the last sequence
            last = db.fetchone(
                "SELECT sequence_no FROM executionframes"
                " WHERE run_id = ? ORDER BY sequence_no DESC LIMIT 1",
                (str(run_id),))
            if last is not None:
                return {"error": f"Frame {target_seq} not found (valid range: 0-{last.sequence_no})",
                        "target_seq": target_seq}
            return {"error": f"Frame {target_seq} not found (no frames in recording)",
                    "target_seq": target_seq}
        try:
            locals_data = json.loads(frame.locals_snapshot) if frame.locals_snapshot else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "Failed to parse locals at seq %d: %s", target_seq, e)
            locals_data = {}
        # Merge return-only snapshots with nearest full locals
        if (frame.frame_event == 'return'
                and set(locals_data.keys()) <= {'__return__'}):
            fallback = db.fetchone(
                "SELECT locals_snapshot FROM executionframes"
                " WHERE run_id = ? AND locals_snapshot IS NOT NULL"
                " AND locals_snapshot != ''"
                " AND call_depth = ? AND thread_id = ?"
                " AND function_name = ? AND filename = ?"
                " AND sequence_no < ?"
                " ORDER BY sequence_no DESC LIMIT 1",
                (run_id, frame.call_depth, frame.thread_id,
                 frame.function_name, frame.filename, target_seq))
            if fallback and fallback.locals_snapshot:
                try:
                    full_locals = json.loads(fallback.locals_snapshot)
                    full_locals.update(locals_data)
                    locals_data = full_locals
                except (json.JSONDecodeError, TypeError):
                    pass
        return {
            "seq": target_seq,
            "file": frame.filename,
            "line": frame.line_no,
            "function_name": frame.function_name,
            "call_depth": frame.call_depth,
            "locals": locals_data,
            "warm_only": True,
        }
