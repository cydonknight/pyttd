"""Thread-safe deferred SQLite connection manager.

Replaces Peewee's SqliteDatabase(None) + db.init(path) pattern.
Each thread gets its own connection via threading.local().
"""

import sqlite3
import threading
from contextlib import contextmanager


class RowProxy:
    """Wraps sqlite3.Row for attribute access (row.filename instead of row['filename']).

    Minimizes changes across query sites that previously used Peewee model instances.
    """
    __slots__ = ('_row',)

    def __init__(self, row):
        self._row = row

    def __getattr__(self, name):
        try:
            return self._row[name]
        except (KeyError, IndexError):
            raise AttributeError(name)

    def __getitem__(self, key):
        return self._row[key]

    def __bool__(self):
        return self._row is not None

    def __repr__(self):
        if self._row is None:
            return 'RowProxy(None)'
        return f'RowProxy({dict(self._row)})'


class Database:
    """Thread-safe deferred SQLite connection manager.

    Each thread gets its own connection via threading.local().
    Connections are created lazily on first query.
    """

    def __init__(self):
        self._path = None
        self._local = threading.local()

    def init(self, path):
        """Set the database path. Close existing connection first."""
        self.close()
        self._path = path

    @property
    def path(self):
        return self._path

    def get_connection(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread."""
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            if self._path is None:
                raise RuntimeError("Database not initialized")
            conn = sqlite3.connect(self._path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = wal")
            conn.execute("PRAGMA cache_size = -65536")
            conn.execute("PRAGMA foreign_keys = 1")
            conn.execute("PRAGMA synchronous = 1")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    def execute(self, sql, params=()):
        """Execute SQL and return cursor."""
        return self.get_connection().execute(sql, params)

    def execute_sql(self, sql, params=()):
        """Alias for execute() — backward compat with Peewee's db.execute_sql()."""
        return self.execute(sql, params)

    def executemany(self, sql, params_seq):
        """Execute SQL with many parameter sets."""
        return self.get_connection().executemany(sql, params_seq)

    def fetchone(self, sql, params=()):
        """Fetch one row as RowProxy, or None."""
        row = self.get_connection().execute(sql, params).fetchone()
        return RowProxy(row) if row else None

    def fetchall(self, sql, params=()):
        """Fetch all rows as list of RowProxy."""
        return [RowProxy(r) for r in self.get_connection().execute(sql, params).fetchall()]

    def fetchval(self, sql, params=()):
        """Fetch a single scalar value."""
        row = self.get_connection().execute(sql, params).fetchone()
        return row[0] if row else None

    def fetchdicts(self, sql, params=()):
        """Fetch all rows as list of dicts."""
        return [dict(r) for r in self.get_connection().execute(sql, params).fetchall()]

    def iterate(self, sql, params=()):
        """Yield RowProxy objects for streaming large result sets."""
        cursor = self.get_connection().execute(sql, params)
        for row in cursor:
            yield RowProxy(row)

    @contextmanager
    def atomic(self):
        """Transaction context manager (replacement for Peewee's db.atomic())."""
        conn = self.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def commit(self):
        """Commit the current thread's connection."""
        conn = getattr(self._local, 'conn', None)
        if conn:
            conn.commit()

    def close(self):
        """Close the current thread's connection."""
        conn = getattr(self._local, 'conn', None)
        if conn:
            conn.close()
            self._local.conn = None

    def is_closed(self):
        """Check if the current thread has no connection."""
        return getattr(self._local, 'conn', None) is None


db = Database()
