import threading
import time

import pytest

from shuffleupagus.core.cache import CACHE_DEFAULT_CUTOFF, Cache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Cache, "_filename", lambda self: str(tmp_path / f"{self.name}.joblib.gz")
    )
    return Cache("test", cutoff=CACHE_DEFAULT_CUTOFF, autosave=False)


def test_write_and_read(cache):
    cache.write("key1", {"data": 42})
    assert cache.read("key1") == {"data": 42}


def test_read_missing_returns_none(cache):
    assert cache.read("nonexistent") is None


def test_write_overwrites(cache):
    cache.write("k", "first")
    cache.write("k", "second")
    assert cache.read("k") == "second"


def test_read_expired_returns_none(cache):
    # Inject an expired entry directly (timestamp in the past, short TTL)
    cache._cache["stale"] = ["stale_value", time.time() - 3600, 60.0]
    assert cache.read("stale") is None


def test_read_stale_returns_expired_value(cache):
    cache._cache["stale"] = ["stale_value", time.time() - 3600, 60.0]
    assert cache.read("stale") is None           # expired: read() returns None
    assert cache.read_stale("stale") == "stale_value"  # stale read still returns value


def test_read_stale_missing_returns_none(cache):
    assert cache.read_stale("ghost") is None


def test_touch_refreshes_expired_entry(cache):
    cache._cache["old"] = ["val", time.time() - 3600, 60.0]
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
    # write with explicit short TTL; entry expires immediately when injected old
    cache._cache["short"] = ["val", time.time() - 10, 5.0]
    assert cache.read("short") is None           # expired
    assert cache.read_stale("short") == "val"    # stale read returns it


def test_clean_evicts_expired(tmp_path, monkeypatch):
    c = Cache.__new__(Cache)
    c.name = "test"
    c.cutoff = 60.0
    c.autosave = False
    c._update_count = 0
    c._lock = threading.Lock()
    c._saving = False
    c._cache = {}
    monkeypatch.setattr(Cache, "_filename", lambda self: str(tmp_path / "t.joblib.gz"))

    c._cache["old"] = ["stale_value", time.time() - 3600, 60.0]
    c._cache["fresh"] = ["fresh_value", time.time(), 60.0]

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
    path = str(tmp_path / "svc.joblib.gz")
    monkeypatch.setattr(Cache, "_filename", lambda self: path)

    c1 = Cache("svc", autosave=False)
    c1.write("hello", "world")
    c1.save()

    c2 = Cache("svc", autosave=False)
    assert c2.read("hello") == "world"


def test_autosave_triggers(tmp_path, monkeypatch):
    path = str(tmp_path / "auto.joblib.gz")
    monkeypatch.setattr(Cache, "_filename", lambda self: path)
    import shuffleupagus.core.cache as cache_mod

    original = cache_mod.CACHE_AUTOSAVE_LIMIT
    cache_mod.CACHE_AUTOSAVE_LIMIT = 2

    c = Cache("auto", autosave=True)
    for i in range(4):
        c.write(f"k{i}", i)

    # After 4 writes with limit=2, save should have been triggered (count hits 3 > 2)
    c2 = Cache("auto", autosave=False)
    assert c2.read("k0") == 0

    cache_mod.CACHE_AUTOSAVE_LIMIT = original


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
    path = str(tmp_path / "concurrent.joblib.gz")
    monkeypatch.setattr(Cache, "_filename", lambda self: path)
    c = Cache("concurrent", autosave=False)
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


def test_autosave_under_contention(tmp_path, monkeypatch):
    """Autosave with low threshold and 8 threads does not deadlock."""
    import shuffleupagus.core.cache as cache_mod

    path = str(tmp_path / "contention.joblib.gz")
    monkeypatch.setattr(Cache, "_filename", lambda self: path)
    original = cache_mod.CACHE_AUTOSAVE_LIMIT
    cache_mod.CACHE_AUTOSAVE_LIMIT = 5

    c = Cache("contention", autosave=True)
    errors: list[Exception] = []

    def worker(thread_id):
        try:
            for i in range(OPS_PER_THREAD):
                c.write(f"t{thread_id}-k{i}", i)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    alive = [t for t in threads if t.is_alive()]
    cache_mod.CACHE_AUTOSAVE_LIMIT = original
    assert not alive, "Deadlock detected: threads still alive after timeout"
    assert not errors, f"Autosave contention raised: {errors}"
