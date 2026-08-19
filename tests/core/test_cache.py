import sqlite3
import threading
import time

import pytest

from shuffleupagus.core.cache import CACHE_DEFAULT_CUTOFF, Cache, CacheClosedError


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
