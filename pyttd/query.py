from uuid import UUID

from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models import storage


def get_last_run(db_path: str) -> Runs:
    storage.connect_to_db(db_path)
    try:
        run = Runs.select().order_by(Runs.timestamp_start.desc()).first()
    except Exception:
        raise ValueError("No runs found in database (table may not exist)")
    if run is None:
        raise ValueError("No runs found in database")
    return run


def get_all_runs(db_path: str) -> list[Runs]:
    """Return all runs sorted by timestamp_start desc."""
    storage.connect_to_db(db_path)
    storage.initialize_schema([Runs, ExecutionFrames])
    return list(Runs.select().order_by(Runs.timestamp_start.desc()))


def get_run_by_id(db_path: str, run_id_str: str) -> Runs:
    """Find a run by exact UUID or prefix match.

    Raises ValueError on 0 or >1 matches.
    """
    storage.connect_to_db(db_path)
    storage.initialize_schema([Runs, ExecutionFrames])

    # Try exact UUID match first
    try:
        exact_uuid = UUID(run_id_str)
        run = Runs.get_or_none(Runs.run_id == exact_uuid)
        if run is not None:
            return run
    except ValueError:
        pass

    # Prefix match — cast run_id to text and LIKE
    from peewee import SQL
    matches = list(Runs.select().where(
        SQL('CAST("run_id" AS TEXT) LIKE ?', [run_id_str + '%'])
    ))

    if len(matches) == 0:
        raise ValueError(f"No run found matching '{run_id_str}'")
    if len(matches) > 1:
        ids = [str(m.run_id)[:12] for m in matches]
        raise ValueError(f"Ambiguous prefix '{run_id_str}' matches {len(matches)} runs: {', '.join(ids)}... Use a longer prefix.")
    return matches[0]


def get_frames(run_id, limit=50, offset=0) -> list[ExecutionFrames]:
    return list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no)
        .offset(offset).limit(limit))


def get_frame_at_seq(run_id, seq) -> ExecutionFrames | None:
    return ExecutionFrames.get_or_none(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.sequence_no == seq))


def get_line_code(filename: str, line_no: int) -> str:
    """Lazily fetch source line via linecache (not stored in DB)."""
    import linecache
    return linecache.getline(filename, line_no).strip()
