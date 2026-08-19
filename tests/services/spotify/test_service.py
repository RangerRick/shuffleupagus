"""Tests for SpotifyService with all network calls mocked."""

import threading
import time
from unittest.mock import MagicMock

import pytest
import spotipy

from shuffleupagus.core.apiresponse import ApiResponseError
from shuffleupagus.core.cache import Cache
from shuffleupagus.core.model import Album, Artist, Service
from shuffleupagus.services.spotify.service import (
    SpotifyService,
    _retry_after_seconds,
)

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
    cache = Cache("spotify")
    svc.cache = cache
    svc.config = {}
    svc.spotify = MagicMock()
    svc._api_lock = threading.Lock()
    svc._rate_limited = None
    svc.tag = "[spotify] "
    yield svc
    # Close the cache this fixture built, not svc.cache — a test may have swapped
    # svc.cache for a mock, which would orphan the real sqlite connection.
    cache.close()


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
    obj = Artist("a1", "Existing")
    svc.cache.write("artist:a1", _artist_payload("a1", "Existing"))
    artist = svc.get_artist(obj)
    assert artist.name == "Existing"


# ---------------------------------------------------------------------------
# get_artist_albums
# ---------------------------------------------------------------------------


def test_get_artist_albums(svc):
    svc.spotify.artist_albums.return_value = {
        "items": [_album_payload("alb1", "Album One"), _album_payload("alb2", "Album Two")]
    }
    artist = Artist("a1", "Artist")
    albums = svc.get_artist_albums(artist)
    assert len(albums) == 2
    assert albums[0].name == "Album One"


def test_get_artist_albums_cache_hit(svc):
    svc.cache.write("artist:a1:albums", [_album_payload("alb1", "Cached Album")])
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 1
    svc.spotify.artist_albums.assert_not_called()


def test_get_artist_albums_empty(svc):
    svc.spotify.artist_albums.return_value = {"items": []}
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert albums == []


def test_get_artist_albums_fingerprint_match_uses_stale(svc):
    """Stale cached albums + matching fingerprint → skip full refetch."""
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
    svc.spotify.artist_albums.return_value = {"items": [_album_payload("alb1", "Album")]}
    svc.get_artist_albums(Artist("a1", "A"))

    assert svc.cache.read_stale("fingerprint:artist:a1") == "alb1"


# ---------------------------------------------------------------------------
# get_album_tracks
# ---------------------------------------------------------------------------


def test_get_album_tracks(svc):

    svc.spotify.album_tracks.return_value = {"items": [_track_payload()]}
    svc.spotify.artist.return_value = _artist_payload()
    album = Album("alb1", "Album")
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 1
    assert tracks[0].name == "Track"
    assert tracks[0].isrc == "USRC00000001"


def test_get_album_tracks_no_isrc(svc):

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


def _mock_playlist_items(svc, track_ids):
    """Make playlist_items read back exactly track_ids, one page at a time."""

    def _items(_playlist_id, limit=100, offset=0, **_kwargs):
        page = track_ids[offset : offset + limit]
        return {"items": [{"track": {"id": tid}} for tid in page]}

    svc.spotify.playlist_items.side_effect = _items


def test_sync_replaces_and_adds(svc):
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "Playlist"}]}
    tracks = [f"t{i}" for i in range(100)]
    _mock_playlist_items(svc, tracks)
    svc.sync("Playlist", tracks)
    svc.spotify.playlist_replace_items.assert_called_once_with("pl1", tracks[:80])
    svc.spotify.playlist_add_items.assert_called_once_with("pl1", tracks[80:])


def test_sync_single_batch(svc):
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "P"}]}
    _mock_playlist_items(svc, ["t1", "t2"])
    svc.sync("P", ["t1", "t2"])
    svc.spotify.playlist_replace_items.assert_called_once()
    svc.spotify.playlist_add_items.assert_not_called()


def test_sync_readds_missing_tracks(svc):
    """A track absent from the read-back is re-added, then verification passes."""
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "P"}]}
    reads = [["t1"], ["t1", "t2"]]

    def _items(_playlist_id, limit=100, offset=0, **_kwargs):
        page = reads[0] if len(reads) == 1 else reads.pop(0)
        return {"items": [{"track": {"id": tid}} for tid in page[offset : offset + limit]]}

    svc.spotify.playlist_items.side_effect = _items
    svc.sync("P", ["t1", "t2"])
    svc.spotify.playlist_add_items.assert_called_once_with("pl1", ["t2"])


