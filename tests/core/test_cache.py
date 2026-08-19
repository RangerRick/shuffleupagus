import os
import re
import sqlite3
import stat
import threading
import time

import pytest

from shuffleupagus.core.cache import (
    CACHE_DEFAULT_CUTOFF,
    Cache,
    CacheClosedError,
    CacheUnavailableError,
)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    with Cache("test", cutoff=CACHE_DEFAULT_CUTOFF) as c:
        yield c


def _inject_stale(cache, key, value, ttl, age):
    """Write an entry then backdate its stored_at so it appears expired."""
    cache.write(key, value, ttl=ttl)
    cache._conn.execute(
        "UPDATE cache SET stored_at = ? WHERE key = ?",
        (time.time() - age, key),
    )
    cache._conn.commit()


def test_write_and_read(cache):
    cache.write("key1", {"data": 42})
    assert cache.read("key1") == {"data": 42}


def test_context_manager_returns_cache_and_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    with Cache("ctx") as c:
        assert isinstance(c, Cache)
        c.write("key", "value")
        assert c.read("key") == "value"
    with pytest.raises(CacheClosedError):
        c.read("key")


def test_context_manager_closes_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    with pytest.raises(ValueError), Cache("ctx_err") as c:
        raise ValueError("boom")
    with pytest.raises(CacheClosedError):
        c.read("key")


def test_read_missing_returns_none(cache):
    assert cache.read("nonexistent") is None


def test_write_overwrites(cache):
    cache.write("k", "first")
    cache.write("k", "second")
    assert cache.read("k") == "second"


def test_read_expired_returns_none(cache):
    _inject_stale(cache, "stale", "stale_value", ttl=60.0, age=3600)
    assert cache.read("stale") is None


def test_read_stale_returns_expired_value(cache):
    _inject_stale(cache, "stale", "stale_value", ttl=60.0, age=3600)
    assert cache.read("stale") is None
    assert cache.read_stale("stale") == "stale_value"


def test_read_stale_missing_returns_none(cache):
    assert cache.read_stale("ghost") is None


def test_touch_refreshes_expired_entry(cache):
    _inject_stale(cache, "old", "val", ttl=60.0, age=3600)
    assert cache.read("old") is None
    assert cache.touch("old") is True
    assert cache.read("old") == "val"


def test_touch_missing_returns_false(cache):
    assert cache.touch("ghost") is False


def test_delete_removes_entry(cache):
    cache.write("to_delete", "val")
    assert cache.delete("to_delete") is True
    assert cache.read("to_delete") is None


def test_delete_missing_returns_false(cache):
    assert cache.delete("ghost") is False


def test_write_custom_ttl(cache):
    _inject_stale(cache, "short", "val", ttl=5.0, age=10)
    assert cache.read("short") is None
    assert cache.read_stale("short") == "val"


def test_clean_evicts_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / "t.db"))
    with Cache("test", cutoff=60.0) as c:
        _inject_stale(c, "old", "stale_value", ttl=60.0, age=3600)
        c.write("fresh", "fresh_value", ttl=60.0)

        evicted = c._clean()

        assert evicted == 1
        assert c.read("old") is None
        assert c.read("fresh") == "fresh_value"


def test_clean_keeps_fresh_entries(cache):
    cache.write("a", 1)
    cache.write("b", 2)
    evicted = cache._clean()
    assert evicted == 0
    assert cache.read("a") == 1
    assert cache.read("b") == 2


def test_save_and_load(tmp_path, monkeypatch):
    db_path = str(tmp_path / "svc.db")
    monkeypatch.setattr(Cache, "_db_path", lambda self: db_path)

    with Cache("svc") as c1:
        c1.write("hello", "world")
        c1.save()

    with Cache("svc") as c2:
        assert c2.read("hello") == "world"


# ---------------------------------------------------------------------------
# Concurrent access tests
# ---------------------------------------------------------------------------

