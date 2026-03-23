import json
import logging

import pyttd_native
from pyttd.models.db import db

logger = logging.getLogger(__name__)

_FRAME_BY_SEQ_SQL = """
    SELECT filename, line_no, function_name, call_depth, locals_snapshot
    FROM executionframes
    WHERE run_id = ? AND sequence_no = ?
"""


class ReplayController:
    def goto_frame(self, run_id, target_seq) -> dict:
        """Cold navigation: restore checkpoint, fast-forward, return frame state.
        Falls back to warm-only navigation if no usable checkpoint.
        Cold result merges DB metadata with child's live locals."""
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
        frame = db.fetchone(_FRAME_BY_SEQ_SQL, (run_id, target_seq))
        if frame is None:
            return {"error": "frame_not_found", "target_seq": target_seq}
        try:
            locals_data = json.loads(frame.locals_snapshot) if frame.locals_snapshot else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                "Failed to parse locals at seq %d: %s", target_seq, e)
            locals_data = {}
        return {
            "seq": target_seq,
            "file": frame.filename,
            "line": frame.line_no,
            "function_name": frame.function_name,
            "call_depth": frame.call_depth,
            "locals": locals_data,
            "warm_only": True,
        }
