import json
import os
import sqlite3
import threading
import time
from typing import Self

CACHE_DEFAULT_CUTOFF = 60 * 60 * 24 * 7 * 1.0  # 1 week


class CacheClosedError(RuntimeError):
    """A closed Cache was used.

    RuntimeError, because this codebase already treats that as "abort this
    service with a message the user can act on" — see the convention on #28.
    """


class Cache:
    """A sqlite-backed key/value cache with a TTL.

    **Ownership.** A `with` block takes ownership of the connection and closes it
    on exit, so only the code that created a Cache should use one. `Service.cache`
    is shared across threads; putting that in a `with` block closes the connection
    out from under every other holder, and they will then raise CacheClosedError.

    `__enter__`/`__exit__` exist for callers that own the lifetime — today that is
    the test suite, which is why nothing under `src/` uses them. Production
    teardown goes through `Service.close()` instead, and both paths evict before
    closing so they leave the same thing on disk.
    """

    name: str
    cutoff: float

    def __init__(self, name: str, cutoff: float = CACHE_DEFAULT_CUTOFF):
        self.name = name
        self.cutoff = cutoff
        self._lock = threading.Lock()
        self._closed = False
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

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        """Raise if this cache is closed. The caller must already hold _lock.

        Checked under the lock rather than before taking it: an unlocked check
        could pass and then have close() land before the statement runs, which
        turns a clear error back into the bare sqlite3.ProgrammingError this
        exists to replace.
        """
        if self._closed:
            raise CacheClosedError(f"cache '{self.name}' is closed")

    def read(self, key: str):
        """Return the cached value if present and not expired, else None."""
        with self._lock:
            self._require_open()
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
            self._require_open()
            row = self._conn.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def write(self, key: str, obj, ttl: float | None = None):
        effective_ttl = ttl if ttl is not None else self.cutoff
        with self._lock:
            self._require_open()
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, stored_at, ttl) VALUES (?, ?, ?, ?)",
                (key, json.dumps(obj), time.time(), effective_ttl),
            )
            self._conn.commit()
        return obj

    def touch(self, key: str) -> bool:
        """Reset the timestamp of a cache entry to now."""
        with self._lock:
            self._require_open()
            cursor = self._conn.execute(
                "UPDATE cache SET stored_at = ? WHERE key = ?",
                (time.time(), key),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def delete(self, key: str) -> bool:
        """Remove a cache entry."""
        with self._lock:
            self._require_open()
            cursor = self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()
        return cursor.rowcount > 0

    def _clean(self):
        """Evict expired entries. Returns count of evicted rows."""
        with self._lock:
            self._require_open()
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
        """Close the database connection. Idempotent.

        Takes the same lock as every other operation, so a close racing an
        in-flight read or write waits for it instead of pulling the connection
        out from under it and raising ProgrammingError mid-statement.

        Closing twice is a no-op: teardown paths can reach this more than once,
        and making the second call raise would turn correct cleanup into an
        error.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        # save() then close(), matching Service.close(). Without the save() the
        # two teardown paths would leave different amounts of expired data on
        # disk for the same cache.
        if not self._closed:
            self.save()
        self.close()
