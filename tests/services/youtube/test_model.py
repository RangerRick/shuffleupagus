import pytest

from shuffleupagus.services.youtube.model import (
    YoutubeAlbum,
    YoutubeArtist,
    YoutubeTrack,
    sanitize_id,
)

# --- sanitize_id ---

@pytest.mark.parametrize("raw, expected", [
    ("UCabc123", "UCabc123"),
    ("youtube:artist:UCabc123", "UCabc123"),
    ("https://www.youtube.com/channel/UCabc123", "UCabc123"),
    ("https://music.youtube.com/channel/UCabc123?si=x", "UCabc123"),
])
def test_sanitize_id(raw, expected):
    assert sanitize_id(raw) == expected


# --- YoutubeArtist ---

def test_artist_defaults():
    a = YoutubeArtist("UCabc", "Artist Name")
    assert a.id == "UCabc"
    assert a.name == "Artist Name"
    assert a.handle is None
    assert a.browseIds == {}
    assert a.inlineAlbums == []


def test_artist_with_handle():
    a = YoutubeArtist("UCabc", "Artist", handle="/@artist")
    assert a.handle == "/@artist"


def test_artist_display_name_prefers_handle():
    a = YoutubeArtist("UCabc", "Real Name", handle="/@handle")
    assert a.display_name() == "/@handle"


def test_artist_display_name_falls_back_to_name():
    a = YoutubeArtist("UCabc", "Real Name")
    assert a.display_name() == "Real Name"


def test_artist_matches_url():
    a = YoutubeArtist("UCabc123", "A")
    assert a.matches("UCabc123")
    assert a.matches("https://www.youtube.com/channel/UCabc123")


def test_artist_from_dict_albums_only():
    obj = {
        "channelId": "UC123",
        "name": "Band",
        "albums": {"browseId": "MPLA123", "params": "PQ==", "results": [{"title": "A1"}]},
    }
    a = YoutubeArtist.from_dict(obj)
    assert a.id == "UC123"
    assert a.browseIds["albums"] == "MPLA123"
    assert a.params["albums"] == "PQ=="
    assert len(a.inlineAlbums) == 1


def test_artist_from_dict_singles_appended():
    obj = {
        "channelId": "UC123",
        "name": "Band",
        "albums": {"browseId": None, "params": None, "results": [{"title": "A1"}]},
        "singles": {"browseId": None, "params": None, "results": [{"title": "S1"}, {"title": "S2"}]},
    }
    a = YoutubeArtist.from_dict(obj)
    assert len(a.inlineAlbums) == 3


def test_artist_from_dict_songs_browse_id():
    obj = {
        "channelId": "UC123",
        "name": "Band",
        "songs": {"browseId": "VLC123", "params": "PZ=="},
    }
    a = YoutubeArtist.from_dict(obj)
    assert a.browseIds["songs"] == "VLC123"


# --- YoutubeAlbum ---

def test_album_from_dict_browse_id():
    obj = {"browseId": "MPL123", "title": "My Album", "year": "2021"}
    alb = YoutubeAlbum.from_dict(obj)
    assert alb.id == "MPL123"
    assert alb.name == "My Album"
    assert alb.release_date is not None


def test_album_from_dict_audio_playlist_id_fallback():
    obj = {"audioPlaylistId": "APL456", "title": "EP", "year": "2020"}
    alb = YoutubeAlbum.from_dict(obj)
    assert alb.id == "APL456"


def test_album_from_dict_id_fallback():
    obj = {"id": "ID789", "title": "Single", "year": None, "type": "2019"}
    alb = YoutubeAlbum.from_dict(obj)
    assert alb.id == "ID789"
    assert alb.release_date is not None


def test_album_year_rejects_non_numeric():
    # 'type' sometimes contains "Single" instead of a year
    obj = {"id": "x", "title": "T", "year": "Single", "type": "EP"}
    alb = YoutubeAlbum.from_dict(obj)
    assert alb.release_date is None


def test_album_year_rejects_wrong_length():
    obj = {"id": "x", "title": "T", "year": "21", "type": None}
    alb = YoutubeAlbum.from_dict(obj)
    assert alb.release_date is None


# --- YoutubeTrack ---

def test_track_from_dict_video_id():
    obj = {"videoId": "vid1", "title": "Song", "duration_seconds": 200}
    t = YoutubeTrack.from_dict(obj)
    assert t.id == "vid1"
    assert t.name == "Song"
    assert t.duration_ms == 200_000


def test_track_from_dict_video_details():
    obj = {
        "videoDetails": {
            "videoId": "vid2",
            "title": "Another Song",
            "lengthSeconds": "180",
        }
    }
    t = YoutubeTrack.from_dict(obj)
    assert t.id == "vid2"
    assert t.duration_ms == 180_000


def test_track_from_dict_invalid_raises():
    with pytest.raises(ValueError):
        YoutubeTrack.from_dict({"unexpected": True})


def test_track_matches_url():
    t = YoutubeTrack("vid1", "Song", 60_000)
    assert t.matches("vid1")
    assert not t.matches("other")