def test_sync_raises_when_tracks_never_verify(svc):
    """A track the API never persists raises instead of retrying forever."""
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "P"}]}
    _mock_playlist_items(svc, ["t1"])
    with pytest.raises(RuntimeError, match="could not be verified"):
        svc.sync("P", ["t1", "t2"])


# ---------------------------------------------------------------------------
# rate-limit detection
# ---------------------------------------------------------------------------


def _rate_limit_exc(headers):
    """Build the 429 SpotifyException spotipy raises, carrying response headers."""
    return spotipy.SpotifyException(429, -1, "rate limited", headers=headers)


def test_retry_after_seconds_reads_header():
    assert _retry_after_seconds(_rate_limit_exc({"Retry-After": "3661"})) == 3661


def test_retry_after_seconds_missing_header():
    assert _retry_after_seconds(_rate_limit_exc({})) == 0


def test_retry_after_seconds_no_headers_attr():
    assert _retry_after_seconds(Exception("rate limited")) == 0


def test_call_429_with_retry_after_records_window(svc):
    """A 429 carrying Retry-After is reported with a countdown and cached."""
    svc.spotify.artist.side_effect = _rate_limit_exc({"Retry-After": "3661"})
    with pytest.raises(RuntimeError, match="1h 1m from now"):
        svc._call(svc.spotify.artist, "a1")
    assert svc.cache.read_stale(Service._RATE_LIMIT_CACHE_KEY) > time.time()


def test_call_429_without_retry_after_does_not_cache(svc):
    """Without Retry-After there is no known window, so nothing is persisted."""
    svc.spotify.artist.side_effect = _rate_limit_exc({})
    with pytest.raises(RuntimeError, match="no Retry-After header"):
        svc._call(svc.spotify.artist, "a1")
    assert svc.cache.read_stale(Service._RATE_LIMIT_CACHE_KEY) is None


@pytest.mark.parametrize(
    "exc",
    [
        TypeError("a programming error"),
        AttributeError("another one"),
        ValueError("not an API failure at all"),
        ConnectionError("the network went away"),
    ],
)
def test_call_reraises_anything_that_is_not_a_rate_limit(svc, exc):
    """_call detects 429 and passes everything else through untouched.

    This is what makes the broad catch safe, so it is worth pinning: the
    exception the caller sees must be the original object, not a RuntimeError
    wearing its message.
    """
    svc.spotify.artist.side_effect = exc
    with pytest.raises(type(exc)) as caught:
        svc._call(svc.spotify.artist, "a1")
    assert caught.value is exc
    assert svc.cache.read_stale(Service._RATE_LIMIT_CACHE_KEY) is None


# ---------------------------------------------------------------------------
# _require_config
# ---------------------------------------------------------------------------


def test_require_config_missing_key(svc):
    with pytest.raises(ValueError, match="Missing required config key"):
        svc._require_config("client-id")


def test_require_config_present(svc):
    svc.config = {"client-id": "abc123"}
    assert svc._require_config("client-id") == "abc123"


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_sets_up_session(svc, monkeypatch):
    mock_oauth = MagicMock()
    mock_oauth.cache_handler.get_cached_token.return_value = {"access_token": "tok"}
    mock_oauth.validate_token.return_value = {"access_token": "tok"}
    mock_oauth.is_token_expired.return_value = False

    monkeypatch.setattr(
        "shuffleupagus.services.spotify.service.SpotifyOAuth",
        lambda **kwargs: mock_oauth,
    )
    mock_spotify_instance = MagicMock()
    mock_spotify_instance._session = MagicMock()
    monkeypatch.setattr(
        "shuffleupagus.services.spotify.service.spotipy.Spotify",
        lambda **kwargs: mock_spotify_instance,
    )

    svc.config = {
        "client-id": "cid",
        "client-secret": "csecret",
        "scope": "user-library-read",
    }
    svc.login()

    assert svc.spotify is mock_spotify_instance
    assert svc._rate_limited is None


# ---------------------------------------------------------------------------
# _check_rate_limit
# ---------------------------------------------------------------------------


def test_check_rate_limit_no_cached_epoch(svc):
    """No cached rate-limit entry means no error."""
    svc._check_rate_limit("Spotify")


