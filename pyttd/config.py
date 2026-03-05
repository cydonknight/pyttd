from dataclasses import dataclass, field

@dataclass
class PyttdConfig:
    checkpoint_interval: int = 1000
    max_checkpoints: int = 32
    ring_buffer_size: int = 65536
    flush_interval_ms: int = 10
    max_repr_length: int = 256
    ignore_patterns: list[str] = field(default_factory=list)
    db_path: str | None = None