NUM_THREADS = 8
OPS_PER_THREAD = 200


def test_concurrent_reads_and_writes(cache):
    """8 threads doing interleaved reads/writes produce no exceptions."""
    errors: list[Exception] = []

    def worker(thread_id):
        try:
            for i in range(OPS_PER_THREAD):
                key = f"t{thread_id}-k{i % 20}"
                cache.write(key, i)
                cache.read(key)
                cache.read_stale(key)
                cache.touch(key)
                if i % 5 == 0:
                    cache.delete(key)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    alive = [t for t in threads if t.is_alive()]
    assert not alive, "Deadlock detected: threads still alive after timeout"
    assert not errors, f"Concurrent access raised: {errors}"


def test_concurrent_write_and_save(tmp_path, monkeypatch):
    """Concurrent writes during save() produce no corruption."""
    db_path = str(tmp_path / "concurrent.db")
    monkeypatch.setattr(Cache, "_db_path", lambda self: db_path)
    with Cache("concurrent") as c:
        errors: list[Exception] = []

        def writer(thread_id):
            try:
                for i in range(OPS_PER_THREAD):
                    c.write(f"t{thread_id}-k{i}", i)
            except Exception as exc:
                errors.append(exc)

        def saver():
            try:
                for _ in range(20):
                    c.save()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(NUM_THREADS)]
        threads.append(threading.Thread(target=saver))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        alive = [t for t in threads if t.is_alive()]
        assert not alive, "Deadlock detected: threads still alive after timeout"
        assert not errors, f"Concurrent write+save raised: {errors}"


def test_close_releases_connection(cache):
    """close() closes the sqlite connection, so later use raises."""
    cache.write("k", "v")
    cache.close()
    with pytest.raises(CacheClosedError):
        cache.read("k")


def test_close_takes_the_lock(cache):
    """close() acquires _lock, so it cannot cut into an in-flight statement."""
    cache.write("k", "v")
    acquired = []
    real_lock = cache._lock

    class _TrackingLock:
        def __enter__(self):
            acquired.append(True)
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    cache._lock = _TrackingLock()
    cache.close()
    assert acquired, "close() did not take the lock"


# ---------------------------------------------------------------------------
# Closed-cache semantics (#49)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.read("k"),
        lambda c: c.read_stale("k"),
        lambda c: c.write("k", "v"),
        lambda c: c.touch("k"),
        lambda c: c.delete("k"),
        lambda c: c.save(),
        lambda c: c._clean(),
    ],
)
def test_every_entry_point_names_the_cache_when_closed(cache, call):
    """A closed cache must say which cache, not raise a bare sqlite3 error."""
    cache.close()
    with pytest.raises(CacheClosedError) as caught:
        call(cache)
    assert "test" in str(caught.value)


def test_close_is_idempotent(cache):
    """Teardown paths can reach close() more than once."""
    cache.close()
    cache.close()


def test_closed_is_observable(cache):
    assert cache.closed is False
    cache.close()
    assert cache.closed is True


