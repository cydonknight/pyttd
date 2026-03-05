DB_NAME_SUFFIX = ".pyttd.db"     # <script_name>.pyttd.db
PRAGMAS = {
    'journal_mode': 'wal',
    'cache_size': -1024 * 64,
    'foreign_keys': 1,
    'synchronous': 1,            # WAL + synchronous=NORMAL is safe
    'busy_timeout': 5000,        # 5s timeout for concurrent access (flush thread + queries)
}
