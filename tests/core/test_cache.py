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


def test_clean_evicts_expired(tmp_path, monkeypatch):
    c = Cache.__new__(Cache)
    c.name = "test"
    c.cutoff = 60.0
    c.autosave = False
    c._update_count = 0
    c._cache = {}
    monkeypatch.setattr(Cache, "_filename", lambda self: str(tmp_path / "t.joblib.gz"))

    # write an entry with a timestamp far in the past
    c._cache["old"] = ["stale_value", time.time() - 3600]
    c._cache["fresh"] = ["fresh_value", time.time()]

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
