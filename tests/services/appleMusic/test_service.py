"""Tests for AppleMusicService with all network/applescript calls mocked."""

from unittest.mock import MagicMock, patch

import pytest

from shuffleupagus.core.cache import Cache
from shuffleupagus.services.appleMusic.service import AppleMusicService

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
    s.cache = Cache("appleMusic")
    s.config = {"media-user-token": "tok"}
    s.client = MagicMock()
    s.client.proxies = {}
    s.client.session_length = 30
    s.tag = "[apple] "
    return s


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


def test_get_artist_api_error_returns_none(svc):
    svc.client.artist.side_effect = Exception("network error")
    artist = svc.get_artist("a1")
    assert artist is None


def test_get_artist_empty_data_returns_none(svc):
    svc.client.artist.return_value = {"data": []}
    artist = svc.get_artist("a1")
    assert artist is None


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


def test_get_album_by_id_error_returns_none(svc):
    svc.client.album.side_effect = Exception("api error")
    album = svc.get_album_by_id("alb1")
    assert album is None


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


def test_get_artist_albums_error_returns_empty(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship.side_effect = Exception("error")
    albums = svc.get_artist_albums(Artist("a1", "A"))
    assert albums == []


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


def test_get_artist_top_tracks_error_returns_empty(svc):
    from shuffleupagus.core.model import Artist

    svc.client.artist_relationship_view.side_effect = Exception("error")
    tracks = svc.get_artist_top_tracks(Artist("a1", "A"))
    assert tracks == []


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


def test_sync(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp
    svc.client._auth_headers.return_value = {}

    with patch("shuffleupagus.services.appleMusic.service.applescript.AppleScript", side_effect=_mock_applescripts()):
        svc.sync("Playlist", ["t1", "t2", "t3"])

    svc.client._session.post.assert_called()
    # One post call for 3 tracks (all in single batch)
    assert svc.client._session.post.call_count == 1


def test_sync_batches_tracks(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")

    post_resp = MagicMock()
    post_resp.status_code = 204
    svc.client._session.post.return_value = post_resp
    svc.client._auth_headers.return_value = {}

    tracks = [f"t{i}" for i in range(90)]
    with patch("shuffleupagus.services.appleMusic.service.applescript.AppleScript", side_effect=_mock_applescripts()):
        svc.sync("Playlist", tracks)

    # 90 tracks → 2 batches of 80 and 10
    assert svc.client._session.post.call_count == 2
