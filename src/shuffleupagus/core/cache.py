import json
import os
import sqlite3
import threading
import time

CACHE_DEFAULT_CUTOFF = 60 * 60 * 24 * 7 * 1.0  # 1 week


class Cache:
    name: str
    cutoff: float

    def __init__(self, name: str, cutoff: float = CACHE_DEFAULT_CUTOFF):
        self.name = name
        self.cutoff = cutoff
        self._lock = threading.Lock()
        print(f"* loading '{name}' cache", flush=True)
        path = self._db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key       TEXT PRIMARY KEY,
                value     TEXT NOT NULL,
                stored_at REAL NOT NULL,
                ttl       REAL NOT NULL
            )"""
        )
        self._conn.commit()

    def _db_path(self):
        return os.path.expanduser(f"~/.cache/shuffleupagus/{self.name}.db")

    def read(self, key: str):
        """Return the cached value if present and not expired, else None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, stored_at, ttl FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        value, stored_at, ttl = row
        if time.time() - stored_at > ttl:
            return None
        return json.loads(value)

    def read_stale(self, key: str):
        """Return the cached value regardless of TTL, or None if absent."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def write(self, key: str, obj, ttl: float | None = None):
        effective_ttl = ttl if ttl is not None else self.cutoff
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, stored_at, ttl) VALUES (?, ?, ?, ?)",
                (key, json.dumps(obj), time.time(), effective_ttl),
            )
            self._conn.commit()
        return obj

    def touch(self, key: str) -> bool:
        """Reset the timestamp of a cache entry to now."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE cache SET stored_at = ? WHERE key = ?",
                (time.time(), key),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def delete(self, key: str) -> bool:
        """Remove a cache entry."""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()
        return cursor.rowcount > 0

    def _clean(self):
        """Evict expired entries. Returns count of evicted rows."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM cache WHERE (? - stored_at) > ttl",
                (time.time(),),
            )
            self._conn.commit()
        return cursor.rowcount

    def save(self):
        """Run eviction. Data is already durable on disk."""
        self._clean()

    def close(self):
        """Close the database connection.

        Takes the same lock as every other operation, so a close racing an
        in-flight read or write waits for it instead of pulling the connection
        out from under it and raising ProgrammingError mid-statement.
        """
        with self._lock:
            self._conn.close()