def test_context_manager_evicts_before_closing(tmp_path, monkeypatch):
    """__exit__ runs eviction, matching what Service.close does."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    with Cache("evict") as c:
        _inject_stale(c, "old", "v", ttl=60.0, age=3600)
        c.write("fresh", "v", ttl=10**9)
    with Cache("evict") as c2:
        assert c2.read_stale("old") is None, "expired entry survived the with block"
        assert c2.read("fresh") == "v"


def test_context_manager_exit_tolerates_an_already_closed_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    with Cache("early") as c:
        c.close()
    assert c.closed is True


def test_exit_closes_even_when_eviction_fails(tmp_path, monkeypatch):
    """__exit__ exists to guarantee release. A failing save() must not leak."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    c = Cache("evict_fail")
    monkeypatch.setattr(Cache, "_clean", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        c.__exit__(None, None, None)
    assert c.closed is True, "connection leaked when eviction raised"


def test_exit_tolerates_another_holder_closing_first(tmp_path, monkeypatch):
    """The unlocked check is a race; __exit__ must not raise because it lost it."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    c = Cache("race")
    original_save = Cache.save

    def racing_save(self):
        self.close()  # stands in for another thread winning the race
        return original_save(self)

    monkeypatch.setattr(Cache, "save", racing_save)
    c.__exit__(None, None, None)
    assert c.closed is True


def test_exit_does_not_mask_the_bodys_exception(tmp_path, monkeypatch):
    """A teardown error must not replace the error the caller cares about."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    c = Cache("mask")
    original_save = Cache.save

    def racing_save(self):
        self.close()
        return original_save(self)

    monkeypatch.setattr(Cache, "save", racing_save)
    with pytest.raises(ValueError, match="the real error"), c:
        raise ValueError("the real error")
    assert c.closed is True


# --- sqlite failures (#60) ---


class _BrokenConn:
    """A connection whose every statement raises, as a corrupt database does."""

    def __init__(self, error=None):
        self.error = error or sqlite3.DatabaseError("database disk image is malformed")
        self.closed = False

    def execute(self, *args, **kwargs):
        raise self.error

    def commit(self):
        raise self.error

    def close(self):
        self.closed = True


def _break_conn(cache, error=None):
    """Swap in a failing connection, closing the real one first.

    Without the close, the orphaned sqlite connection is finalized by the
    garbage collector during an unrelated test, which pytest reports as an
    unraisable exception in whichever test happened to trigger the collection.
    """
    cache._conn.close()
    cache._conn = _BrokenConn(error)
    return cache


@pytest.fixture
def broken_cache(cache):
    return _break_conn(cache)


def test_read_survives_a_database_error(broken_cache):
    assert broken_cache.read("key1") is None


def test_read_stale_survives_a_database_error(broken_cache):
    assert broken_cache.read_stale("key1") is None


def test_write_survives_a_database_error(broken_cache):
    assert broken_cache.write("key1", {"data": 42}) == {"data": 42}


def test_touch_survives_a_database_error(broken_cache):
    assert broken_cache.touch("key1") is False


def test_delete_survives_a_database_error(broken_cache):
    assert broken_cache.delete("key1") is False


def test_clean_survives_a_database_error(broken_cache):
    assert broken_cache._clean() == 0


def test_save_survives_a_database_error(broken_cache):
    broken_cache.save()


def test_database_error_is_reported_once_per_cache(broken_cache, capsys):
    broken_cache.read("a")
    broken_cache.read("b")
    broken_cache.read("c")
    assert capsys.readouterr().out.count("unusable") == 1


def test_database_error_names_the_cache(broken_cache, capsys):
    broken_cache.read("key1")
    reported = [line for line in capsys.readouterr().out.splitlines() if "unusable" in line]
    assert len(reported) == 1
    assert "'test'" in reported[0]


def test_database_error_message_is_bounded(cache, capsys):
    _break_conn(cache, sqlite3.DatabaseError("x" * 5000))
    cache.read("key1")
    assert len(capsys.readouterr().out) < 500


def test_closed_cache_still_raises_rather_than_degrading(cache):
    cache.close()
    with pytest.raises(CacheClosedError):
        cache.read("key1")


def test_corrupt_database_file_does_not_abort_startup(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a sqlite database" * 20)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(path))
    with Cache("corrupt") as c:
        assert c.read("anything") is None


def test_read_survives_a_corrupt_json_value(cache):
    """A corrupt cache file can hold a row whose value is not valid JSON."""
    cache.write("key1", {"data": 42})
    cache._conn.execute("UPDATE cache SET value = ? WHERE key = ?", ("{not json", "key1"))
    cache._conn.commit()
    assert cache.read("key1") is None


def test_read_stale_survives_a_corrupt_json_value(cache):
    cache.write("key1", {"data": 42})
    cache._conn.execute("UPDATE cache SET value = ? WHERE key = ?", ("{not json", "key1"))
    cache._conn.commit()
    assert cache.read_stale("key1") is None


def test_corrupt_json_is_reported(cache, capsys):
    cache.write("key1", {"data": 42})
    cache._conn.execute("UPDATE cache SET value = ? WHERE key = ?", ("{not json", "key1"))
    cache._conn.commit()
    capsys.readouterr()
    cache.read("key1")
    assert "unusable" in capsys.readouterr().out


@pytest.mark.parametrize("stored", [12345, 3.5, None, b"\xff\xfe"])
def test_decode_survives_a_non_text_value(cache, stored):
    """A corrupt database file can return a value that is not text.

    The column is declared TEXT NOT NULL, so sqlite's own affinity rules stop
    this being reachable through a normal statement — which is why _decode is
    exercised directly. File corruption does not go through those rules.
    """
    assert cache._decode(stored) is None


# --- required: values the caller cannot rebuild (#60 follow-up) ---


def test_required_read_raises_instead_of_missing(broken_cache):
    with pytest.raises(CacheUnavailableError, match="cannot be rebuilt"):
        broken_cache.read("key1", required=True)


def test_required_read_stale_raises_instead_of_missing(broken_cache):
    with pytest.raises(CacheUnavailableError, match="cannot be rebuilt"):
        broken_cache.read_stale("key1", required=True)


def test_required_write_raises_instead_of_swallowing(broken_cache):
    with pytest.raises(CacheUnavailableError, match="cannot be rebuilt"):
        broken_cache.write("key1", {"data": 42}, required=True)


def test_required_still_raises_once_already_degraded(broken_cache):
    broken_cache.read("key1")
    assert broken_cache._degraded
    with pytest.raises(CacheUnavailableError):
        broken_cache.read_stale("key1", required=True)


def test_unrequired_calls_still_degrade_quietly(broken_cache):
    assert broken_cache.read("key1") is None
    assert broken_cache.write("key1", {"a": 1}) == {"a": 1}


def test_required_read_raises_on_an_undecodable_value(cache):
    cache.write("key1", {"data": 42})
    cache._conn.execute("UPDATE cache SET value = ? WHERE key = ?", ("{not json", "key1"))
    cache._conn.commit()
    with pytest.raises(CacheUnavailableError):
        cache.read("key1", required=True)


# --- a programming error is a bug here, not a broken file ---


def test_a_programming_error_is_not_degraded(cache):
    broken = _BrokenConn(sqlite3.ProgrammingError("Incorrect number of bindings supplied"))
    real, cache._conn = cache._conn, broken
    try:
        with pytest.raises(sqlite3.ProgrammingError):
            cache.read("key1")
        assert not cache._degraded
    finally:
        # Put the working connection back, or the fixture's own teardown hits
        # the same error and reports it as a fixture failure.
        cache._conn = real


# --- the cache goes cold, not flaky ---


def test_no_statement_is_issued_once_degraded(cache):
    _break_conn(cache)
    cache.read("key1")
    cache._conn = _CountingConn()
    assert cache.read("key1") is None
    assert cache.write("key1", {"a": 1}) == {"a": 1}
    assert cache.touch("key1") is False
    assert cache.delete("key1") is False
    assert cache._clean() == 0
    assert cache._conn.calls == 0


class _CountingConn:
    """Records whether the cache touched the database at all."""

    def __init__(self):
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("a degraded cache issued a statement")

    def commit(self):
        self.calls += 1

    def close(self):
        pass


# --- each distinct failure is reported once ---


def test_a_second_different_failure_is_still_reported(cache, capsys):
    _break_conn(cache, sqlite3.OperationalError("database is locked"))
    cache.read("a")
    cache._conn = _BrokenConn(sqlite3.DatabaseError("database disk image is malformed"))
    cache._degraded = False
    cache.read("b")
    out = capsys.readouterr().out
    assert "locked" in out
    assert "malformed" in out


def test_a_lock_does_not_advise_deleting_the_database(cache, capsys):
    _break_conn(cache, sqlite3.OperationalError("database is locked"))
    cache.read("a")
    out = capsys.readouterr().out
    assert "Delete" not in out
    assert "Another shuffleupagus process" in out


# --- corrupt timestamps ---


def test_read_survives_a_non_numeric_stored_at(cache):
    cache.write("key1", {"data": 42})
    cache._conn.execute("UPDATE cache SET stored_at = ? WHERE key = ?", ("not a time", "key1"))
    cache._conn.commit()
    assert cache.read("key1") is None


# --- file permissions (#71) ---


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_cache_directory_is_private(tmp_path, monkeypatch):
    db = tmp_path / "cachedir" / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    with Cache("test"):
        assert _mode(db.parent) == 0o700


def test_cache_database_is_private(tmp_path, monkeypatch):
    db = tmp_path / "cachedir" / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    with Cache("test") as c:
        c.write("k", {"a": 1})
        assert _mode(db) & 0o077 == 0


def test_an_existing_world_readable_directory_is_tightened(tmp_path, monkeypatch):
    """A directory left behind by an earlier version keeps its old mode.

    makedirs(mode=...) only applies to directories it actually creates, so an
    upgrade would otherwise stay exposed forever.
    """
    cachedir = tmp_path / "cachedir"
    cachedir.mkdir(mode=0o755)
    db = cachedir / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    with Cache("test"):
        assert _mode(cachedir) == 0o700


def test_sqlite_sidecar_files_are_unreachable(tmp_path, monkeypatch):
    """WAL and shared-memory files are created by sqlite, not by us.

    Their own modes are sqlite's business; a 0700 directory is what stops
    another user reaching them, which is why the directory mode is the check
    that matters.
    """
    db = tmp_path / "cachedir" / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    with Cache("test") as c:
        c.write("k", {"a": 1})
        assert _mode(db.parent) & 0o077 == 0


# --- construction is inside the degradation policy (#73) ---


def test_an_unwritable_cache_directory_degrades(tmp_path, monkeypatch, capsys):
    """The most common real breakage, and the one the policy did not cover."""
    parent = tmp_path / "readonly"
    parent.mkdir(mode=0o500)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(parent / "sub" / "test.db"))
    try:
        cache = Cache("test")
        assert cache.read("k") is None
        assert cache.write("k", {"a": 1}) == {"a": 1}
        assert "unusable" in capsys.readouterr().out
    finally:
        parent.chmod(0o700)


def test_an_unopenable_database_degrades(tmp_path, monkeypatch, capsys):
    """A directory where the database name is taken by a directory."""
    cachedir = tmp_path / "cachedir"
    (cachedir / "test.db").mkdir(parents=True)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(cachedir / "test.db"))
    cache = Cache("test")
    assert cache.read("k") is None
    assert "unusable" in capsys.readouterr().out


