from datetime import datetime
import os
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.frames import ExecutionFrames
from pyttd.models.runs import Runs
from pyttd.models.checkpoints import Checkpoint
from pyttd.models.io_events import IOEvent
from pyttd.tracing.constants import IGNORE_PATTERNS as INTERNAL_IGNORE

class Recorder:
    def __init__(self, config: PyttdConfig):
        self.config = config
        self._recording = False
        self._run = None
        self._realpath_cache = {}

    def start(self, db_path: str, script_path: str | None = None):
        """Initialize DB, create Runs record, set ignore patterns, install frame eval hook."""
        storage.connect_to_db(db_path)
        storage.initialize_schema([Runs, ExecutionFrames, Checkpoint, IOEvent])
        # Clear stale checkpoint state from any previous crashed session
        Checkpoint.update(is_alive=False, child_pid=None).execute()
        self._run = Runs.create(script_path=script_path)
        all_ignore = list(INTERNAL_IGNORE) + list(self.config.ignore_patterns)
        pyttd_native.set_ignore_patterns(all_ignore)

        if self.config.redact_secrets and self.config.secret_patterns:
            pyttd_native.set_secret_patterns(self.config.secret_patterns)
        else:
            pyttd_native.set_secret_patterns([])

        pyttd_native.set_include_patterns(self.config.include_functions)
        pyttd_native.set_file_include_patterns(self.config.include_files)
        pyttd_native.set_exclude_patterns(
            self.config.exclude_functions,
            self.config.exclude_files,
        )

        kwargs = dict(
            flush_callback=self._on_flush,
            buffer_size=self.config.ring_buffer_size,
            flush_interval_ms=self.config.flush_interval_ms,
        )
        if self.config.checkpoint_interval > 0:
            kwargs['checkpoint_callback'] = self._on_checkpoint
            kwargs['checkpoint_interval'] = self.config.checkpoint_interval

        kwargs['io_flush_callback'] = self._on_io_event
        kwargs['io_replay_loader'] = self._load_io_events_for_replay

        try:
            pyttd_native.start_recording(**kwargs)
        except Exception:
            self._run.delete_instance()
            self._run = None
            storage.close_db()
            raise
        # C setenv() updates the C environ (for subprocesses) but not Python's
        # os.environ dict (cached at import time). Update it explicitly so user
        # scripts can see it via os.environ.get('PYTTD_RECORDING').
        os.environ['PYTTD_RECORDING'] = '1'
        # Set max_frames AFTER start_recording (which resets it to 0)
        if self.config.max_frames > 0:
            pyttd_native.set_max_frames(self.config.max_frames)
        self._recording = True

    def stop(self) -> dict:
        """Stop recording. Does NOT close DB or kill checkpoints —
        they're needed for replay. Call kill_checkpoints() + cleanup()
        during session shutdown."""
        if not self._recording:
            return {}
        pyttd_native.stop_recording()
        self._recording = False
        os.environ.pop('PYTTD_RECORDING', None)
        stats = pyttd_native.get_recording_stats()
        if self._run:
            self._run.timestamp_end = datetime.now().timestamp()
            self._run.total_frames = stats.get('frame_count', 0)
            self._run.save()
        return stats

    def kill_checkpoints(self):
        """Send DIE to all live checkpoint children. Called during shutdown."""
        pyttd_native.kill_all_checkpoints()
        if self._run:
            Checkpoint.update(is_alive=False, child_pid=None).where(
                Checkpoint.run_id == self._run.run_id
            ).execute()

    def cleanup(self):
        """Close DB connection. Called during session shutdown."""
        self.kill_checkpoints()
        storage.close_db()

    @property
    def run_id(self):
        return self._run.run_id if self._run else None

    def _on_flush(self, events: list[dict]):
        """Called by C flush thread (with GIL held) to batch-insert frames."""
        cache = self._realpath_cache
        for event in events:
            event['run_id'] = self._run.run_id
            fn = event.get('filename')
            if fn:
                resolved = cache.get(fn)
                if resolved is None:
                    resolved = os.path.realpath(fn)
                    cache[fn] = resolved
                event['filename'] = resolved
        try:
            storage.batch_insert(ExecutionFrames, events)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("batch_insert failed")

    def _on_io_event(self, event: dict):
        """Called synchronously by C I/O hooks (with GIL held) to insert a single IOEvent."""
        event['run_id'] = self._run.run_id
        IOEvent.create(**event)

    def _load_io_events_for_replay(self, after_seq: int) -> list[dict]:
        """Called by checkpoint child to pre-load IOEvents for deterministic fast-forward."""
        return list(IOEvent.select(IOEvent.function_name, IOEvent.return_value)
            .where((IOEvent.run_id == self._run.run_id) & (IOEvent.sequence_no > after_seq))
            .order_by(IOEvent.sequence_no, IOEvent.io_sequence)
            .dicts())

    def _on_checkpoint(self, child_pid: int, sequence_no: int):
        """Called by C eval hook (with GIL held) after successful fork().
        Non-fatal — exception is logged and cleared by C code."""
        Checkpoint.create(run_id=self._run.run_id, sequence_no=sequence_no,
                          child_pid=child_pid)
