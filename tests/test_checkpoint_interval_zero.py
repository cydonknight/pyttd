"""Item #6 regression: checkpoint triggers never fire when interval == 0.

The TLS fast path leaves g_my_next_checkpoint_seq at UINT64_MAX when
checkpoints are disabled, so the slow path is unreachable.  This test
records a workload with interval=0 and asserts no checkpoints were
created."""


def test_no_checkpoint_when_interval_zero(record_func):
    db_path, run_id, stats = record_func('''
        def work(n):
            total = 0
            for i in range(n):
                total += i * i
            return total

        for _ in range(50):
            work(100)
    ''', checkpoint_interval=0)
    assert stats.get('checkpoint_count', 0) == 0, \
        f"expected 0 checkpoints when interval=0, got {stats}"
    # Skip counter should also stay 0 — the guard never runs.
    assert stats.get('checkpoints_skipped_threads', 0) == 0