def test_a_degraded_construction_still_closes_cleanly(tmp_path, monkeypatch):
    cachedir = tmp_path / "cachedir"
    (cachedir / "test.db").mkdir(parents=True)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(cachedir / "test.db"))
    with Cache("test") as cache:
        pass
    assert cache.closed


# --- close() releases the descriptor (#73) ---


def test_close_releases_the_connection_even_when_it_raises(cache):
    """_closed was set before the close, so a raising close leaked the fd."""
    real = cache._conn
    calls = []

    class _RaisingClose:
        def execute(self, *a, **k):
            return real.execute(*a, **k)

        def commit(self):
            return real.commit()

        def close(self):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("disk I/O error")
            real.close()

    cache._conn = _RaisingClose()
    cache.close()
    # A close that raised left the descriptor open, so the cache is NOT closed
    # and a later call retries rather than being swallowed as a no-op.
    assert not cache.closed
    cache.close()
    assert cache.closed
    assert len(calls) == 2


def test_close_is_still_idempotent_after_a_failure(cache):
    cache.close()
    cache.close()
    assert cache.closed


# --- a truncated message says so (#73) ---


def test_a_truncated_message_is_marked_as_truncated(cache, capsys):
    _break_conn(cache, sqlite3.DatabaseError("x" * 500))
    cache.read("k")
    out = capsys.readouterr().out
    assert "…" in out or "..." in out


