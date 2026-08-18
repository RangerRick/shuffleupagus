"""Tests for the top-level orchestration helpers."""

import sqlite3
from unittest.mock import MagicMock

import pytest

from shuffleupagus.core.cache import Cache
from shuffleupagus.core.model import Service
from shuffleupagus.shuffleupagus import _close_service


def test_service_close_releases_cache_connection(tmp_path, monkeypatch):
    """Service.close() evicts expired entries and closes the sqlite connection."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    svc = Service.__new__(Service)
    svc.cache = Cache("test")
    svc.cache.write("k", "v")

    svc.close()

    with pytest.raises(sqlite3.ProgrammingError):
        svc.cache.read("k")


def test_close_service_swallows_errors():
    """One service failing to close does not stop the caller."""
    svc = MagicMock()
    svc.tag = "[test] "
    svc.close.side_effect = RuntimeError("boom")

    _close_service(svc)

    svc.close.assert_called_once()


def test_service_close_shuts_down_thread_pools(tmp_path, monkeypatch):
    """close() shuts both worker pools down before releasing the cache."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    svc = Service.__new__(Service)
    svc.cache = Cache("test")

    # Touch both properties so the lazily-created pools actually exist.
    artist_pool = svc.artist_pool
    album_pool = svc.album_pool

    svc.close()

    assert artist_pool._shutdown
    assert album_pool._shutdown


def test_service_close_with_no_pools_created(tmp_path, monkeypatch):
    """A service that never used its pools still closes cleanly."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    svc = Service.__new__(Service)
    svc.cache = Cache("test")

    svc.close()

    with pytest.raises(sqlite3.ProgrammingError):
        svc.cache.read("k")


def test_collect_tracks_fatal_error_shuts_down_pools(tmp_path, monkeypatch):
    """A fatal RuntimeError drops the queued backlog instead of just cancelling."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))

    class _Svc(Service):
        name = "test"

        def __init__(self):
            self.cache = Cache("test")
            self.tag = "[test] "

        def get_artist(self, artist):
            raise RuntimeError("rate limited")

    svc = _Svc()
    pool = svc.artist_pool

    with pytest.raises(RuntimeError, match="rate limited"):
        svc.collect_tracks(artist_ids=["a1", "a2", "a3"])

    assert pool._shutdown, "artist pool was not shut down on fatal error"
    svc.cache.close()
