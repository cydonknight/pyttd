from datetime import datetime
import logging
import os
import sqlite3 as _sqlite3
import pyttd_native
from pyttd.config import PyttdConfig
from pyttd.models import storage
from pyttd.models.db import db
from pyttd.models import schema
from pyttd.tracing.constants import IGNORE_PATTERNS as INTERNAL_IGNORE

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(self, config: PyttdConfig):
        self.config = config
        self._recording = False
        self._run_id = None
        self._realpath_cache = {}
        self._db_path = None
        self._flush_count = 0
        self._size_warned = False
        self._resume_live_callback = None

    def start(self, db_path: str, script_path: str | None = None, attach: bool = False):
        """Initialize DB, create Runs record, set ignore patterns, install frame eval hook."""
        self._db_path = db_path
        storage.connect_to_db(db_path)
        storage.initialize_schema()

        # Drop secondary indexes during recording to speed up inserts.
        for sql in schema.SECONDARY_INDEX_DROP:
            try:
                db.execute_sql(sql)
            except Exception:
                pass

        # Clear stale checkpoint state from crashed sessions.
        try:
            for cp in db.fetchall(
                "SELECT checkpoint_id, child_pid FROM checkpoint WHERE is_alive = 1"
            ):
                if cp.child_pid:
                    try:
                        os.kill(cp.child_pid, 0)
                    except (OSError, ProcessLookupError):
                        db.execute(
                            "UPDATE checkpoint SET is_alive = 0, child_pid = NULL "
                            "WHERE checkpoint_id = ?", (cp.checkpoint_id,))
                        db.commit()
        except Exception:
            logger.debug("Could not clean stale checkpoints (DB may have been locked)")

        self._run_id = schema.create_run(script_path=script_path, is_attach=attach)

        # Initialize binary log for recording
        pyttd_native.binlog_open(db_path, self._run_id)

        # Auto-evict old runs if keep_runs is configured
        if self.config.keep_runs > 0:
            evicted = storage._evict_old_runs_internal(self.config.keep_runs)
            if evicted:
                logger.info("Evicted %d old run(s)", len(evicted))
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
        kwargs['db_path'] = db_path

        if self._resume_live_callback:
            kwargs['resume_live_callback'] = self._resume_live_callback

        if attach:
            kwargs['attach_mode'] = 1

        try:
            pyttd_native.start_recording(**kwargs)
        except Exception:
            db.execute("DELETE FROM runs WHERE run_id = ?", (self._run_id,))
            db.commit()
            self._run_id = None
            storage.close_db()
            raise
        os.environ['PYTTD_RECORDING'] = '1'
        if self.config.max_frames > 0:
            pyttd_native.set_max_frames(self.config.max_frames)
        if self.config.max_db_size_mb > 0:
            pyttd_native.binlog_set_size_limit(
                self.config.max_db_size_mb * 1024 * 1024)
        if self.config.checkpoint_memory_limit_mb > 0:
            pyttd_native.set_checkpoint_memory_limit(
                self.config.checkpoint_memory_limit_mb * 1024 * 1024)
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

        # Bulk-load binary log into SQLite
        try:
            pyttd_native.binlog_load(self._db_path)
        except Exception:
            logger.warning("Failed to load binlog into SQLite", exc_info=True)

        # Rebuild secondary indexes
        try:
            conn = _sqlite3.connect(self._db_path)
            for sql in schema.SECONDARY_INDEX_CREATE:
                conn.execute(sql)
            conn.commit()
            conn.close()
        except Exception:
            logger.debug("Failed to rebuild indexes")

        stats = pyttd_native.get_recording_stats()
        if self._run_id:
            schema.update_run(self._run_id,
                              timestamp_end=datetime.now().timestamp(),
                              total_frames=stats.get('frame_count', 0))
        return stats

    def kill_checkpoints(self):
        """Send DIE to all live checkpoint children. Called during shutdown."""
        pyttd_native.kill_all_checkpoints()
        if self._run_id:
            try:
                db.execute(
                    "UPDATE checkpoint SET is_alive = 0, child_pid = NULL "
                    "WHERE run_id = ?", (self._run_id,))
                db.commit()
            except Exception:
                pass

    def cleanup(self):
        """Close DB connection. Called during session shutdown."""
        self.kill_checkpoints()
        storage.close_db()

    @property
    def run_id(self):
        return self._run_id

    def _on_flush(self, events: list[dict]):
        """Called by C flush thread (with GIL held) to batch-insert frames.
        NOTE: In Phase 1, this is no longer called during normal recording
        (C-level SQLite flush handles it). Kept for backward compat / tests."""
        cache = self._realpath_cache
        for event in events:
            event['run_id'] = self._run_id
            fn = event.get('filename')
            if fn:
                resolved = cache.get(fn)
                if resolved is None:
                    resolved = os.path.realpath(fn)
                    cache[fn] = resolved
                event['filename'] = resolved
        try:
            storage.batch_insert(None, events)
        except Exception:
            logger.exception("batch_insert failed")

    def _on_io_event(self, event: dict):
        """Called synchronously by C I/O hooks (with GIL held) to insert a single IOEvent."""
        schema.create_io_event(
            run_id=self._run_id,
            sequence_no=event['sequence_no'],
            io_sequence=event['io_sequence'],
            function_name=event['function_name'],
            return_value=event['return_value'],
        )

    def _load_io_events_for_replay(self, after_seq: int) -> list[dict]:
        """Called by checkpoint child to pre-load IOEvents for deterministic fast-forward."""
        return db.fetchdicts(
            "SELECT function_name, return_value FROM ioevent "
            "WHERE run_id = ? AND sequence_no > ? "
            "ORDER BY sequence_no, io_sequence",
            (self._run_id, after_seq))

    def _on_checkpoint(self, child_pid: int, sequence_no: int):
        """Called by C eval hook (with GIL held) after successful fork().
        Non-fatal — exception is logged and cleared by C code."""
        schema.create_checkpoint(self._run_id, sequence_no, child_pid)