def test_a_short_message_is_not_marked(cache, capsys):
    _break_conn(cache, sqlite3.DatabaseError("short"))
    cache.read("k")
    out = capsys.readouterr().out
    assert "…" not in out


# --- the "absent or broken" conflation is deliberate (#73) ---


def test_touch_answers_false_for_both_absent_and_broken(cache):
    """No caller distinguishes them, and both mean the same thing to callers.

    A failed touch just lets the entry expire on its own schedule, which is
    what would have happened without the cache at all.
    """
    assert cache.touch("never-written") is False
    _break_conn(cache)
    assert cache.touch("also-broken") is False


def test_delete_answers_false_for_both_absent_and_broken(cache):
    assert cache.delete("never-written") is False
    _break_conn(cache)
    assert cache.delete("also-broken") is False


def test_clean_answers_zero_for_both_nothing_expired_and_broken(cache):
    assert cache._clean() == 0
    _break_conn(cache)
    assert cache._clean() == 0


def test_a_failed_rate_limit_delete_is_harmless(tmp_path, monkeypatch):
    """The one caller of delete() in a path that matters.

    _check_rate_limit deletes an expired window as cleanup. If that delete is
    lost, the stale row stays and the next run re-reads it, finds it expired
    again, and returns — so the conflation costs a wasted row, not a wrong
    answer.
    """
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    with Cache("test") as c:
        c.write("rate_limit_until", time.time() - 100)
        _break_conn(c)
        assert c.delete("rate_limit_until") is False
        # The row is still there, and still reads as expired.
        assert c.read_stale("rate_limit_until") is None


