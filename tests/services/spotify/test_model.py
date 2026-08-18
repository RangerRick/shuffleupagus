import pytest

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
