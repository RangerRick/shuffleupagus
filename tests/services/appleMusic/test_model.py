import pytest

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
