import pytest

from shuffleupagus.core.apiresponse import ApiResponseError
from shuffleupagus.services.appleMusic.model import (
    AppleMusicAlbum,
    AppleMusicArtist,
    AppleMusicTrack,
    sanitize_id,
)

# --- sanitize_id ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("123456789", "123456789"),
        ("https://music.apple.com/us/artist/123456789", "123456789"),
        ("https://music.apple.com/us/album/my-album/987?l=en", "987"),
    ],
)
def test_sanitize_id(raw, expected):
    assert sanitize_id(raw) == expected


# --- AppleMusicArtist ---


def test_artist_from_dict():
    obj = {"id": "a1", "attributes": {"name": "My Artist"}}
    artist = AppleMusicArtist.from_dict(obj)
    assert artist.id == "a1"
    assert artist.name == "My Artist"


def test_artist_sanitize_id():
    assert AppleMusicArtist.sanitize_id("https://music.apple.com/us/artist/99") == "99"


# --- AppleMusicAlbum ---


def test_album_from_dict():
    obj = {
        "id": "alb1",
        "attributes": {"name": "My Album", "releaseDate": "2022-03-15"},
    }
    alb = AppleMusicAlbum.from_dict(obj)
    assert alb.id == "alb1"
    assert alb.name == "My Album"
    assert str(alb.release_date) == "2022-03-15"


# --- AppleMusicTrack ---


def _track_obj(id="t1", name="Track", duration_ms=180_000, isrc="USRC00000001"):
    return {
        "id": id,
        "attributes": {
            "name": name,
            "durationInMillis": duration_ms,
            "isrc": isrc,
        },
    }


def test_track_from_dict_minimal():
    t = AppleMusicTrack.from_dict(_track_obj())
    assert t.id == "t1"
    assert t.name == "Track"
    assert t.duration_ms == 180_000
    assert t.isrc == "USRC00000001"
    assert t.album is None
    assert t.artists == []


def test_track_from_dict_with_album_and_artists():
    from shuffleupagus.core.model import Album, Artist

    alb = Album("alb1", "A")
    art = Artist("art1", "X")
    t = AppleMusicTrack.from_dict(_track_obj(), album=alb, artists=[art])
    assert t.album is not None
    assert t.album.id == "alb1"
    assert t.artists[0].name == "X"


# --- response shape checks (#59) ---


def _artist_payload():
    return {"id": "ar1", "attributes": {"name": "Artist"}}


def _album_payload():
    return {"id": "a1", "attributes": {"name": "Album", "releaseDate": "2020-01-01"}}


def _track_payload():
    return {
        "id": "t1",
        "attributes": {"name": "Track", "durationInMillis": 1000, "isrc": "US1234567890"},
    }


@pytest.mark.parametrize("container", [[], "string", 42, None])
def test_artist_from_dict_wrong_container(container):
    with pytest.raises(ApiResponseError, match="not an object"):
        AppleMusicArtist.from_dict(container)


def test_artist_from_dict_missing_id():
    obj = _artist_payload()
    del obj["id"]
    with pytest.raises(ApiResponseError, match="id is missing"):
        AppleMusicArtist.from_dict(obj)


def test_artist_from_dict_missing_attributes():
    with pytest.raises(ApiResponseError, match="attributes"):
        AppleMusicArtist.from_dict({"id": "ar1"})


def test_artist_from_dict_attributes_wrong_container():
    obj = _artist_payload()
    obj["attributes"] = ["not", "an", "object"]
    with pytest.raises(ApiResponseError, match="not an object"):
        AppleMusicArtist.from_dict(obj)


def test_artist_from_dict_wrong_name_type():
    obj = _artist_payload()
    obj["attributes"]["name"] = 42
    with pytest.raises(ApiResponseError, match="not a string"):
        AppleMusicArtist.from_dict(obj)


@pytest.mark.parametrize("container", [[], "string", 42, None])
def test_album_from_dict_wrong_container(container):
    with pytest.raises(ApiResponseError, match="not an object"):
        AppleMusicAlbum.from_dict(container)


@pytest.mark.parametrize("missing", ["name", "releaseDate"])
def test_album_from_dict_missing_attribute(missing):
    obj = _album_payload()
    del obj["attributes"][missing]
    with pytest.raises(ApiResponseError, match=missing):
        AppleMusicAlbum.from_dict(obj)


def test_album_from_dict_wrong_release_date_type():
    obj = _album_payload()
    obj["attributes"]["releaseDate"] = 2020
    with pytest.raises(ApiResponseError, match="not a string"):
        AppleMusicAlbum.from_dict(obj)


@pytest.mark.parametrize("container", [[], "string", 42, None])
def test_track_from_dict_wrong_container(container):
    with pytest.raises(ApiResponseError, match="not an object"):
        AppleMusicTrack.from_dict(container)


@pytest.mark.parametrize("missing", ["name", "durationInMillis", "isrc"])
def test_track_from_dict_missing_attribute(missing):
    obj = _track_payload()
    del obj["attributes"][missing]
    with pytest.raises(ApiResponseError, match=missing):
        AppleMusicTrack.from_dict(obj)


def test_track_from_dict_wrong_duration_type():
    obj = _track_payload()
    obj["attributes"]["durationInMillis"] = {"ms": 1000}
    with pytest.raises(ApiResponseError, match="not a number"):
        AppleMusicTrack.from_dict(obj)


def test_track_from_dict_reports_the_service():
    with pytest.raises(ApiResponseError, match="Apple Music"):
        AppleMusicTrack.from_dict({})