def test_check_rate_limit_expired(svc):
    """Expired rate-limit entry is deleted and does not raise."""
    svc.cache.write("rate_limit_until", time.time() - 10, ttl=999999)
    svc._check_rate_limit("Spotify")
    assert svc.cache.read_stale("rate_limit_until") is None


def test_check_rate_limit_active(svc):
    """Active rate-limit raises RuntimeError with time info."""
    future = time.time() + 7200
    svc.cache.write("rate_limit_until", future, ttl=999999)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc._check_rate_limit("Spotify")


# ---------------------------------------------------------------------------
# _acquire_token
# ---------------------------------------------------------------------------


def test_acquire_token_valid_cached(svc):
    """Valid non-expired token does not trigger refresh or browser."""
    creds = MagicMock()
    creds.cache_handler.get_cached_token.return_value = {"access_token": "t"}
    creds.validate_token.return_value = {"access_token": "t"}
    creds.is_token_expired.return_value = False
    svc._acquire_token(creds)
    creds.refresh_access_token.assert_not_called()
    creds.get_access_token.assert_not_called()


def test_acquire_token_expired_refresh_succeeds(svc):
    """Expired token with successful refresh does not prompt browser."""
    creds = MagicMock()
    creds.cache_handler.get_cached_token.return_value = {"refresh_token": "r"}
    creds.validate_token.return_value = {"refresh_token": "r"}
    creds.is_token_expired.return_value = True
    svc._acquire_token(creds)
    creds.refresh_access_token.assert_called_once_with("r")
    creds.get_access_token.assert_not_called()


def test_acquire_token_no_token_no_tty(svc, monkeypatch):
    """No token and no TTY raises RuntimeError."""
    creds = MagicMock()
    creds.cache_handler.get_cached_token.return_value = None
    creds.validate_token.return_value = None
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    with pytest.raises(RuntimeError, match="No valid Spotify token"):
        svc._acquire_token(creds)


def test_acquire_token_expired_refresh_fails_no_tty(svc, monkeypatch):
    """Token refresh fails without TTY raises RuntimeError."""
    creds = MagicMock()
    creds.cache_handler.get_cached_token.return_value = {"refresh_token": "r"}
    creds.validate_token.return_value = {"refresh_token": "r"}
    creds.is_token_expired.return_value = True
    creds.refresh_access_token.side_effect = Exception("refresh failed")
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
    with pytest.raises(RuntimeError, match="refresh failed"):
        svc._acquire_token(creds)


def test_acquire_token_expired_refresh_fails_with_tty(svc, monkeypatch):
    """Token refresh fails with TTY falls through to browser auth."""
    creds = MagicMock()
    creds.cache_handler.get_cached_token.return_value = {"refresh_token": "r"}
    creds.validate_token.return_value = {"refresh_token": "r"}
    creds.is_token_expired.return_value = True
    creds.refresh_access_token.side_effect = Exception("refresh failed")
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    svc._acquire_token(creds)
    creds.get_access_token.assert_called_once_with(as_dict=False)


def test_acquire_token_no_token_with_tty(svc, monkeypatch):
    """No token with TTY triggers browser auth."""
    creds = MagicMock()
    creds.cache_handler.get_cached_token.return_value = None
    creds.validate_token.return_value = None
    monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
    svc._acquire_token(creds)
    creds.get_access_token.assert_called_once_with(as_dict=False)


# ---------------------------------------------------------------------------
# _call
# ---------------------------------------------------------------------------


def test_call_rate_limited_before_lock(svc):
    """If already rate-limited, _call raises immediately."""
    svc._rate_limited = "Rate limited"
    with pytest.raises(RuntimeError, match="Rate limited"):
        svc._call(lambda: None)


def test_call_detects_429_with_retry_after(svc):
    """429 with Retry-After caches the rate limit and raises."""
    exc = spotipy.SpotifyException(429, -1, "rate limited")
    exc.http_status = 429
    exc.headers = {"Retry-After": "600"}

    def boom():
        raise exc

    with pytest.raises(RuntimeError, match="rate-limited"):
        svc._call(boom)

    assert svc._rate_limited is not None
    assert svc.cache.read_stale("rate_limit_until") is not None


