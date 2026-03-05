from datetime import datetime
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.tracing.constants import IGNORE_PATTERNS as INTERNAL_IGNORE

class Recorder:
    def __init__(self, config: PyttdConfig):
        self.config = config
        self._recording = False
        self._run = None

    def start(self, db_path: str, script_path: str | None = None):
        """Initialize DB, create Runs record, set ignore patterns, install frame eval hook."""
        storage.connect_to_db(db_path)
        storage.initialize_schema([Runs, ExecutionFrames])
        self._run = Runs.create(script_path=script_path)
        all_ignore = list(INTERNAL_IGNORE) + list(self.config.ignore_patterns)
        pyttd_native.set_ignore_patterns(all_ignore)
        pyttd_native.start_recording(
            flush_callback=self._on_flush,
            buffer_size=self.config.ring_buffer_size,
            flush_interval_ms=self.config.flush_interval_ms,
        )
        self._recording = True

    def stop(self) -> dict:
        """Stop recording, flush remaining, update Runs record, return stats.
        Does NOT close the DB — it's needed for replay mode after recording.
        Call cleanup() during session shutdown to close the DB."""
        pyttd_native.stop_recording()
        self._recording = False
        stats = pyttd_native.get_recording_stats()
        if self._run:
            self._run.timestamp_end = datetime.now().timestamp()
            self._run.total_frames = stats.get('frame_count', 0)
            self._run.save()
        return stats

    def cleanup(self):
        """Close DB connection. Called during session shutdown (disconnect),
        NOT after recording stops (DB is needed for replay)."""
        storage.close_db()

    @property
    def run_id(self):
        return self._run.run_id if self._run else None

    def _on_flush(self, events: list[dict]):
        """Called by C flush thread (with GIL held) to batch-insert frames."""
        for event in events:
            event['run_id'] = self._run.run_id
        try:
            storage.batch_insert(ExecutionFrames, events)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("batch_insert failed")
            raise
