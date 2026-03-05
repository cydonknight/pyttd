from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models import storage

def get_last_run(db_path: str) -> Runs:
    storage.connect_to_db(db_path)
    return Runs.select().order_by(Runs.timestamp_start.desc()).get()

def get_frames(run_id, limit=50, offset=0) -> list[ExecutionFrames]:
    return list(ExecutionFrames.select()
        .where(ExecutionFrames.run_id == run_id)
        .order_by(ExecutionFrames.sequence_no)
        .offset(offset).limit(limit))

def get_frame_at_seq(run_id, seq) -> ExecutionFrames:
    return ExecutionFrames.get(
        (ExecutionFrames.run_id == run_id) &
        (ExecutionFrames.sequence_no == seq))

def get_line_code(filename: str, line_no: int) -> str:
    """Lazily fetch source line via linecache (not stored in DB)."""
    import linecache
    return linecache.getline(filename, line_no).strip()