def test_call_detects_429_in_message(svc):
    """429 detected via message string when http_status is missing."""
    exc = Exception("HTTP Error 429 Too Many Requests")

    def boom():
        raise exc

    with pytest.raises(RuntimeError, match="rate-limited"):
        svc._call(boom)

    assert svc._rate_limited is not None


def test_call_reraises_non_429(svc):
    """Non-rate-limit errors are re-raised unchanged."""
    exc = spotipy.SpotifyException(500, -1, "server error")
    exc.http_status = 500

    def boom():
        raise exc

    with pytest.raises(spotipy.SpotifyException):
        svc._call(boom)

    assert svc._rate_limited is None


def test_call_success(svc):
    """Successful call returns the method's return value."""
    result = svc._call(lambda: "ok")
    assert result == "ok"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def test_close_saves_cache(svc):
    svc.cache = MagicMock()
    svc.close()
    svc.cache.save.assert_called_once()


# ---------------------------------------------------------------------------
# get_artist (edge cases)
# ---------------------------------------------------------------------------


def test_get_artist_none_id_raises(svc):
    """Artist object with None id raises ValueError."""
    # Deliberately invalid: exercises the runtime guard, so the type error is the point.
    obj = Artist(None, "Name")  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="Artist ID is missing"):
        svc.get_artist(obj)


def test_get_artist_from_object_cache_miss_fetches_api(svc):
    """When Artist object is passed and cache misses, it uses the object (dict) directly."""
    svc.spotify.artist.return_value = _artist_payload("a2", "Direct Artist")
    artist = svc.get_artist("a2")
    assert artist.id == "a2"
    assert artist.name == "Direct Artist"
    svc.spotify.artist.assert_called_once_with("a2")


# ---------------------------------------------------------------------------
# get_artist_tracks
# ---------------------------------------------------------------------------


def test_get_artist_tracks_empty_albums(svc):
    """No albums means no tracks."""
    svc.cache.write("artist:a1:albums", [])
    svc.spotify.artist_albums.return_value = {"items": []}
    tracks = svc.get_artist_tracks(Artist("a1", "A"))
    assert tracks == []


def test_get_artist_tracks_collects_from_albums(svc):
    """Tracks from multiple albums are collected."""
    svc.cache.write(
        "artist:a1:albums",
        [_album_payload("alb1", "A1"), _album_payload("alb2", "A2")],
    )
    track1 = _track_payload("t1", "T1", album_id="alb1")
    track2 = _track_payload("t2", "T2", album_id="alb2")
    svc.spotify.album_tracks.side_effect = [
        {"items": [track1]},
        {"items": [track2]},
    ]
    svc.spotify.artist.return_value = _artist_payload()

    tracks = svc.get_artist_tracks(Artist("a1", "A"))
    assert len(tracks) == 2


def test_get_artist_tracks_album_error_skipped(svc):
    """An error fetching one album's tracks doesn't fail the whole call."""
    svc.cache.write(
        "artist:a1:albums",
        [_album_payload("alb1", "A1"), _album_payload("alb2", "A2")],
    )
    track2 = _track_payload("t2", "T2", album_id="alb2")
    svc.spotify.album_tracks.side_effect = [
        Exception("boom"),
        {"items": [track2]},
    ]
    svc.spotify.artist.return_value = _artist_payload()

    tracks = svc.get_artist_tracks(Artist("a1", "A"))
    assert len(tracks) == 1


# ---------------------------------------------------------------------------
# get_album_by_id
# ---------------------------------------------------------------------------


def test_get_album_by_id_cache_miss(svc):
    svc.spotify.album.return_value = _album_payload("alb1", "My Album")
    album = svc.get_album_by_id("alb1")
    assert album.id == "alb1"
    assert album.name == "My Album"
    svc.spotify.album.assert_called_once_with("alb1")


def test_get_album_by_id_cache_hit(svc):
    svc.cache.write("album:alb1", _album_payload("alb1", "Cached"))
    album = svc.get_album_by_id("alb1")
    assert album.name == "Cached"
    svc.spotify.album.assert_not_called()


# ---------------------------------------------------------------------------
# get_artist_albums (edge: fingerprint check fails)
# ---------------------------------------------------------------------------