# --- permission hardening from review (#71 follow-up) ---


def test_the_database_is_never_world_readable_even_briefly(tmp_path, monkeypatch):
    """sqlite created the file at the umask mode, then we narrowed it.

    Between those two the file was readable, and a descriptor opened in that
    window keeps its access after the chmod. The file is now created at the
    right mode instead.
    """
    db = tmp_path / "cachedir" / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    seen = []
    real_connect = sqlite3.connect

    def _spy(path, *a, **k):
        seen.append(_mode(path))
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    with Cache("test"):
        pass
    assert seen, "sqlite3.connect was never called"
    assert all(m & 0o077 == 0 for m in seen), seen


def test_a_symlinked_cache_directory_is_refused(tmp_path, monkeypatch, capsys):
    """chmod follows symlinks, so this was a chmod primitive on any directory."""
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    link = tmp_path / "cachedir"
    link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(link / "test.db"))
    cache = Cache("test")
    assert cache.read("k") is None
    assert _mode(victim) == 0o755
    assert "unusable" in capsys.readouterr().out


def test_a_symlinked_database_is_refused(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("not a database")
    victim.chmod(0o644)
    cachedir = tmp_path / "cachedir"
    cachedir.mkdir(mode=0o700)
    link = cachedir / "test.db"
    link.symlink_to(victim)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(link))
    cache = Cache("test")
    assert cache.read("k") is None
    assert _mode(victim) == 0o644


def test_tightening_an_existing_directory_is_announced(tmp_path, monkeypatch, capsys):
    """Silently reverting a mode the user chose leaves them no way to respond."""
    cachedir = tmp_path / "cachedir"
    cachedir.mkdir(mode=0o755)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(cachedir / "test.db"))
    with Cache("test"):
        pass
    assert "tightened" in capsys.readouterr().out


