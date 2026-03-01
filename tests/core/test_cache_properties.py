"""Property-based tests for shuffleupagus.core.cache.Cache."""

import os
import tempfile
import time
from unittest.mock import patch

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from shuffleupagus.core.cache import CACHE_DEFAULT_CUTOFF, Cache


def _make_cache(tmp_dir: str, name: str = "test", cutoff: float = CACHE_DEFAULT_CUTOFF) -> Cache:
    """Create a Cache that persists to a temp directory."""
    path = os.path.join(tmp_dir, f"{name}.db")
    with patch.object(Cache, "_db_path", return_value=path):
        c = Cache(name, cutoff=cutoff)
    c._db_path = lambda: path  # type: ignore[method-assign]
    return c


# Values that JSON can round-trip without issue.
_serializable = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
    st.lists(st.integers()),
    st.dictionaries(st.text(), st.integers()),
)

_key = st.text(min_size=1)


def _inject_stale(cache, key, value, ttl, age):
    """Write an entry then backdate its stored_at so it appears expired."""
    cache.write(key, value, ttl=ttl)
    cache._conn.execute(
        "UPDATE cache SET stored_at = ? WHERE key = ?",
        (time.time() - age, key),
    )
    cache._conn.commit()


# ---------------------------------------------------------------------------
# Write -> read roundtrip
# ---------------------------------------------------------------------------


@given(key=_key, value=_serializable)
@settings(max_examples=200)
def test_write_then_read_returns_value(key, value):
    """A value written to the cache is immediately readable under the same key."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        cache.write(key, value)
        assert cache.read(key) == value


@given(key=_key, v1=_serializable, v2=_serializable)
def test_write_twice_latest_wins(key, v1, v2):
    """The second write to the same key shadows the first."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        cache.write(key, v1)
        cache.write(key, v2)
        assert cache.read(key) == v2


@given(key=_key, value=_serializable)
def test_fresh_write_with_large_ttl_is_always_readable(key, value):
    """A write with a very long TTL is never expired immediately."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp, cutoff=10**9)
        cache.write(key, value)
        assert cache.read(key) == value


# ---------------------------------------------------------------------------
# Expired entries
# ---------------------------------------------------------------------------


@given(key=_key, value=_serializable, age=st.floats(min_value=1.0, max_value=3600.0))
def test_expired_entry_not_readable(key, value, age):
    """An entry whose timestamp is older than its TTL is not returned by read()."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        ttl = age / 2.0  # TTL is half the age, so entry is definitely expired
        _inject_stale(cache, key, value, ttl=ttl, age=age)
        assert cache.read(key) is None


@given(key=_key, value=_serializable, age=st.floats(min_value=1.0, max_value=3600.0))
def test_stale_read_returns_expired_value(key, value, age):
    """read_stale() always returns expired values (or None if absent)."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        ttl = age / 2.0
        _inject_stale(cache, key, value, ttl=ttl, age=age)
        assert cache.read(key) is None
        assert cache.read_stale(key) == value


@given(key=_key, value=_serializable)
def test_stale_read_returns_fresh_value_too(key, value):
    """read_stale() also works for entries that haven't expired."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp, cutoff=10**9)
        cache.write(key, value)
        assert cache.read_stale(key) == value


# ---------------------------------------------------------------------------
# Touch
# ---------------------------------------------------------------------------


@given(key=_key, value=_serializable, age=st.floats(min_value=1.0, max_value=3600.0))
def test_touch_makes_expired_entry_readable(key, value, age):
    """After touch(), an expired entry is readable again."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        ttl = age / 2.0
        _inject_stale(cache, key, value, ttl=ttl, age=age)
        assert cache.read(key) is None
        result = cache.touch(key)
        assert result is True
        assert cache.read(key) == value


@given(key=_key)
def test_touch_missing_key_returns_false(key):
    """touch() on a non-existent key returns False."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        assert cache.touch(key) is False


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@given(key=_key, value=_serializable)
def test_delete_existing_returns_true_and_removes(key, value):
    """delete() returns True and the key is gone afterward."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp, cutoff=10**9)
        cache.write(key, value)
        assert cache.delete(key) is True
        assert cache.read(key) is None
        assert cache.read_stale(key) is None


@given(key=_key)
def test_delete_missing_returns_false(key):
    """delete() on a non-existent key returns False."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp)
        assert cache.delete(key) is False


@given(key=_key, value=_serializable)
def test_double_delete_second_returns_false(key, value):
    """Deleting a key twice: first True, second False."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp, cutoff=10**9)
        cache.write(key, value)
        assert cache.delete(key) is True
        assert cache.delete(key) is False


# ---------------------------------------------------------------------------
# Multiple keys don't interfere
# ---------------------------------------------------------------------------


@given(key1=_key, key2=_key, v1=_serializable, v2=_serializable)
def test_independent_keys(key1, key2, v1, v2):
    """Writing two different keys does not corrupt either value."""
    assume(key1 != key2)
    with tempfile.TemporaryDirectory() as tmp:
        cache = _make_cache(tmp, cutoff=10**9)
        cache.write(key1, v1)
        cache.write(key2, v2)
        assert cache.read(key1) == v1
        assert cache.read(key2) == v2
