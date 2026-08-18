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