def test_creating_a_new_directory_is_not_announced(tmp_path, monkeypatch, capsys):
    db = tmp_path / "fresh" / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    with Cache("test"):
        pass
    assert "tightened" not in capsys.readouterr().out


def test_a_symlinked_database_is_refused_without_o_nofollow(tmp_path, monkeypatch):
    """The fallback for a platform where os.O_NOFOLLOW does not exist.

    Racier than the flag, which refuses the symlink atomically, but it keeps the
    protection rather than quietly dropping it where the flag is unavailable.
    """
    import shuffleupagus.core.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_O_NOFOLLOW", 0)
    victim = tmp_path / "victim.txt"
    victim.write_text("not a database")
    victim.chmod(0o644)
    cachedir = tmp_path / "cachedir"
    cachedir.mkdir(mode=0o700)
    link = cachedir / "test.db"
    link.symlink_to(victim)
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(link))

    cache = Cache("test")
    assert cache.read("k") is None
    assert _mode(victim) == 0o644


def test_a_normal_database_still_opens_without_o_nofollow(tmp_path, monkeypatch):
    """Dropping the flag must not break the ordinary path."""
    import shuffleupagus.core.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_O_NOFOLLOW", 0)
    db = tmp_path / "cachedir" / "test.db"
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(db))
    with Cache("test") as c:
        c.write("k", {"a": 1})
        assert c.read("k") == {"a": 1}
    assert _mode(db) & 0o077 == 0


# --- the database path stays inside the cache root (#76) ---------------------


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Redirect the cache root, leaving the real _db_path in place.

    _CACHE_DIR is redirected rather than _db_path, so the containment guard
    still runs and resolves against whatever the root currently is. Patching
    _db_path — which most tests in this file do, because they only care where
    the database lands — would skip the very check these tests are about.
    """
    import shuffleupagus.core.cache as cache_mod

    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", root)
    return root


def _named(name: str) -> Cache:
    """A Cache with only its name set, so _db_path can be called in isolation.

    Constructing one for real opens a database and leaks the connection unless
    it is closed, and none of these tests want a database — they want the path
    that would have been used.
    """
    cache = Cache.__new__(Cache)
    cache.name = name
    return cache


@pytest.mark.parametrize("name", ["appleMusic", "spotify", "youtube"])
def test_the_names_actually_in_use_are_accepted(cache_root, name):
    """The three real service names must keep working."""
    assert _named(name)._db_path() == str(cache_root / f"{name}.db")


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "../../etc/passwd",
        "/etc/passwd",
        "sub/../../escape",
    ],
)
@pytest.mark.usefixtures("cache_root")
def test_a_name_that_leaves_the_cache_root_is_refused(name):
    """Escaping the root is the whole point: _prepare_dir chmods what it creates.

    The message has to carry the offending value, so that is asserted here for
    every case rather than in a second test that re-runs one of them.
    """
    with pytest.raises(ValueError, match=rf"Path traversal.*{re.escape(repr(name + '.db'))}"):
        _named(name)._db_path()


def test_a_bare_dot_name_is_contained_rather_than_refused(cache_root):
    """`..` is not a traversal here, because `.db` is appended before the join.

    The name becomes the ordinary filename `...db`, which sits in the root. Worth
    stating: it looks like it should be refused, and a future reader who moves
    the suffix after the containment check would turn it into a real escape.
    """
    assert _named("..")._db_path() == str(cache_root / "...db")


def test_a_subdirectory_name_stays_inside_the_root(cache_root):
    """Contained, so not this issue's concern, and recorded so it isn't mistaken for one.

    A separator in the name nests a directory under the root. _prepare_dir then
    creates and chmods that directory -- but it is one the cache owns, not an
    arbitrary one, which is the distinction #76 turns on.
    """
    assert _named("sub/inner")._db_path() == str(cache_root / "sub" / "inner.db")
