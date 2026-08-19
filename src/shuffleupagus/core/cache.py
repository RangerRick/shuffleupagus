import json
import os
import sqlite3
import stat
import threading
import time
from typing import Self

CACHE_DEFAULT_CUTOFF = 60 * 60 * 24 * 7 * 1.0  # 1 week


class CacheClosedError(RuntimeError):
    """A closed Cache was used.

    RuntimeError, because this codebase already treats that as "abort this
    service with a message the user can act on" — see the convention on #28.
    """


class CacheUnavailableError(RuntimeError):
    """The cache could not serve a value the caller cannot rebuild.

    Most of what the cache holds is a copy of something a service will happily
    send again, so a broken cache costs a slower run and nothing else. Some of
    it is not: a recorded rate-limit window exists precisely because the API
    will not answer again. Losing that silently walks the next run into an API
    that has already refused it, so those callers ask for the failure.
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
        self._degraded = False
        self._reported: set[str] = set()
        print(f"* loading '{name}' cache", flush=True)
        path = self._db_path()
        # Opening is inside the policy too. An unwritable cache directory or a
        # database that will not open is the most common real breakage of a
        # cache, and it is exactly the case where a rebuildable file should
        # cost a slower run rather than the whole run.
        self._conn: sqlite3.Connection | None = None
        try:
            self._prepare_dir(os.path.dirname(path))
            self._prepare_file(path)
            self._conn = sqlite3.connect(path, check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            self._degrade("opening", exc)
            return
        try:
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
        except sqlite3.DatabaseError as exc:
            self._degrade("opening", exc)

    @property
    def _db(self) -> sqlite3.Connection:
        """The connection, for code that has already passed the _degraded gate.

        _conn is None only when construction failed, and that also sets
        _degraded, which every operation checks before reaching here. This says
        that invariant out loud so the type checker can see it, rather than
        leaving six call sites indexing an Optional.
        """
        if self._conn is None:
            raise CacheUnavailableError(f"cache '{self.name}' was never opened. {self._remedy(sqlite3.Error())}")
        return self._conn

    _DIR_MODE = 0o700
    _FILE_MODE = 0o600

    def _prepare_dir(self, directory: str) -> None:
        """Create the cache directory private, and tighten one left by an older version.

        The directory mode is the control that matters. sqlite creates the
        `-wal` and `-shm` sidecar files itself, under its own umask, so their
        modes are not ours to set — a directory nobody else can traverse is
        what keeps them unreachable.

        The chmod is not redundant with the makedirs mode: makedirs applies a
        mode only to directories it actually creates, so an upgrade from a
        version that left 0755 behind would otherwise stay exposed forever.
        """
        os.makedirs(directory, mode=self._DIR_MODE, exist_ok=True)
        # Refused rather than resolved. os.chmod follows symlinks and has no
        # working follow_symlinks=False on Linux, so a symlinked cache
        # directory would make this a chmod of whatever it points at.
        if os.path.islink(directory):
            raise OSError(f"cache directory {directory} is a symlink; refusing to change its target's permissions")
        if stat.S_IMODE(os.stat(directory).st_mode) & 0o077:
            os.chmod(directory, self._DIR_MODE)
            # Said out loud: this also reverts a mode the user may have chosen
            # on purpose, and it would do so again on every run. Silently
            # undoing someone's decision leaves them nothing to respond to.
            print(f"* tightened {directory} to {self._DIR_MODE:o}", flush=True)

    def _prepare_file(self, path: str) -> None:
        """Make sure the database exists at the right mode before sqlite opens it.

        sqlite creates the file under the process umask, which on a default
        umask of 022 leaves it world-readable from the moment connect() returns
        until a chmod lands. A descriptor opened in that window keeps its
        access afterwards, so the file is created at the right mode instead of
        being narrowed after the fact.

        O_NOFOLLOW refuses a symlink for the same reason _prepare_dir does.
        O_CREAT applies the mode only when it actually creates the file, so a
        database left world-readable by an older version is narrowed below.
        """
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, self._FILE_MODE)
        os.close(fd)
        if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            os.chmod(path, self._FILE_MODE)

    def _decode(self, value: str, required: bool = False):
        """Decode a stored value, degrading to a miss if it is not valid JSON.

        A corrupt database can return a row whose value is not what was written.
        sqlite reports nothing wrong in that case — the row reads back fine and
        json.loads is what fails — so this is the same disposable-cache failure
        as a DatabaseError and takes the same path.

        Both error types are caught because the column is not declared STRICT:
        text that is not JSON raises ValueError, and a row holding a number or
        NULL instead of text raises TypeError.
        """
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            self._degrade("decoding", exc)
            return self._unusable(required, "decoded")

    def _degrade(self, operation: str, exc: Exception) -> None:
        """Mark the cache unusable and say so, then let the run continue.

        Most of what the cache holds can be fetched again from the service it
        came from, so a corrupt or unwritable file costs a slower run rather
        than a wrong one. Callers holding a value that cannot be rebuilt pass
        required=True and get CacheUnavailableError instead.

        Once this fires the cache stops touching the database at all. Reporting
        the failure but continuing to issue statements leaves it flaky rather
        than cold: some succeed, some do not, and no caller can tell which
        answers it is getting.

        One message per distinct failure, keyed by exception class. A single
        latch would report "database is locked" and then stay quiet through a
        later "disk image is malformed", which is the more serious of the two
        and the one naming the real remedy.
        """
        if isinstance(exc, sqlite3.ProgrammingError):
            # A wrong binding count or bad SQL is a bug in this file, not a
            # broken file on disk. Degrading would hide it behind a cache miss,
            # and the tests assert miss-equivalent values, so it would pass them.
            raise exc

        self._degraded = True
        kind = type(exc).__name__
        if kind in self._reported:
            return
        self._reported.add(kind)
        print(
            f"! cache '{self.name}' is unusable, continuing without it ({operation}): {self._brief(exc)}. "
            f"{self._remedy(exc)}",
            flush=True,
        )

    @staticmethod
    def _brief(exc: Exception, limit: int = 60) -> str:
        """A bounded repr of an untrusted message, marked when it was cut.

        The sqlite message can carry a row this project does not control, so it
        is truncated. Without the ellipsis the reader cannot tell whether they
        are looking at the whole message or the first 60 characters of one.
        """
        text = repr(str(exc))
        return text if len(text) <= limit else text[:limit] + "…"

    def _remedy(self, exc: Exception) -> str:
        """Advice that fits the failure, or none at all.

        Telling the user to delete the database is right for corruption and
        actively harmful for a lock: another shuffleupagus process is using
        that file, and deleting it destroys that run's cache.
        """
        if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
            return "Another shuffleupagus process is probably using it; wait for that run to finish."
        return f"Delete {self._db_path()} to rebuild it."

    def _unusable(self, required: bool, operation: str):
        """Answer for an operation the cache can no longer perform.

        Returns None for callers that can rebuild the value, which reads as a
        miss. Raises for callers that cannot.
        """
        if required:
            raise CacheUnavailableError(
                f"cache '{self.name}' is unusable, and the value being {operation} cannot be rebuilt. "
                f"{self._remedy(sqlite3.DatabaseError())}"
            )

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

    def read(self, key: str, required: bool = False):
        """Return the cached value if present and not expired, else None.

        A database failure answers None, the same as a miss, unless the caller
        passes required=True — see CacheUnavailableError.
        """
        with self._lock:
            self._require_open()
            if self._degraded:
                return self._unusable(required, "read")
            try:
                row = self._db.execute(
                    "SELECT value, stored_at, ttl FROM cache WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                # Unpacked and compared inside the guard: the columns are not
                # STRICT, so a corrupt row can hold text where a time should be,
                # and the subtraction below would then raise TypeError.
                value, stored_at, ttl = row
                expired = time.time() - stored_at > ttl
            except sqlite3.DatabaseError as exc:
                self._degrade("reading", exc)
                return self._unusable(required, "read")
            except (TypeError, ValueError) as exc:
                self._degrade("reading", exc)
                return self._unusable(required, "read")
        if expired:
            return None
        return self._decode(value, required)

    def read_stale(self, key: str, required: bool = False):
        """Return the cached value regardless of TTL, or None if absent.

        required=True turns a database failure into CacheUnavailableError
        rather than an answer indistinguishable from "no such key".
        """
        with self._lock:
            self._require_open()
            if self._degraded:
                return self._unusable(required, "read")
            try:
                row = self._db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
            except sqlite3.DatabaseError as exc:
                self._degrade("reading", exc)
                return self._unusable(required, "read")
        if row is None:
            return None
        return self._decode(row[0], required)

    def write(self, key: str, obj, ttl: float | None = None, required: bool = False):
        """Store a value and return it.

        The value is returned whether or not the store succeeded, because
        callers pass a freshly fetched object through this and a dead cache
        must not turn a good fetch into None. That makes the return value
        useless as a success signal, which is why a caller that needs the store
        to have happened passes required=True and gets an exception instead.
        """
        effective_ttl = ttl if ttl is not None else self.cutoff
        with self._lock:
            self._require_open()
            if self._degraded:
                self._unusable(required, "written")
                return obj
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO cache (key, value, stored_at, ttl) VALUES (?, ?, ?, ?)",
                    (key, json.dumps(obj), time.time(), effective_ttl),
                )
                self._db.commit()
            except sqlite3.DatabaseError as exc:
                self._degrade("writing", exc)
                self._unusable(required, "written")
        return obj

    def touch(self, key: str) -> bool:
        """Reset the timestamp of a cache entry to now.

        False means the entry was not there, or the cache is unusable. No
        caller distinguishes the two: a failed touch just lets the entry
        expire on its own schedule.
        """
        with self._lock:
            self._require_open()
            if self._degraded:
                return False
            try:
                cursor = self._db.execute(
                    "UPDATE cache SET stored_at = ? WHERE key = ?",
                    (time.time(), key),
                )
                self._db.commit()
            except sqlite3.DatabaseError as exc:
                self._degrade("updating", exc)
                return False
        return cursor.rowcount > 0

    def delete(self, key: str) -> bool:
        """Remove a cache entry. False means it was absent, or the cache is unusable."""
        with self._lock:
            self._require_open()
            if self._degraded:
                return False
            try:
                cursor = self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._db.commit()
            except sqlite3.DatabaseError as exc:
                self._degrade("deleting", exc)
                return False
        return cursor.rowcount > 0

    def _clean(self):
        """Evict expired entries. Returns the number of rows removed.

        Zero means nothing had expired, or the cache is unusable and nothing
        could be removed.
        """
        with self._lock:
            self._require_open()
            if self._degraded:
                return 0
            try:
                cursor = self._db.execute(
                    "DELETE FROM cache WHERE (? - stored_at) > ttl",
                    (time.time(),),
                )
                self._db.commit()
            except sqlite3.DatabaseError as exc:
                self._degrade("evicting", exc)
                return 0
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

        _closed is set only once the connection is actually released. Setting it
        first meant a close that raised left the descriptor open and the cache
        marked closed, so nothing would ever try again — the one path where a
        second call is not a no-op but a retry.
        """
        with self._lock:
            if self._closed:
                return
            if self._conn is None:
                # Construction failed before the connection existed.
                self._closed = True
                return
            try:
                self._conn.close()
            except sqlite3.DatabaseError as exc:
                self._degrade("closing", exc)
                return
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        # save() then close(), matching Service.close(). Without the save() the
        # two teardown paths would leave different amounts of expired data on
        # disk for the same cache.
        #
        # No pre-check on _closed: reading it unlocked is a race, and losing it
        # would raise CacheClosedError out of teardown — which masks whatever the
        # with body raised. Another holder having closed first is not something
        # teardown should complain about.
        #
        # close() is in a finally because releasing the connection is the whole
        # promise of __exit__: eviction failing must not turn a with block into
        # a leak.
        try:
            self.save()
        except CacheClosedError:
            pass
        finally:
            self.close()
