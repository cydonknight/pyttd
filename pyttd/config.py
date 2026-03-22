from dataclasses import dataclass, field

_DEFAULT_SECRET_PATTERNS = [
    'password', 'secret', 'token', 'api_key', 'apikey',
    'auth', 'credential', 'private_key',
]

@dataclass
class PyttdConfig:
    checkpoint_interval: int = 1000
    ring_buffer_size: int = 65536
    flush_interval_ms: int = 10
    ignore_patterns: list[str] = field(default_factory=list)
    db_path: str | None = None
    redact_secrets: bool = True
    secret_patterns: list[str] = field(default_factory=lambda: list(_DEFAULT_SECRET_PATTERNS))
    include_functions: list[str] = field(default_factory=list)
    max_frames: int = 0        # 0 = unlimited
    max_memory_mb: int = 0     # 0 = unlimited
    max_db_size_mb: int = 0    # 0 = unlimited; warn when DB exceeds this
    keep_runs: int = 0         # 0 = keep all; N = keep only last N runs
    include_files: list[str] = field(default_factory=list)
    exclude_functions: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.checkpoint_interval < 0:
            raise ValueError("checkpoint_interval must be >= 0")
        if self.ring_buffer_size != 0 and self.ring_buffer_size < 64:
            raise ValueError("ring_buffer_size must be 0 (default) or >= 64")
        if self.flush_interval_ms <= 0:
            raise ValueError("flush_interval_ms must be > 0")
        if self.max_frames < 0:
            raise ValueError("max_frames must be >= 0")
        if self.max_memory_mb < 0:
            raise ValueError("max_memory_mb must be >= 0")
        if self.max_db_size_mb < 0:
            raise ValueError("max_db_size_mb must be >= 0")
        if self.keep_runs < 0:
            raise ValueError("keep_runs must be >= 0")
