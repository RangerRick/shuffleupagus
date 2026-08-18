from shuffleupagus.core.model import Album, Track
from shuffleupagus.core.util import spread_artist_playlists


def _track(id):
    t = Track(id=id, name=id, duration_ms=60_000, album=Album("a", "A"))
    return t


def _zero_offset(monkeypatch):
    """Patch random.randint to always return 0 so the offset never drops tracks."""
    monkeypatch.setattr("shuffleupagus.core.util.random.randint", lambda a, b: 0)


def test_spread_preserves_all_tracks(monkeypatch):
    _zero_offset(monkeypatch)
    playlists = {
        "a1": [_track("a"), _track("b"), _track("c")],
        "a2": [_track("d"), _track("e"), _track("f")],
    }
    result = spread_artist_playlists(playlists, [])
    assert set(result) == {"a", "b", "c", "d", "e", "f"}


def test_spread_returns_unique_ids():
    playlists = {
        "a1": [_track("x"), _track("y")],
        "a2": [_track("z")],
    }
    result = spread_artist_playlists(playlists, [])
    assert len(result) == len(set(result))


def test_spread_single_artist(monkeypatch):
    _zero_offset(monkeypatch)
    playlists = {"a1": [_track("x"), _track("y"), _track("z")]}
    result = spread_artist_playlists(playlists, [])
    assert set(result) == {"x", "y", "z"}


def test_spread_empty():
    result = spread_artist_playlists({}, [])
    assert result == []


def test_spread_vip_positioning(monkeypatch):
    _zero_offset(monkeypatch)
    playlists = {
        "vip": [_track("v1"), _track("v2")],
        "reg": [_track("r1"), _track("r2")],
    }
    result = spread_artist_playlists(playlists, ["vip"])
    assert set(result) == {"v1", "v2", "r1", "r2"}
    # VIP tracks appear before non-VIP tracks (VIP inserted at position 0 in artist list)
    vip_indices = [result.index(t) for t in result if t in ("v1", "v2")]
    reg_indices = [result.index(t) for t in result if t in ("r1", "r2")]
    assert max(vip_indices) < max(reg_indices)
