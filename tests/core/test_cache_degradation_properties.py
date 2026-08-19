"""Property-based tests for the cache's degradation state machine.

The cache has three flags that interact — _degraded, _reported, _closed — and a
promise resting on them: once it degrades it stops touching the database and
answers as a miss, so a broken cache costs a slower run rather than a failed
one. Individual transitions have unit tests. What those cannot cover is the
claim about *sequences*, which is where a state machine actually goes wrong.

The invariant: no sequence of operations on a degraded cache raises anything
except CacheClosedError (the caller used a closed cache) or
CacheUnavailableError (the caller asked for a value it cannot rebuild).
"""

import contextlib
import sqlite3
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.core.cache import Cache, CacheClosedError, CacheUnavailableError

# The errors a caller is allowed to see. Anything else escaping is the bug.
_ALLOWED = (CacheClosedError, CacheUnavailableError)


@pytest.fixture(autouse=True, scope="module")
def _temp_cache_dir(tmp_path_factory):
    """Keep every Cache in this module inside a temp directory.

    Patched at module scope rather than with monkeypatch: Hypothesis rejects a
    function-scoped fixture under @given, and without this every constructed
    Cache would write to the real ~/.cache/shuffleupagus.
    """
    directory = tmp_path_factory.mktemp("cache-properties")
    original = Cache._db_path
    Cache._db_path = lambda self: str(directory / f"{self.name}.db")  # type: ignore[method-assign]
    yield
    Cache._db_path = original  # type: ignore[method-assign]


def _degraded_cache(name: str, error: Exception) -> Cache:
    """A cache whose connection fails every statement.

    The real connection is closed before being replaced: orphaning it leaves an
    unclosed sqlite handle for the garbage collector, and this project turns
    that ResourceWarning into an error.
    """
    # Any, because _conn is typed as a real sqlite3.Connection and the whole
    # point here is to put something else there.
    cache: Any = Cache(name)
    cache._conn.close()
    cache._conn = _Broken(error)
    return cache


class _Broken:
    """A connection that fails every statement, as a corrupt database does."""

    def __init__(self, error: Exception):
        self.error = error

    def execute(self, *args, **kwargs):
        raise self.error

    def commit(self):
        raise self.error

    def close(self):
        raise self.error


# Every entry point, named so a failure says which one broke the invariant.
_OPERATIONS = {
    "read": lambda c, k: c.read(k),
    "read_stale": lambda c, k: c.read_stale(k),
    "write": lambda c, k: c.write(k, {"v": 1}),
    "touch": lambda c, k: c.touch(k),
    "delete": lambda c, k: c.delete(k),
    "clean": lambda c, k: c._clean(),
    "save": lambda c, k: c.save(),
    "close": lambda c, k: c.close(),
    "read_required": lambda c, k: c.read(k, required=True),
    "read_stale_required": lambda c, k: c.read_stale(k, required=True),
    "write_required": lambda c, k: c.write(k, {"v": 1}, required=True),
}

_errors = st.sampled_from(
    [
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.IntegrityError("constraint failed"),
    ]
)
_sequences = st.lists(st.sampled_from(sorted(_OPERATIONS)), min_size=1, max_size=12)
_keys = st.text(min_size=1, max_size=6)


def _run(cache: Cache, operations: list[str], key: str, where: str) -> None:
    """Apply each operation, failing the test on any error not in _ALLOWED."""
    for name in operations:
        try:
            _OPERATIONS[name](cache, key)
        except _ALLOWED:
            pass
        except Exception as exc:
            pytest.fail(f"{name} leaked {type(exc).__name__} on a {where} cache: {exc}")


@given(operations=_sequences, error=_errors, key=_keys)
def test_a_degraded_cache_only_raises_its_own_errors(operations, error, key):
    """The core invariant, over arbitrary operation sequences."""
    cache = _degraded_cache("degraded", error)
    _run(cache, operations, key, "degraded")


@given(operations=_sequences, key=_keys)
def test_a_healthy_cache_only_raises_its_own_errors(operations, key):
    """The same claim for a working cache, so the invariant is not vacuous."""
    cache = Cache("healthy")
    try:
        _run(cache, operations, key, "healthy")
    finally:
        cache.close()


@given(operations=_sequences, error=_errors, key=_keys)
def test_degradation_is_never_undone(operations, error, key):
    """Once cold, the cache stays cold. A flag that flickers is worse than either state."""
    cache = _degraded_cache("sticky", error)
    _run(cache, operations, key, "degraded")
    if not cache._degraded:
        return
    for name in operations:
        with contextlib.suppress(*_ALLOWED):
            _OPERATIONS[name](cache, key)
        assert cache._degraded, f"{name} cleared the degraded flag"


@pytest.mark.parametrize("count", [1, 2, 8])
def test_one_message_per_failure_class(count, capsys):
    """Repeating one failure says it once; a different class says it again.

    Not a @given test: Hypothesis rejects the function-scoped capsys fixture,
    and the interesting axis here is small and enumerable anyway.
    """
    cache = _degraded_cache("reported", sqlite3.DatabaseError("disk image is malformed"))
    capsys.readouterr()
    for _ in range(count):
        cache.read("k")
    assert capsys.readouterr().out.count("is unusable") == 1

    # A different exception class is a different failure, and is reported again.
    cache._decode("not valid json")
    assert capsys.readouterr().out.count("is unusable") == 1


# --- _brief: the bounded, marked form of an untrusted message ---


@given(text=st.text(max_size=400))
def test_brief_is_always_bounded(text):
    assert len(Cache._brief(sqlite3.DatabaseError(text))) <= 61


@given(text=st.text(max_size=400))
def test_brief_marks_only_what_it_cut(text):
    brief = Cache._brief(sqlite3.DatabaseError(text))
    assert brief.endswith("…") == (len(repr(str(sqlite3.DatabaseError(text)))) > 60)


@given(text=st.text(max_size=20))
def test_a_short_message_survives_intact(text):
    """Nothing is lost when there was nothing to cut."""
    rendered = repr(str(sqlite3.DatabaseError(text)))
    if len(rendered) <= 60:
        assert Cache._brief(sqlite3.DatabaseError(text)) == rendered
