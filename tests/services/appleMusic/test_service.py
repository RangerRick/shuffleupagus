"""Tests for AppleMusicService with all network/applescript calls mocked."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import applescript
import pytest
import requests

from shuffleupagus.core.apiresponse import ApiResponseError
from shuffleupagus.core.cache import Cache
from shuffleupagus.services.appleMusic.service import (
    AppleMusicService,
    _applescript_count,
    _applescript_str,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _artist_response(id="a1", name="Artist"):
    return {"data": [{"id": id, "attributes": {"name": name}}]}


def _album_response(id="alb1", name="Album", release_date="2022-01-01"):
    return {"data": [{"id": id, "attributes": {"name": name, "releaseDate": release_date}}]}


def _track_response(id="t1", name="Track", duration_ms=180_000, isrc="USRC00000001"):
    return {
        "data": [
            {
                "id": id,
                "attributes": {
                    "name": name,
                    "durationInMillis": duration_ms,
                    "isrc": isrc,
                },
            }
        ]
    }


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    s = AppleMusicService.__new__(AppleMusicService)
    cache = Cache("appleMusic")
    s.cache = cache
    s.config = {"media-user-token": "tok"}
    s.client = MagicMock()
    s.client.proxies = {}
    s.client.session_length = 30
    s.tag = "[apple] "
    yield s
    # Close the cache this fixture built, not s.cache — a test may have swapped
    # s.cache for a mock, which would orphan the real sqlite connection.
    cache.close()


# ---------------------------------------------------------------------------
# get_artist
# ---------------------------------------------------------------------------


def test_get_artist_cache_miss(svc):
    svc.client.artist.return_value = _artist_response("a1", "My Artist")
    artist = svc.get_artist("a1")
    assert artist.id == "a1"
    assert artist.name == "My Artist"
    svc.client.artist.assert_called_once_with("a1")


def test_get_artist_cache_hit(svc):
    svc.cache.write("artist:a1", _artist_response("a1", "Cached Artist"))
    artist = svc.get_artist("a1")
    assert artist.name == "Cached Artist"
    svc.client.artist.assert_not_called()


def test_get_artist_from_artist_object(svc):
    from shuffleupagus.core.model import Artist

    obj = Artist("a1", "Existing")
    svc.cache.write("artist:a1", _artist_response("a1", "Existing"))
    artist = svc.get_artist(obj)
    assert artist.name == "Existing"
    svc.client.artist.assert_not_called()


def test_get_artist_api_error_raises(svc):
    """A network error is a failure to find out, not an artist that is absent.

    Answering None dropped the artist from the playlist with nothing said.
    """
    svc.client.artist.side_effect = Exception("network error")
    with pytest.raises(RuntimeError, match="could not fetch artist"):
        svc.get_artist("a1")


def test_get_artist_empty_data_returns_none(svc):
    svc.client.artist.return_value = {"data": []}
    artist = svc.get_artist("a1")
    assert artist is None


def test_get_artist_sanitizes_url_id(svc):
    """A raw music.apple.com URL is sanitized before reaching the API client."""
    svc.client.artist.return_value = _artist_response("123456", "My Artist")
    artist = svc.get_artist("https://music.apple.com/us/artist/my-artist/123456")
    assert artist.id == "123456"
    svc.client.artist.assert_called_once_with("123456")


# ---------------------------------------------------------------------------
# fatal (auth/rate-limit) errors abort the run instead of being swallowed
# ---------------------------------------------------------------------------


def _http_error(status_code, headers=None):
    """Build the HTTPError applemusicpy raises once its own retries are exhausted."""
    resp = MagicMock()
    resp.status_code = status_code
    # A real dict, not a MagicMock: int(MagicMock()) is 1, which would silently
    # fake a Retry-After of one second on every 429 test.
    resp.headers = {} if headers is None else headers
    return requests.exceptions.HTTPError(f"HTTP {status_code}", response=resp)


@pytest.mark.parametrize("status_code", [401, 403])
def test_get_artist_auth_error_raises(svc, status_code):
    svc.client.artist.side_effect = _http_error(status_code)
    with pytest.raises(RuntimeError, match=str(status_code)):
        svc.get_artist("a1")


def test_get_artist_rate_limit_raises(svc):
    svc.client.artist.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc.get_artist("a1")


def test_get_album_by_id_fatal_error_raises(svc):
    svc.client.album.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc.get_album_by_id("alb1")


def test_get_artist_albums_fatal_error_raises(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc.get_artist_albums(Artist("a1", "A"))


def test_get_album_tracks_fatal_error_raises(svc):
    from shuffleupagus.core.model import Album

    svc.client.album_relationship.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc.get_album_tracks(Album("alb1", "A"))


def test_get_artist_top_tracks_fatal_error_raises(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship_view.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc.get_artist_top_tracks(Artist("a1", "A"))


def test_rate_limit_honours_retry_after(svc):
    """A Retry-After header is turned into a countdown and a cached window."""
    from shuffleupagus.core.model import Service

    svc.client.artist.side_effect = _http_error(429, {"Retry-After": "3661"})
    with pytest.raises(RuntimeError, match="1h 1m from now"):
        svc.get_artist("a1")
    assert svc.cache.read_stale(Service._RATE_LIMIT_CACHE_KEY) > time.time()


def test_rate_limit_without_retry_after_does_not_cache(svc):
    """Without Retry-After there is no known window, so nothing is persisted."""
    from shuffleupagus.core.model import Service

    svc.client.artist.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="no Retry-After header"):
        svc.get_artist("a1")
    assert svc.cache.read_stale(Service._RATE_LIMIT_CACHE_KEY) is None


def test_rate_limit_ignores_garbage_retry_after(svc):
    """A non-numeric Retry-After is treated as absent, not crashed on."""
    svc.client.artist.side_effect = _http_error(429, {"Retry-After": "soon"})
    with pytest.raises(RuntimeError, match="no Retry-After header"):
        svc.get_artist("a1")


# ---------------------------------------------------------------------------
# get_album_by_id
# ---------------------------------------------------------------------------


def test_get_album_by_id_cache_miss(svc):
    svc.client.album.return_value = _album_response("alb1", "Test Album")
    album = svc.get_album_by_id("alb1")
    assert album.id == "alb1"
    assert album.name == "Test Album"


def test_get_album_by_id_cache_hit(svc):
    svc.cache.write("album:alb1", _album_response("alb1", "Cached Album"))
    album = svc.get_album_by_id("alb1")
    assert album.name == "Cached Album"
    svc.client.album.assert_not_called()


def test_get_album_by_id_error_raises(svc):
    svc.client.album.side_effect = Exception("api error")
    with pytest.raises(RuntimeError, match="could not fetch album"):
        svc.get_album_by_id("alb1")


# ---------------------------------------------------------------------------
# get_artist_albums
# ---------------------------------------------------------------------------


def test_get_artist_albums(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship.return_value = {
        "data": [
            {"id": "alb1", "attributes": {"name": "Album One", "releaseDate": "2021-01-01"}},
            {"id": "alb2", "attributes": {"name": "Album Two", "releaseDate": "2022-01-01"}},
        ]
    }
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 2
    assert albums[0].name == "Album One"
    svc.client.artist_relationship.assert_called_once_with("a1", "albums")


def test_get_artist_albums_cache_hit(svc):
    from shuffleupagus.core.model import Artist

    svc.cache.write(
        "artist:a1:albums", {"data": [{"id": "alb1", "attributes": {"name": "Cached", "releaseDate": "2020-01-01"}}]}
    )
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert len(albums) == 1
    svc.client.artist_relationship.assert_not_called()


def test_get_artist_albums_empty(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship.return_value = {"data": []}
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert albums == []


def test_get_artist_albums_error_raises(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship.side_effect = Exception("error")
    with pytest.raises(RuntimeError, match="could not fetch artist albums"):
        svc.get_artist_albums(Artist("a1", "A"))


# ---------------------------------------------------------------------------
# get_album_tracks
# ---------------------------------------------------------------------------


def test_get_album_tracks(svc):
    from shuffleupagus.core.model import Album

    svc.client.album_relationship.return_value = {
        "data": [
            {"id": "t1", "attributes": {"name": "Track 1", "durationInMillis": 200_000, "isrc": "US001"}},
            {"id": "t2", "attributes": {"name": "Track 2", "durationInMillis": 180_000, "isrc": "US002"}},
        ]
    }
    album = Album("alb1", "Album")
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 2
    assert tracks[0].id == "t1"
    assert tracks[0].duration_ms == 200_000
    assert tracks[0].album.id == "alb1"


def test_get_album_tracks_cache_hit(svc):
    from shuffleupagus.core.model import Album

    svc.cache.write(
        "album:alb1:tracks",
        {"data": [{"id": "t1", "attributes": {"name": "T", "durationInMillis": 100_000, "isrc": "US001"}}]},
    )
    tracks = svc.get_album_tracks(Album("alb1", "A"))
    assert len(tracks) == 1
    svc.client.album_relationship.assert_not_called()


def test_get_album_tracks_empty(svc):
    from shuffleupagus.core.model import Album

    svc.client.album_relationship.return_value = {"data": []}
    tracks = svc.get_album_tracks(Album("alb1", "A"))
    assert tracks == []


def test_get_album_tracks_with_artist(svc):
    from shuffleupagus.core.model import Album, Artist

    svc.client.album_relationship.return_value = {
        "data": [{"id": "t1", "attributes": {"name": "T", "durationInMillis": 100_000, "isrc": "US001"}}]
    }
    album = Album("alb1", "Album")
    artist = Artist("a1", "Artist")
    tracks = svc.get_album_tracks(album, artist)
    assert tracks[0].artists[0].id == "a1"


# ---------------------------------------------------------------------------
# _get_track_by_id
# ---------------------------------------------------------------------------


def test_get_track_by_id_cache_miss(svc):
    svc.client.song.return_value = _track_response("t1", "My Track")
    track = svc._get_track_by_id("t1")
    assert track.id == "t1"
    assert track.name == "My Track"


def test_get_track_by_id_sanitizes_id(svc):
    svc.client.song.return_value = _track_response("t1", "My Track")
    svc._get_track_by_id("https://music.apple.com/us/song/my-track/t1")
    svc.client.song.assert_called_once_with("t1")


def test_get_track_by_id_error_raises(svc):
    svc.client.song.side_effect = Exception("network error")
    with pytest.raises(RuntimeError, match="could not fetch track"):
        svc._get_track_by_id("t1")


def test_get_track_by_id_fatal_error_raises(svc):
    svc.client.song.side_effect = _http_error(429)
    with pytest.raises(RuntimeError, match="rate-limited"):
        svc._get_track_by_id("t1")


def test_get_track_by_id_empty_data_returns_none(svc):
    svc.client.song.return_value = {"data": []}
    track = svc._get_track_by_id("t1")
    assert track is None


# ---------------------------------------------------------------------------
# get_artist_top_tracks
# ---------------------------------------------------------------------------


def test_get_artist_top_tracks(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship_view.return_value = {"data": [{"id": "t1"}, {"id": "t2"}]}
    svc.client.song.side_effect = [_track_response("t1", "Song 1"), _track_response("t2", "Song 2")]
    tracks = svc.get_artist_top_tracks(Artist("a1", "A"))
    assert len(tracks) == 2
    assert tracks[0].name == "Song 1"


def test_get_artist_top_tracks_cache_hit(svc):
    from shuffleupagus.core.model import Artist

    svc.cache.write("top-tracks:a1", {"data": [{"id": "t1"}]})
    svc.client.song.return_value = _track_response("t1", "Cached Song")
    tracks = svc.get_artist_top_tracks(Artist("a1", "A"))
    assert len(tracks) == 1
    svc.client.artist_relationship_view.assert_not_called()


def test_get_artist_top_tracks_empty(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship_view.return_value = {"data": []}
    tracks = svc.get_artist_top_tracks(Artist("a1", "A"))
    assert tracks == []


def test_get_artist_top_tracks_error_raises(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship_view.side_effect = Exception("error")
    with pytest.raises(RuntimeError, match="could not fetch top tracks"):
        svc.get_artist_top_tracks(Artist("a1", "A"))


# ---------------------------------------------------------------------------
# get_playlist_id_for_name
# ---------------------------------------------------------------------------


def _mock_session_get(svc, data_list=None, status_code=200, error_json=None):
    resp = MagicMock()
    resp.status_code = status_code
    if data_list is not None:
        resp.json.return_value = {"data": data_list}
    elif error_json is not None:
        resp.json.return_value = error_json
    resp.text = ""
    svc.client._session.get.return_value = resp
    svc.client._auth_headers.return_value = {}
    return resp


def test_get_playlist_id_for_name_found(svc):
    _mock_session_get(
        svc,
        [
            {"id": "pl1", "attributes": {"name": "My Playlist"}},
            {"id": "pl2", "attributes": {"name": "Other"}},
        ],
    )
    assert svc.get_playlist_id_for_name("My Playlist") == "pl1"


def test_get_playlist_id_for_name_not_found(svc):
    _mock_session_get(svc, [{"id": "pl1", "attributes": {"name": "Other"}}])
    with pytest.raises(Exception, match="Failed to fetch playlist"):
        svc.get_playlist_id_for_name("Missing")


def test_get_playlist_id_for_name_missing_data_key_names_the_field(svc):
    """A response without 'data' must not surface as a bare KeyError."""
    _mock_session_get(svc, error_json={"errors": [{"detail": "nope"}]})
    with pytest.raises(ValueError) as caught:
        svc.get_playlist_id_for_name("My Playlist")
    message = str(caught.value)
    assert "Apple Music" in message
    assert "data" in message
    assert "missing" in message


@pytest.mark.parametrize("data", [{"not": "a list"}, "text", 3])
def test_get_playlist_id_for_name_wrong_typed_data_is_reported(svc, data):
    _mock_session_get(svc, error_json={"data": data})
    with pytest.raises(ValueError, match="not a list"):
        svc.get_playlist_id_for_name("My Playlist")


def test_get_playlist_id_for_name_entry_without_id_names_the_field(svc):
    _mock_session_get(svc, [{"attributes": {"name": "My Playlist"}}])
    with pytest.raises(ValueError) as caught:
        svc.get_playlist_id_for_name("My Playlist")
    assert "id" in str(caught.value)
    assert "missing" in str(caught.value)


@pytest.mark.parametrize("entry", ["not an object", 42, None, ["nested"]])
def test_get_playlist_id_for_name_non_object_entry_is_reported(svc, entry):
    """A malformed entry must not degrade into a misleading 'not found' after retries."""
    _mock_session_get(svc, [entry])
    with pytest.raises(ValueError) as caught:
        svc.get_playlist_id_for_name("My Playlist")
    message = str(caught.value)
    assert "Apple Music" in message
    assert "not an object" in message


def test_get_playlist_length_2xx_without_meta_names_the_field(svc):
    """A success body missing meta is a shape problem, not something to retry."""
    _mock_session_get(svc, error_json={"data": []})
    with pytest.raises(ValueError) as caught:
        svc._AppleMusicService__get_playlist_length("pl1")
    message = str(caught.value)
    assert "Apple Music" in message
    assert "meta.total" in message
    assert "missing" in message


def test_get_playlist_length_non_numeric_total_names_the_field(svc):
    _mock_session_get(svc, error_json={"meta": {"total": "many"}})
    with pytest.raises(ValueError) as caught:
        svc._AppleMusicService__get_playlist_length("pl1")
    message = str(caught.value)
    assert "Apple Music" in message
    assert "meta.total" in message
    assert "not a number" in message


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _mock_applescripts():
    """Create AppleScript mock that returns None for delete, 0 for count."""
    scripts = []

    def make_script(*_args, **_kwargs):
        mock = MagicMock()
        scripts.append(mock)
        # First script is the delete (returns None), second is the count (returns 0)
        mock.run.return_value = None if len(scripts) == 1 else 0
        return mock

    return make_script


def _stub_get_playlist_tracks(svc, return_values):
    """Patch __get_playlist_tracks to return successive values."""
    it = iter(return_values)
    svc._AppleMusicService__get_playlist_tracks = MagicMock(side_effect=lambda _pid: next(it))


def _stub_get_playlist_length(svc, value=0):
    """Patch __get_playlist_length to return a fixed value."""
    svc._AppleMusicService__get_playlist_length = MagicMock(return_value=value)


def _stub_get_playlist_length_seq(svc, return_values):
    """Patch __get_playlist_length to return successive values."""
    it = iter(return_values)
    svc._AppleMusicService__get_playlist_length = MagicMock(side_effect=lambda _pid: next(it))


def test_sync(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    # sync reads current (empty), cloud count confirms 3 after batch
    _stub_get_playlist_tracks(svc, [[]])
    _stub_get_playlist_length_seq(svc, [3])

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp
    svc.client._auth_headers.return_value = {}

    with patch("shuffleupagus.services.appleMusic.service.time.sleep"):
        svc.sync("Playlist", ["t1", "t2", "t3"])

    svc.client._session.post.assert_called()
    # One post call for 3 tracks (all in single batch)
    assert svc.client._session.post.call_count == 1


def test_sync_batches_tracks(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    tracks = [f"t{i}" for i in range(90)]
    # sync reads current (empty), cloud count confirms 80 then 90
    _stub_get_playlist_tracks(svc, [[]])
    _stub_get_playlist_length_seq(svc, [80, 90])

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp
    svc.client._auth_headers.return_value = {}

    with patch("shuffleupagus.services.appleMusic.service.time.sleep"):
        svc.sync("Playlist", tracks)

    # 90 tracks → 2 batches of 80 and 10
    assert svc.client._session.post.call_count == 2


def test_sync_skips_when_unchanged(svc):
    """When playlist already matches desired, no AppleScript or POST calls."""
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    _stub_get_playlist_tracks(svc, [["t1", "t2", "t3"]])
    svc.client._auth_headers.return_value = {}

    svc.sync("Playlist", ["t1", "t2", "t3"])

    svc.client._session.post.assert_not_called()


def test_sync_clears_and_waits_for_cloud(svc):
    """When playlist is non-empty but different, clear + wait for cloud."""
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    # First read: stale tracks; deletion poll returns 0; batch verify returns 2
    _stub_get_playlist_tracks(svc, [["old1"]])
    _stub_get_playlist_length_seq(svc, [0, 2])

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp
    svc.client._auth_headers.return_value = {}

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=_mock_applescripts(),
        ),
        patch("shuffleupagus.services.appleMusic.service.time.sleep"),
    ):
        svc.sync("Playlist", ["t1", "t2"])

    svc.client._session.post.assert_called()


def test_sync_aborts_when_cloud_deletion_stalls(svc):
    """If cloud never reports 0 tracks after deletion, abort with error."""
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    _stub_get_playlist_tracks(svc, [["old1"]])
    # Cloud always returns 500 tracks — deletion never completes
    _stub_get_playlist_length(svc, 500)

    svc.client._auth_headers.return_value = {}

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=_mock_applescripts(),
        ),
        patch("shuffleupagus.services.appleMusic.service.time.sleep"),
        pytest.raises(RuntimeError, match="Cloud still reports 500 tracks"),
    ):
        svc.sync("Playlist", ["t1", "t2"])

    # Should not have attempted to add any tracks
    svc.client._session.post.assert_not_called()


def test_sync_verify_retries_missing_in_batch(svc):
    """Cloud count is short, cloud API identifies missing, retry adds them."""
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    # sync reads current: empty (no clear)
    # cloud count returns 2 (short), cloud tracks return [t1, t2]
    # retry adds t3, cloud count returns 3 (good)
    _stub_get_playlist_tracks(svc, [[], ["t1", "t2"]])
    _stub_get_playlist_length_seq(svc, [2, 3])

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp
    svc.client._auth_headers.return_value = {}

    with patch("shuffleupagus.services.appleMusic.service.time.sleep"):
        svc.sync("Playlist", ["t1", "t2", "t3"])

    # Initial batch + retry of missing t3
    assert svc.client._session.post.call_count == 2


# ---------------------------------------------------------------------------
# __get_playlist_tracks
# ---------------------------------------------------------------------------


def test_get_playlist_tracks_pagination(svc):
    """Follow pagination to collect all catalog IDs."""
    svc.client._auth_headers.return_value = {}

    page1 = MagicMock()
    page1.status_code = 200
    page1.json.return_value = {
        "data": [
            {"attributes": {"playParams": {"catalogId": "c1"}}},
            {"attributes": {"playParams": {"catalogId": "c2"}}},
        ],
        "next": "/v1/me/library/playlists/pl1/tracks?offset=2",
    }
    page2 = MagicMock()
    page2.status_code = 200
    page2.json.return_value = {
        "data": [
            {"attributes": {"playParams": {"catalogId": "c3"}}},
        ],
    }
    svc.client._session.get.side_effect = [page1, page2]

    result = svc._AppleMusicService__get_playlist_tracks("pl1")
    assert result == ["c1", "c2", "c3"]
    assert svc.client._session.get.call_count == 2


def test_get_playlist_tracks_empty_404(svc):
    """API returns 404 with error 40403 for empty playlists."""
    svc.client._auth_headers.return_value = {}

    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = {"errors": [{"code": "40403", "title": "No related resources"}]}
    svc.client._session.get.return_value = resp

    result = svc._AppleMusicService__get_playlist_tracks("pl1")
    assert result == []


def test_get_playlist_tracks_404_unknown_error(svc):
    """A 404 without error code 40403 raises RuntimeError."""
    svc.client._auth_headers.return_value = {}

    resp = MagicMock()
    resp.status_code = 404
    resp.json.return_value = {"errors": [{"code": "40404", "title": "Not Found"}]}
    svc.client._session.get.return_value = resp

    with pytest.raises(RuntimeError, match="Failed to read playlist tracks: 404"):
        svc._AppleMusicService__get_playlist_tracks("pl1")


def test_get_playlist_tracks_server_error(svc):
    """A non-2xx, non-404 response raises RuntimeError."""
    svc.client._auth_headers.return_value = {}

    resp = MagicMock()
    resp.status_code = 500
    svc.client._session.get.return_value = resp

    with pytest.raises(RuntimeError, match="Failed to read playlist tracks: 500"):
        svc._AppleMusicService__get_playlist_tracks("pl1")


# ---------------------------------------------------------------------------
# __post_batch
# ---------------------------------------------------------------------------


def test_post_batch_retries_then_raises(svc):
    """__post_batch raises after 3 failed attempts."""
    svc.client._auth_headers.return_value = {}

    resp = MagicMock()
    resp.status_code = 500
    resp.reason = "Internal Server Error"
    resp.text = "error body"
    svc.client._session.post.return_value = resp

    with pytest.raises(RuntimeError, match="Failed to add tracks after 3 retries"):
        svc._AppleMusicService__post_batch("pl1", ["t1"])

    assert svc.client._session.post.call_count == 3


# ---------------------------------------------------------------------------
# __clear_playlist
# ---------------------------------------------------------------------------


def test_clear_playlist_polls_until_zero(svc):
    """__clear_playlist polls local Music.app until count reaches 0."""
    counts = [100, 50, 0]
    scripts = []

    def make_script(*_args, **_kwargs):
        mock = MagicMock()
        scripts.append(mock)
        if len(scripts) == 1:
            # Delete script
            mock.run.return_value = None
        else:
            # Count script — return successive values
            it = iter(counts)
            mock.run.side_effect = lambda: next(it)
        return mock

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=make_script,
        ),
        patch("shuffleupagus.services.appleMusic.service.time.sleep"),
    ):
        svc._AppleMusicService__clear_playlist("Test")

    # Count script polled 3 times: 100, 50, 0
    assert scripts[1].run.call_count == 3


def _script_error(number, message="AppleScript failed"):
    return applescript.ScriptError({"NSAppleScriptErrorNumber": number, "NSAppleScriptErrorMessage": message})


def test_clear_playlist_denied_automation_names_the_setting(svc):
    """errAEEventNotPermitted is the common first-run failure and needs a pointer."""
    err = _script_error(-1743, "Not authorized to send Apple events to Music.")
    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=lambda *_a, **_k: MagicMock(run=MagicMock(side_effect=err)),
        ),
        pytest.raises(RuntimeError) as caught,
    ):
        svc._AppleMusicService__clear_playlist("My Mix")
    message = str(caught.value)
    assert "My Mix" in message
    assert "Automation" in message
    assert "Privacy" in message


def test_clear_playlist_missing_playlist_does_not_blame_permissions(svc):
    """A -1728 is a missing playlist, so the Automation advice would be misleading."""
    err = _script_error(-1728, 'Can\'t get playlist "Ghost".')
    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=lambda *_a, **_k: MagicMock(run=MagicMock(side_effect=err)),
        ),
        pytest.raises(RuntimeError) as caught,
    ):
        svc._AppleMusicService__clear_playlist("Ghost")
    message = str(caught.value)
    assert "Ghost" in message
    assert "Automation" not in message


def test_clear_playlist_count_script_failure_is_also_reported(svc):
    """The delete can succeed and the count script still fail."""
    scripts = []

    def make_script(*_a, **_k):
        mock = MagicMock()
        scripts.append(mock)
        if len(scripts) == 1:
            mock.run.return_value = None
        else:
            mock.run.side_effect = _script_error(-1743, "Not authorized.")
        return mock

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=make_script,
        ),
        pytest.raises(RuntimeError, match="Automation"),
    ):
        svc._AppleMusicService__clear_playlist("My Mix")


def test_clear_playlist_gives_up_when_count_never_drops(svc):
    """A count that never reaches zero raises instead of polling forever."""
    scripts = []

    def make_script(*_args, **_kwargs):
        mock = MagicMock()
        scripts.append(mock)
        mock.run.return_value = None if len(scripts) == 1 else 7
        return mock

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=make_script,
        ),
        patch("shuffleupagus.services.appleMusic.service.time.sleep"),
        pytest.raises(RuntimeError, match="still reports 7 tracks"),
    ):
        svc._AppleMusicService__clear_playlist("Test")


# ---------------------------------------------------------------------------
# __add_tracks re-queue
# ---------------------------------------------------------------------------


def test_add_tracks_requeues_missing_after_all_retries(svc):
    """When verification fails all 3 attempts, missing tracks are re-queued."""
    svc.client._auth_headers.return_value = {}

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp

    # Cloud count never matches: always returns 2 instead of 3
    # Cloud track list always returns [t1, t2] (t3 missing)
    # After re-queue, cloud count returns 3 (t3 re-added successfully)
    svc._AppleMusicService__get_playlist_length = MagicMock(side_effect=[2, 2, 2, 2, 3])
    svc._AppleMusicService__get_playlist_tracks = MagicMock(
        side_effect=[
            ["t1", "t2"],  # verify attempt 1
            ["t1", "t2"],  # verify attempt 2
            ["t1", "t2"],  # verify attempt 3
            ["t1", "t2"],  # final fallback check
            ["t1", "t2", "t3"],  # re-queued batch verify
        ]
    )

    with patch("shuffleupagus.services.appleMusic.service.time.sleep"):
        svc._AppleMusicService__add_tracks("pl1", ["t1", "t2", "t3"])

    # 1 initial batch + 3 retries of t3 + 1 final fallback retry + 1 re-queued batch
    assert svc.client._session.post.call_count >= 5


def test_add_tracks_raises_when_requeue_rounds_exhausted(svc):
    """A track that never verifies raises instead of re-queuing forever."""
    svc.client._auth_headers.return_value = {}

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp

    # Cloud never reflects t2: count is always 1, list is always [t1].
    svc._AppleMusicService__get_playlist_length = MagicMock(return_value=1)
    svc._AppleMusicService__get_playlist_tracks = MagicMock(return_value=["t1"])

    with (
        patch("shuffleupagus.services.appleMusic.service.time.sleep"),
        pytest.raises(RuntimeError, match="could not be verified"),
    ):
        svc._AppleMusicService__add_tracks("pl1", ["t1", "t2"])


def test_sync_empty_tracks_skips(svc):
    """sync() with empty track list logs warning and returns."""
    svc.sync("Playlist", [])
    svc.sync("Playlist", None)
    # No API calls should have been made
    svc.client._session.post.assert_not_called()
    svc.client._session.get.assert_not_called()


# ---------------------------------------------------------------------------
# AppleScript string escaping
# ---------------------------------------------------------------------------


def test_applescript_str_escapes_quotes_and_backslashes():
    assert _applescript_str('My "Best" Mix') == 'My \\"Best\\" Mix'
    assert _applescript_str("back\\slash") == "back\\\\slash"
    # Backslash is doubled before the quote is escaped, so the quote's escape
    # character is not itself escaped.
    assert _applescript_str('a\\"b') == 'a\\\\\\"b'


def test_applescript_str_leaves_plain_text_alone():
    assert _applescript_str("Shuffleupagus Test") == "Shuffleupagus Test"


@pytest.mark.parametrize("bad", ["two\nlines", "carriage\rreturn"])
def test_applescript_str_rejects_line_breaks(bad):
    with pytest.raises(ValueError, match="line break"):
        _applescript_str(bad)


@pytest.mark.parametrize("raw, expected", [(7, 7), (7.0, 7), (0, 0)])
def test_applescript_count_accepts_numeric_results(raw, expected):
    assert _applescript_count(raw) == expected


@pytest.mark.parametrize("bad", [None, {"count": 3}, [1, 2], "7", True])
def test_applescript_count_rejects_non_numeric_results(bad):
    with pytest.raises(TypeError, match="track count"):
        _applescript_count(bad)


def test_clear_playlist_escapes_quoted_name(svc):
    """A playlist name with a quote produces a valid script, not a broken one."""
    scripts = []

    def make_script(source, *_args, **_kwargs):
        scripts.append(source)
        mock = MagicMock()
        mock.run.return_value = 0
        return mock

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.applescript.AppleScript",
            side_effect=make_script,
        ),
        patch("shuffleupagus.services.appleMusic.service.time.sleep"),
    ):
        svc._AppleMusicService__clear_playlist('My "Best" Mix')

    assert scripts, "no AppleScript was built"
    for source in scripts:
        assert 'playlist "My \\"Best\\" Mix"' in source
        # The raw, unescaped form must not appear — that is the broken script.
        assert 'playlist "My "Best" Mix"' not in source


# ---------------------------------------------------------------------------
# Malformed responses (#59)
# ---------------------------------------------------------------------------


def test_get_artist_rejects_a_non_list_data(svc):
    svc.client.artist.return_value = {"data": {"not": "a list"}}
    with pytest.raises(ApiResponseError, match="data is not a list"):
        svc.get_artist("a1")


def test_get_artist_rejects_a_missing_data(svc):
    svc.client.artist.return_value = {"results": []}
    with pytest.raises(ApiResponseError, match="data is missing"):
        svc.get_artist("a1")


def test_get_artist_rejects_a_non_object_entry(svc):
    svc.client.artist.return_value = {"data": ["just a string"]}
    with pytest.raises(ApiResponseError, match="not an object"):
        svc.get_artist("a1")


def test_get_artist_empty_data_is_not_an_error(svc):
    svc.client.artist.return_value = {"data": []}
    assert svc.get_artist("a1") is None


def test_get_album_by_id_rejects_a_non_list_data(svc):
    svc.client.album.return_value = {"data": "nope"}
    with pytest.raises(ApiResponseError, match="data is not a list"):
        svc.get_album_by_id("alb1")


def test_get_artist_albums_rejects_a_non_list_data(svc):
    svc.client.artist_relationship.return_value = {"data": {"nope": True}}
    artist = MagicMock(id="a1", name="Artist")
    with pytest.raises(ApiResponseError, match="data is not a list"):
        svc.get_artist_albums(artist)


def test_get_artist_albums_rejects_a_non_object_response(svc):
    svc.client.artist_relationship.return_value = ["not", "an", "object"]
    artist = MagicMock(id="a1", name="Artist")
    with pytest.raises(ApiResponseError, match="response is not an object"):
        svc.get_artist_albums(artist)


def test_get_album_tracks_rejects_a_non_object_entry(svc):
    svc.client.album_relationship.return_value = {"data": ["oops"]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="not an object"):
        svc.get_album_tracks(album)


def test_malformed_response_names_the_service(svc):
    svc.client.artist.return_value = {"data": {"not": "a list"}}
    with pytest.raises(ApiResponseError, match="Apple Music"):
        svc.get_artist("a1")


def test_get_artist_albums_rejects_a_missing_data(svc):
    """An empty relationship answers {"data": []}, so an absent data is malformed.

    get_artist has always raised on this; the three now agree.
    """
    svc.client.artist_relationship.return_value = {"meta": {}}
    artist = MagicMock(id="a1", name="Artist")
    with pytest.raises(ApiResponseError, match="data is missing"):
        svc.get_artist_albums(artist)


def test_get_album_tracks_rejects_a_missing_data(svc):
    svc.client.album_relationship.return_value = {"meta": {}}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="data is missing"):
        svc.get_album_tracks(album)


def test_get_artist_top_tracks_rejects_a_missing_data(svc):
    svc.client.artist_relationship_view.return_value = {"meta": {}}
    artist = MagicMock(id="a1", name="Artist")
    with pytest.raises(ApiResponseError, match="data is missing"):
        svc.get_artist_top_tracks(artist)


def test_get_artist_albums_empty_data_is_not_an_error(svc):
    """A relationship with nothing in it is a legitimate empty answer."""
    svc.client.artist_relationship.return_value = {"data": []}
    artist = MagicMock(id="a1", name="Artist")
    assert svc.get_artist_albums(artist) == []


def test_get_artist_albums_failed_fetch_is_still_empty(svc):
    """A logged fetch failure leaves ret None and still answers empty."""
    svc.client.artist_relationship.return_value = None
    artist = MagicMock(id="a1", name="Artist")
    assert svc.get_artist_albums(artist) == []


# ---------------------------------------------------------------------------
# A failed fetch is not a missing artist (#73)
# ---------------------------------------------------------------------------


def test_a_404_still_means_not_found(svc):
    """The one status that genuinely means the thing is not there."""
    svc.client.artist.side_effect = _http_error(404)
    assert svc.get_artist("a1") is None


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_error_is_not_a_missing_artist(svc, status):
    """Answering None here silently drops the artist from the playlist."""
    svc.client.artist.side_effect = _http_error(status)
    with pytest.raises(RuntimeError, match="artist"):
        svc.get_artist("a1")


def test_a_network_error_is_not_a_missing_artist(svc):
    """No response at all, so no status to inspect."""
    svc.client.artist.side_effect = requests.exceptions.ConnectionError("connection reset")
    with pytest.raises(RuntimeError, match="artist"):
        svc.get_artist("a1")


def test_a_failed_album_fetch_raises(svc):
    svc.client.album.side_effect = _http_error(503)
    with pytest.raises(RuntimeError, match="album"):
        svc.get_album_by_id("alb1")


def test_a_failed_album_list_raises(svc):
    svc.client.artist_relationship.side_effect = _http_error(503)
    with pytest.raises(RuntimeError):
        svc.get_artist_albums(MagicMock(id="a1", name="Artist"))


def test_a_missing_album_list_is_still_empty(svc):
    svc.client.artist_relationship.side_effect = _http_error(404)
    assert svc.get_artist_albums(MagicMock(id="a1", name="Artist")) == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_still_abort(svc, status):
    svc.client.artist.side_effect = _http_error(status)
    with pytest.raises(RuntimeError, match="Apple Music API error"):
        svc.get_artist("a1")


def test_the_message_names_the_artist(svc):
    svc.client.artist.side_effect = _http_error(500)
    with pytest.raises(RuntimeError) as excinfo:
        svc.get_artist("a1")
    assert "a1" in str(excinfo.value)


def test_get_artist_tracks_does_not_swallow_the_abort_signal(svc):
    """RuntimeError is the convention's "abort this service" signal.

    The album pool caught Exception and logged, so a fetch failure raised by
    _absent_or_raise was turned straight back into a silently skipped album.
    """
    album = MagicMock(id="alb1", name="Album")
    svc.get_artist_albums = MagicMock(return_value=[album])
    svc.get_album_tracks = MagicMock(side_effect=RuntimeError("Apple Music could not fetch album tracks"))
    svc._album_pool = ThreadPoolExecutor(max_workers=1)
    with pytest.raises(RuntimeError, match="could not fetch album tracks"):
        svc.get_artist_tracks(MagicMock(id="a1", name="Artist"))


def test_get_artist_tracks_still_skips_an_unexpected_error(svc):
    """Anything that is not the abort signal keeps the per-album skip."""
    album = MagicMock(id="alb1", name="Album")
    svc.get_artist_albums = MagicMock(return_value=[album])
    svc.get_album_tracks = MagicMock(side_effect=ValueError("malformed album response"))
    svc._album_pool = ThreadPoolExecutor(max_workers=1)
    assert svc.get_artist_tracks(MagicMock(id="a1", name="Artist")) == []


def test_a_fetch_failure_message_is_bounded(svc):
    """`which` is built from API response data at half the call sites."""
    svc.client.artist.side_effect = Exception("y" * 5000)
    with pytest.raises(RuntimeError) as excinfo:
        svc.get_artist("x" * 5000)
    assert len(str(excinfo.value)) < 500


def test_get_artist_tracks_cancels_queued_work_when_aborting(svc):
    """Aborting after a rate limit must not leave queued calls hitting the API.

    One worker and six albums: the first raises, the second blocks, and the
    remaining four are still queued when the abort fires. Those four are the
    ones cancel() can actually stop, and asserting on them is deterministic —
    asserting on a call count would race the pool.
    """
    albums = [MagicMock(id=f"alb{i}", name=f"Album {i}") for i in range(6)]
    svc.get_artist_albums = MagicMock(return_value=albums)
    release = threading.Event()

    def _fetch(album, _artist=None):
        if album is albums[0]:
            raise RuntimeError("Apple Music rate-limited")
        release.wait(timeout=5)
        return []

    svc.get_album_tracks = MagicMock(side_effect=_fetch)

    submitted = []
    pool = ThreadPoolExecutor(max_workers=1)

    class _RecordingPool:
        def submit(self, fn, *args, **kwargs):
            future = pool.submit(fn, *args, **kwargs)
            submitted.append(future)
            return future

    svc._album_pool = _RecordingPool()
    try:
        with pytest.raises(RuntimeError, match="rate-limited"):
            svc.get_artist_tracks(MagicMock(id="a1", name="Artist"))
        assert any(future.cancelled() for future in submitted), "no queued work was cancelled"
    finally:
        release.set()
        pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# login: the secret-key filename stays inside the config directory (#76)
# ---------------------------------------------------------------------------


def test_login_refuses_a_secret_key_that_leaves_the_config_dir(svc):
    """`secret-key` is user-supplied config, so it gets the same guard auth-file has.

    YouTube already routes its equivalent through get_filepath; this one built
    the path with os.path.join and never checked where it landed.
    """
    svc.config = {"secret-key": "../../.ssh/id_rsa", "key-id": "k", "team-id": "t"}
    with pytest.raises(ValueError, match="Path traversal"):
        svc.login()


def test_login_reads_the_key_from_the_config_dir(svc, tmp_path):
    """The ordinary path still works, and it resolves under the config directory."""
    keyfile = tmp_path / "authkey.p8"
    keyfile.write_text("  secret-contents  \n")
    svc.config = {"secret-key": "authkey.p8", "key-id": "k", "team-id": "t"}

    with (
        patch(
            "shuffleupagus.services.appleMusic.service.get_filepath",
            return_value=str(keyfile),
        ) as get_path,
        patch("shuffleupagus.services.appleMusic.service.applemusicpy.AppleMusic") as client,
        patch.object(AppleMusicService, "_check_rate_limit"),
    ):
        svc.login()

    get_path.assert_called_once_with("authkey.p8")
    passed_key = client.call_args.kwargs["secret_key"]
    assert passed_key == "secret-contents"
