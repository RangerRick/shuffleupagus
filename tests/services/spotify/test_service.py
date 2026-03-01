"""Tests for SpotifyService with all network calls mocked."""

from unittest.mock import MagicMock

import pytest

from shuffleupagus.core.cache import Cache
from shuffleupagus.services.spotify.service import SpotifyService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _artist_payload(id="a1", name="Artist"):
    return {"id": id, "name": name}


def _album_payload(id="alb1", name="Album", release_date="2022-01-01"):
    return {"id": id, "name": name, "release_date": release_date}


def _track_payload(id="t1", name="Track", duration_ms=180_000, artist_id="a1", album_id="alb1"):
    return {
        "id": id,
        "name": name,
        "duration_ms": duration_ms,
        "artists": [{"id": artist_id}],
        "external_ids": {"isrc": "USRC00000001"},
        "album": {"id": album_id},
    }


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """Return a SpotifyService with a fresh in-memory cache and a mock Spotify client."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    config_mock = MagicMock()
    config_mock.service.return_value = {"cache-ttl-days": None}
    svc = SpotifyService.__new__(SpotifyService)
    svc.cache = Cache("spotify")
    svc.config = {}
    svc.spotify = MagicMock()
    svc.tag = "[spotify] "
    return svc


# ---------------------------------------------------------------------------
# get_artist
# ---------------------------------------------------------------------------


def test_get_artist_cache_miss(svc):
    svc.spotify.artist.return_value = _artist_payload("a1", "My Artist")
    artist = svc.get_artist("a1")
    assert artist.id == "a1"
    assert artist.name == "My Artist"
    svc.spotify.artist.assert_called_once_with("a1")


def test_get_artist_cache_hit(svc):
    svc.cache.write("artist:a1", _artist_payload("a1", "Cached Artist"))
    artist = svc.get_artist("a1")
    assert artist.name == "Cached Artist"
    svc.spotify.artist.assert_not_called()


def test_get_artist_sanitizes_url(svc):
    svc.spotify.artist.return_value = _artist_payload("a1", "Artist")
    svc.get_artist("https://open.spotify.com/artist/a1")
    svc.spotify.artist.assert_called_once_with("a1")


def test_get_artist_from_artist_object(svc):
    from shuffleupagus.core.model import Artist

    obj = Artist("a1", "Existing")
    svc.cache.write("artist:a1", _artist_payload("a1", "Existing"))
    artist = svc.get_artist(obj)
    assert artist.name == "Existing"


# ---------------------------------------------------------------------------
# get_artist_albums
# ---------------------------------------------------------------------------


def test_get_artist_albums(svc):
    from shuffleupagus.core.model import Artist

    svc.spotify.artist_albums.return_value = {
        "items": [_album_payload("alb1", "Album One"), _album_payload("alb2", "Album Two")]
    }
    artist = Artist("a1", "Artist")
    albums = svc.get_artist_albums(artist)
    assert len(albums) == 2
    assert albums[0].name == "Album One"


def test_get_artist_albums_cache_hit(svc):
    from shuffleupagus.core.model import Artist

    svc.cache.write("artist:a1:albums", [_album_payload("alb1", "Cached Album")])
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 1
    svc.spotify.artist_albums.assert_not_called()


def test_get_artist_albums_empty(svc):
    from shuffleupagus.core.model import Artist

    svc.spotify.artist_albums.return_value = {"items": []}
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert albums == []


def test_get_artist_albums_fingerprint_match_uses_stale(svc):
    """Stale cached albums + matching fingerprint → skip full refetch."""
    import time

    from shuffleupagus.core.model import Artist

    stale_albums = [_album_payload("alb1", "Old Album")]
    # Inject expired albums entry and its fingerprint
    svc.cache.write("artist:a1:albums", stale_albums, ttl=60.0)
    svc.cache._conn.execute("UPDATE cache SET stored_at = ? WHERE key = ?", (time.time() - 3600, "artist:a1:albums"))
    svc.cache.write("fingerprint:artist:a1", "alb1", ttl=86400.0)
    svc.cache._conn.execute(
        "UPDATE cache SET stored_at = ? WHERE key = ?", (time.time() - 1800, "fingerprint:artist:a1")
    )
    svc.cache._conn.commit()
    # API confirms latest album is still alb1
    svc.spotify.artist_albums.return_value = {"items": [_album_payload("alb1")]}

    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 1
    # Only one call (limit=1 fingerprint check), not the full refetch
    svc.spotify.artist_albums.assert_called_once_with("a1", limit=1)


def test_get_artist_albums_fingerprint_mismatch_refetches(svc):
    """Stale cached albums + fingerprint mismatch → full refetch."""
    import time

    from shuffleupagus.core.model import Artist

    stale_albums = [_album_payload("alb1", "Old Album")]
    svc.cache.write("artist:a1:albums", stale_albums, ttl=60.0)
    svc.cache._conn.execute("UPDATE cache SET stored_at = ? WHERE key = ?", (time.time() - 3600, "artist:a1:albums"))
    svc.cache._conn.commit()
    # Latest album is different from what's cached
    svc.spotify.artist_albums.side_effect = [
        {"items": [_album_payload("alb2", "New Album")]},  # fingerprint check
        {"items": [_album_payload("alb1"), _album_payload("alb2")]},  # full refetch
    ]

    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 2
    assert svc.spotify.artist_albums.call_count == 2


def test_get_artist_albums_stores_fingerprint(svc):
    """After a full fetch, the fingerprint is stored in the cache."""
    from shuffleupagus.core.model import Artist

    svc.spotify.artist_albums.return_value = {"items": [_album_payload("alb1", "Album")]}
    svc.get_artist_albums(Artist("a1", "A"))

    assert svc.cache.read_stale("fingerprint:artist:a1") == "alb1"


# ---------------------------------------------------------------------------
# get_album_tracks
# ---------------------------------------------------------------------------


def test_get_album_tracks(svc):
    from shuffleupagus.core.model import Album

    svc.spotify.album_tracks.return_value = {"items": [_track_payload()]}
    svc.spotify.artist.return_value = _artist_payload()
    album = Album("alb1", "Album")
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 1
    assert tracks[0].name == "Track"
    assert tracks[0].isrc == "USRC00000001"


def test_get_album_tracks_no_isrc(svc):
    from shuffleupagus.core.model import Album

    payload = _track_payload()
    del payload["external_ids"]
    svc.spotify.album_tracks.return_value = {"items": [payload]}
    svc.spotify.artist.return_value = _artist_payload()
    tracks = svc.get_album_tracks(Album("alb1", "A"))
    assert tracks[0].isrc is None


# ---------------------------------------------------------------------------
# get_artist_top_tracks
# ---------------------------------------------------------------------------


def test_get_artist_top_tracks(svc):
    from shuffleupagus.core.model import Artist

    track = _track_payload()
    track["album"] = {"id": "alb1"}
    svc.spotify.artist_top_tracks.return_value = {"tracks": [track]}
    svc.spotify.album.return_value = _album_payload()
    svc.spotify.artist.return_value = _artist_payload()
    artist = Artist("a1", "A")
    tracks = svc.get_artist_top_tracks(artist)
    assert len(tracks) == 1
    assert tracks[0].album is not None


def test_get_artist_top_tracks_empty(svc):
    from shuffleupagus.core.model import Artist

    svc.spotify.artist_top_tracks.return_value = {"tracks": []}
    tracks = svc.get_artist_top_tracks(Artist("a1", "A"))
    assert tracks == []


# ---------------------------------------------------------------------------
# get_playlist_id_for_name / sync
# ---------------------------------------------------------------------------


def test_get_playlist_id_for_name_found(svc):
    svc.spotify.current_user_playlists.return_value = {
        "items": [{"id": "pl1", "name": "My Playlist"}, {"id": "pl2", "name": "Other"}]
    }
    assert svc.get_playlist_id_for_name("My Playlist") == "pl1"


def test_get_playlist_id_for_name_not_found(svc):
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "Other"}]}
    with pytest.raises(ValueError, match="not found"):
        svc.get_playlist_id_for_name("Missing")


def test_sync_replaces_and_adds(svc):
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "Playlist"}]}
    tracks = [f"t{i}" for i in range(100)]
    svc.sync("Playlist", tracks)
    svc.spotify.playlist_replace_items.assert_called_once_with("pl1", tracks[:80])
    svc.spotify.playlist_add_items.assert_called_once_with("pl1", tracks[80:])


def test_sync_single_batch(svc):
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "P"}]}
    svc.sync("P", ["t1", "t2"])
    svc.spotify.playlist_replace_items.assert_called_once()
    svc.spotify.playlist_add_items.assert_not_called()
