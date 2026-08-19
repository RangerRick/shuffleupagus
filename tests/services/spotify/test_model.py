import pytest

from shuffleupagus.core.apiresponse import ApiResponseError
from shuffleupagus.services.spotify.model import (
    SpotifyAlbum,
    SpotifyArtist,
    SpotifyTrack,
    sanitize_id,
)

# --- sanitize_id ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("abc123", "abc123"),
        ("spotify:artist:abc123", "abc123"),
        ("spotify:album:xyz", "xyz"),
        ("spotify:track:tid", "tid"),
        ("https://open.spotify.com/artist/abc123", "abc123"),
        ("https://open.spotify.com/track/tid?si=xxx", "tid"),
    ],
)
def test_sanitize_id(raw, expected):
    assert sanitize_id(raw) == expected


# --- SpotifyArtist ---


def test_spotify_artist_strips_prefix():
    a = SpotifyArtist("spotify:artist:id1", "Artist")
    assert a.id == "id1"


def test_spotify_artist_matches_raw():
    a = SpotifyArtist("id1", "Artist")
    assert a.matches("spotify:artist:id1")
    assert a.matches("id1")


def test_spotify_artist_from_dict():
    a = SpotifyArtist.from_dict({"id": "abc", "name": "My Artist"})
    assert a.id == "abc"
    assert a.name == "My Artist"


# --- SpotifyAlbum ---


def test_spotify_album_from_dict():
    alb = SpotifyAlbum.from_dict({"id": "alb1", "name": "My Album", "release_date": "2022-01-01"})
    assert alb.id == "alb1"
    assert alb.name == "My Album"


def test_spotify_album_matches_url():
    alb = SpotifyAlbum("alb1", "Album")
    assert alb.matches("https://open.spotify.com/album/alb1")


# --- SpotifyTrack ---


def test_spotify_track_from_dict_full():
    obj = {
        "id": "tid",
        "name": "My Track",
        "duration_ms": 240_000,
        "isrc": "USRC12345678",
        "album": {"id": "alb1", "name": "My Album", "release_date": "2022"},
        "artists": [{"id": "art1", "name": "Artist 1"}],
    }
    t = SpotifyTrack.from_dict(obj)
    assert t.id == "tid"
    assert t.name == "My Track"
    assert t.duration_ms == 240_000
    assert t.isrc == "USRC12345678"
    assert t.album is not None
    assert t.album.id == "alb1"
    assert len(t.artists) == 1
    assert t.artists[0].name == "Artist 1"


def test_spotify_track_from_dict_no_isrc():
    obj = {
        "id": "tid",
        "name": "T",
        "duration_ms": 100_000,
        "album": {"id": "a", "name": "A", "release_date": "2020"},
        "artists": [],
    }
    t = SpotifyTrack.from_dict(obj)
    assert t.isrc is None


def test_spotify_track_matches_url():
    t = SpotifyTrack("tid", "T", 60_000)
    assert t.matches("https://open.spotify.com/track/tid")
    assert t.matches("spotify:track:tid")


# --- response shape checks (#59) ---


def _track_payload():
    return {
        "id": "t1",
        "name": "Track",
        "duration_ms": 1000,
        "album": {"id": "a1", "name": "Album", "release_date": "2020-01-01"},
        "artists": [{"id": "ar1", "name": "Artist"}],
    }


@pytest.mark.parametrize("missing", ["id", "name"])
def test_artist_from_dict_missing_key(missing):
    obj = {"id": "ar1", "name": "Artist"}
    del obj[missing]
    with pytest.raises(ApiResponseError, match=missing):
        SpotifyArtist.from_dict(obj)


@pytest.mark.parametrize("field", ["id", "name"])
def test_artist_from_dict_wrong_type(field):
    obj: dict = {"id": "ar1", "name": "Artist"}
    obj[field] = 42
    with pytest.raises(ApiResponseError, match="not a string"):
        SpotifyArtist.from_dict(obj)


@pytest.mark.parametrize("container", [[], "string", 42, None])
def test_artist_from_dict_wrong_container(container):
    with pytest.raises(ApiResponseError, match="not an object"):
        SpotifyArtist.from_dict(container)


@pytest.mark.parametrize("missing", ["id", "name", "release_date"])
def test_album_from_dict_missing_key(missing):
    obj = {"id": "a1", "name": "Album", "release_date": "2020-01-01"}
    del obj[missing]
    with pytest.raises(ApiResponseError, match=missing):
        SpotifyAlbum.from_dict(obj)


@pytest.mark.parametrize("field", ["id", "name", "release_date"])
def test_album_from_dict_wrong_type(field):
    obj: dict = {"id": "a1", "name": "Album", "release_date": "2020-01-01"}
    obj[field] = {"nested": True}
    with pytest.raises(ApiResponseError, match="not a string"):
        SpotifyAlbum.from_dict(obj)


@pytest.mark.parametrize("container", [[], "string", 42, None])
def test_album_from_dict_wrong_container(container):
    with pytest.raises(ApiResponseError, match="not an object"):
        SpotifyAlbum.from_dict(container)


@pytest.mark.parametrize("missing", ["id", "name", "duration_ms", "album"])
def test_track_from_dict_missing_key(missing):
    obj = _track_payload()
    del obj[missing]
    with pytest.raises(ApiResponseError, match=missing):
        SpotifyTrack.from_dict(obj)


def test_track_from_dict_wrong_duration_type():
    obj = _track_payload()
    obj["duration_ms"] = {"ms": 1000}
    with pytest.raises(ApiResponseError, match="not a number"):
        SpotifyTrack.from_dict(obj)


@pytest.mark.parametrize("container", [[], "string", 42, None])
def test_track_from_dict_wrong_container(container):
    with pytest.raises(ApiResponseError, match="not an object"):
        SpotifyTrack.from_dict(container)


def test_track_from_dict_album_wrong_container():
    obj = _track_payload()
    obj["album"] = ["not", "an", "object"]
    with pytest.raises(ApiResponseError, match="album is not an object"):
        SpotifyTrack.from_dict(obj)


def test_track_from_dict_artists_wrong_container():
    obj = _track_payload()
    obj["artists"] = {"not": "a list"}
    with pytest.raises(ApiResponseError, match="not a list"):
        SpotifyTrack.from_dict(obj)


def test_track_from_dict_artist_entry_wrong_container():
    obj = _track_payload()
    obj["artists"] = ["just a string"]
    with pytest.raises(ApiResponseError, match="artists\\[\\] entry is not an object"):
        SpotifyTrack.from_dict(obj)


def test_track_from_dict_absent_artists_is_allowed():
    obj = _track_payload()
    del obj["artists"]
    assert SpotifyTrack.from_dict(obj).artists == []


def test_track_from_dict_absent_isrc_is_allowed():
    obj = _track_payload()
    assert SpotifyTrack.from_dict(obj).isrc is None


def test_track_from_dict_reports_the_service():
    with pytest.raises(ApiResponseError, match="Spotify"):
        SpotifyTrack.from_dict({})