def test_get_artist_albums_fingerprint_check_exception(svc):
    """If the fingerprint check API call fails, fall back to full refetch."""
    stale_albums = [_album_payload("alb1", "Old")]
    svc.cache.write("artist:a1:albums", stale_albums, ttl=60.0)
    svc.cache._conn.execute(
        "UPDATE cache SET stored_at = ? WHERE key = ?",
        (time.time() - 3600, "artist:a1:albums"),
    )
    svc.cache._conn.commit()

    svc.spotify.artist_albums.side_effect = [
        Exception("API error"),
        {"items": [_album_payload("alb1", "Refetched")]},
    ]

    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 1
    assert albums[0].name == "Refetched"
    assert svc.spotify.artist_albums.call_count == 2


def test_get_artist_albums_fingerprint_rate_limit_propagates(svc):
    """A 429 during the fingerprint check aborts instead of falling back."""
    stale_albums = [_album_payload("alb1", "Old")]
    svc.cache.write("artist:a1:albums", stale_albums, ttl=60.0)
    svc.cache._conn.execute(
        "UPDATE cache SET stored_at = ? WHERE key = ?",
        (time.time() - 3600, "artist:a1:albums"),
    )
    svc.cache._conn.commit()

    svc.spotify.artist_albums.side_effect = _rate_limit_exc({"Retry-After": "60"})

    with pytest.raises(RuntimeError, match="rate-limited"):
        svc.get_artist_albums(Artist("a1", "A"))
    # No second (full refetch) call — the run stopped at the rate limit.
    assert svc.spotify.artist_albums.call_count == 1


def test_get_artist_albums_none_response(svc):
    """artist_albums returning None yields empty list."""
    svc.spotify.artist_albums.return_value = None
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert albums == []


# ---------------------------------------------------------------------------
# get_playlist_id_for_name (pagination)
# ---------------------------------------------------------------------------


def test_get_playlist_id_for_name_paginated(svc):
    """Playlist found on the second page of results."""
    page1 = {"items": [{"id": f"pl{i}", "name": f"P{i}"} for i in range(50)]}
    page2 = {
        "items": [
            {"id": "target", "name": "Target Playlist"},
        ]
    }
    svc.spotify.current_user_playlists.side_effect = [page1, page2]
    assert svc.get_playlist_id_for_name("Target Playlist") == "target"


# ---------------------------------------------------------------------------
# sync edge cases
# ---------------------------------------------------------------------------


def test_sync_with_none_tracks(svc):
    """Passing None for tracks uses empty list."""
    svc.spotify.current_user_playlists.return_value = {"items": [{"id": "pl1", "name": "P"}]}
    svc.sync("P", None)
    svc.spotify.playlist_replace_items.assert_called_once_with("pl1", [])
    svc.spotify.playlist_add_items.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed responses (#59)
# ---------------------------------------------------------------------------


def test_get_album_tracks_rejects_a_non_list_items(svc):
    svc.spotify.album_tracks.return_value = {"items": {"not": "a list"}}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="items is not a list"):
        svc.get_album_tracks(album)


def test_get_album_tracks_rejects_a_non_object_entry(svc):
    svc.spotify.album_tracks.return_value = {"items": ["oops"]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="not an object"):
        svc.get_album_tracks(album)


def test_get_album_tracks_rejects_a_missing_track_id(svc):
    svc.spotify.album_tracks.return_value = {"items": [{"name": "T", "duration_ms": 100, "artists": []}]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="id is missing"):
        svc.get_album_tracks(album)


def test_get_album_tracks_rejects_a_non_numeric_duration(svc):
    svc.spotify.album_tracks.return_value = {
        "items": [{"id": "t1", "name": "T", "duration_ms": {"ms": 1}, "artists": []}]
    }
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="not a number"):
        svc.get_album_tracks(album)


def test_get_artist_top_tracks_rejects_a_non_list_tracks(svc):
    svc.spotify.artist_top_tracks.return_value = {"tracks": "nope"}
    artist = MagicMock(id="a1", name="Artist")
    with pytest.raises(ApiResponseError, match="tracks is not a list"):
        svc.get_artist_top_tracks(artist)


def test_get_artist_top_tracks_missing_tracks_is_empty(svc):
    svc.spotify.artist_top_tracks.return_value = {"other": []}
    artist = MagicMock(id="a1", name="Artist")
    assert svc.get_artist_top_tracks(artist) == []


def test_malformed_response_names_the_service(svc):
    svc.spotify.album_tracks.return_value = {"items": ["oops"]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="Spotify"):
        svc.get_album_tracks(album)
