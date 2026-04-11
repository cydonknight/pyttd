from uuid import UUID

from pyttd.models.db import db
from pyttd.models import storage


def get_last_run(db_path: str):
    """Return the most recent run, or raise ValueError."""
    storage.connect_to_db(db_path)
    try:
        run = db.fetchone(
            "SELECT * FROM runs ORDER BY timestamp_start DESC LIMIT 1"
        )
    except Exception:
        raise ValueError("No runs found in database (table may not exist)")
    if run is None:
        raise ValueError("No runs found in database")
    return run


def get_all_runs(db_path: str) -> list:
    """Return all runs sorted by timestamp_start desc."""
    storage.connect_to_db(db_path)
    storage.initialize_schema()
    return db.fetchall(
        "SELECT * FROM runs ORDER BY timestamp_start DESC"
    )


def get_run_by_id(db_path: str, run_id_str: str):
    """Find a run by exact UUID or prefix match.

    Raises ValueError on 0 or >1 matches.
    """
    storage.connect_to_db(db_path)
    storage.initialize_schema()

    # Try exact UUID match first
    try:
        exact_uuid = UUID(run_id_str)
        run = db.fetchone(
            "SELECT * FROM runs WHERE run_id = ?",
            (str(exact_uuid),)
        )
        if run is not None:
            return run
    except ValueError:
        pass

    # Also try the hex (no-dashes) form directly
    run = db.fetchone(
        "SELECT * FROM runs WHERE run_id = ?",
        (run_id_str,)
    )
    if run is not None:
        return run

    # Prefix match — cast run_id to text and LIKE.
    # Escape LIKE wildcards (%, _) so a user passing literal '%' or '_' does
    # not match unexpected runs. '\' is the escape character.
    escaped = run_id_str.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    matches = db.fetchall(
        "SELECT * FROM runs WHERE CAST(run_id AS TEXT) LIKE ? ESCAPE '\\'",
        (escaped + '%',)
    )

    if len(matches) == 0:
        raise ValueError(f"No run found matching '{run_id_str}'")
    if len(matches) > 1:
        ids = [str(m.run_id)[:12] for m in matches]
        raise ValueError(
            f"Ambiguous prefix '{run_id_str}' matches {len(matches)} runs: "
            f"{', '.join(ids)}... Use a longer prefix."
        )
    return matches[0]


def get_frames(run_id, limit=50, offset=0) -> list:
    """Return paginated frames for a run, ordered by sequence_no."""
    return db.fetchall(
        "SELECT * FROM executionframes "
        "WHERE run_id = ? ORDER BY sequence_no LIMIT ? OFFSET ?",
        (str(run_id), limit, offset)
    )


def get_frame_at_seq(run_id, seq):
    """Return the frame at a specific sequence number, or None."""
    return db.fetchone(
        "SELECT * FROM executionframes "
        "WHERE run_id = ? AND sequence_no = ?",
        (str(run_id), seq)
    )


def get_line_code(filename: str, line_no: int) -> str:
    """Lazily fetch source line via linecache (not stored in DB)."""
    import linecache
    return linecache.getline(filename, line_no).strip()


def search_frames(run_id, pattern: str, limit: int = 50) -> list:
    """Search frames by substring match on function_name or filename."""
    return db.fetchall(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND"
        " (function_name LIKE '%' || ? || '%' OR filename LIKE '%' || ? || '%')"
        " ORDER BY sequence_no LIMIT ?",
        (str(run_id), pattern, pattern, limit)
    )


def get_frames_by_thread(run_id, thread_id: int, limit: int = 50) -> list:
    """Return frames for a specific thread, ordered by sequence_no."""
    return db.fetchall(
        "SELECT * FROM executionframes"
        " WHERE run_id = ? AND thread_id = ?"
        " ORDER BY sequence_no LIMIT ?",
        (str(run_id), thread_id, limit)
    )
