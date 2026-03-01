from concurrent.futures import ThreadPoolExecutor

import pytest

from shuffleupagus.core.model import (
    MAX_ARTIST_TRACKS,
    MAX_TOP_TRACKS,
    MAX_TRACK_LENGTH_MS,
    Album,
    Artist,
    ShufObject,
    Track,
)

# --- ShufObject ---


def test_shufobject_matches_id():
    obj = ShufObject("abc", "Name")
    assert obj.matches("abc")
    assert not obj.matches("xyz")


def test_shufobject_is_excluded():
    obj = ShufObject("abc", "Name")
    assert obj.is_excluded(["abc", "def"])
    assert not obj.is_excluded(["xyz"])


def test_shufobject_sanitize_id_passthrough():
    assert ShufObject.sanitize_id("foo") == "foo"


# --- Album ---


def test_album_release_date_from_full_string():
    import datetime

    a = Album("id", "Title", "2023-06-15")
    assert a.release_date == datetime.date(2023, 6, 15)


def test_album_release_date_from_year_only():
    import datetime

    a = Album("id", "Title", "2020")
    assert a.release_date == datetime.date(2020, 1, 1)


def test_album_release_date_from_date_object():
    import datetime

    d = datetime.date(2021, 3, 1)
    a = Album("id", "Title", d)
    assert a.release_date == d


def test_album_release_date_none():
    a = Album("id", "Title")
    assert a.release_date is None


def test_album_release_date_invalid_type():
    with pytest.raises(ValueError):
        Album("id", "Title", 12345)


def test_album_str():
    a = Album("id", "My Album")
    assert "My Album" in str(a)


# --- Track ---


def _make_track(name="Song", duration_ms=180_000, id="t1", isrc=None):
    return Track(id=id, name=name, duration_ms=duration_ms, isrc=isrc)


def test_track_longer_than_true():
    t = _make_track(duration_ms=500_000)
    assert t.longer_than(400_000)


def test_track_longer_than_false():
    t = _make_track(duration_ms=100_000)
    assert not t.longer_than(200_000)


def test_track_dedupe_hash_same_for_similar_names():
    # Punctuation and case differences should produce the same hash
    t1 = Track("a", "Hello World", 180_000)
    t2 = Track("b", "hello world!", 180_000)
    assert t1.dedupe_hash == t2.dedupe_hash


def test_track_dedupe_hash_rounds_duration():
    # Duration rounded to nearest 2000ms
    t1 = Track("a", "Song", 180_001)
    t2 = Track("b", "Song", 180_999)
    assert t1.dedupe_hash == t2.dedupe_hash


def test_track_dedupe_hash_differs_for_different_duration():
    t1 = Track("a", "Song", 180_000)
    t2 = Track("b", "Song", 200_000)
    assert t1.dedupe_hash != t2.dedupe_hash


def test_track_str():
    t = _make_track(name="My Track")
    assert "My Track" in str(t)


def test_track_is_excluded():
    t = _make_track(id="track-123")
    assert t.is_excluded(["track-123"])
    assert not t.is_excluded(["other"])


# --- generate_playlist (via a minimal stub service) ---


class _StubTrack(Track):
    pass


def _track(id, name="T", duration_ms=120_000, album_id="alb1"):
    album = Album(album_id, "Album")
    return _StubTrack(id=id, name=name, duration_ms=duration_ms, album=album)


class _StubService:
    """Minimal Service-like object that calls generate_playlist directly."""

    tag = "[test] "
    pool = ThreadPoolExecutor(max_workers=2)

    def get_artist(self, artist):
        return Artist(artist, artist)

    def get_artist_top_tracks(self, artist):
        return [_track(f"top-{i}", f"Top {i}") for i in range(7)]

    def get_artist_tracks(self, artist):
        return [_track(f"art-{i}", f"Art {i}") for i in range(20)]

    def generate_playlist(self, artist_ids, excluded_albums=None, excluded_tracks=None, vip_artist_ids=None):
        from shuffleupagus.core.model import Service

        return Service.generate_playlist(
            self,
            artist_ids=artist_ids,
            excluded_album_ids=excluded_albums or [],
            excluded_track_ids=excluded_tracks or [],
            vip_artist_ids=vip_artist_ids or [],
        )


def test_generate_playlist_respects_max_tracks():
    svc = _StubService()
    result = svc.generate_playlist(["artist1"])
    assert len(result) <= MAX_TOP_TRACKS + MAX_ARTIST_TRACKS


def test_generate_playlist_excludes_tracks():
    svc = _StubService()
    result = svc.generate_playlist(["artist1"], excluded_tracks=["top-0", "top-1"])
    assert "top-0" not in result
    assert "top-1" not in result


def test_generate_playlist_excludes_albums():
    class Svc(_StubService):
        def get_artist_top_tracks(self, artist):
            album = Album("excluded-alb", "Bad Album")
            return [_StubTrack(id="bad", name="Bad", duration_ms=60_000, album=album)]

        def get_artist_tracks(self, artist):
            return []

    svc = Svc()
    result = svc.generate_playlist(["artist1"], excluded_albums=["excluded-alb"])
    assert "bad" not in result


def test_generate_playlist_filters_long_tracks():
    class Svc(_StubService):
        def get_artist_top_tracks(self, artist):
            return [_track("too-long", duration_ms=MAX_TRACK_LENGTH_MS + 1)]

        def get_artist_tracks(self, artist):
            return []

    result = Svc().generate_playlist(["artist1"])
    assert "too-long" not in result


def test_generate_playlist_deduplicates(monkeypatch):
    # Dedup removes artist_tracks that share a hash with top_tracks.
    # Top tracks themselves are not deduped against each other.
    monkeypatch.setattr("shuffleupagus.core.util.random.randint", lambda a, b: 0)

    class Svc(_StubService):
        def get_artist_top_tracks(self, artist):
            return [_track("id-a", "Dup", 120_000)]

        def get_artist_tracks(self, artist):
            return [
                _track("id-b", "Dup", 120_000),  # same hash as id-a → deduped out
                _track("id-c", "Other", 120_000),  # different name → kept
            ]

    result = Svc().generate_playlist(["artist1"])
    assert "id-a" in result  # top track kept
    assert "id-b" not in result  # artist track deduped out (matches id-a hash)
    assert "id-c" in result  # different track kept


def test_generate_playlist_requires_album():
    class Svc(_StubService):
        def get_artist_top_tracks(self, artist):
            return [Track(id="no-album", name="X", duration_ms=60_000)]

        def get_artist_tracks(self, artist):
            return []

    result = Svc().generate_playlist(["artist1"])
    assert "no-album" not in result


def test_generate_playlist_unknown_artist_skips():
    class Svc(_StubService):
        def get_artist(self, artist):
            return None

    result = Svc().generate_playlist(["ghost"])
    assert result == []


def test_generate_playlist_returns_unique_ids():
    svc = _StubService()
    result = svc.generate_playlist(["a1", "a2"])
    assert len(result) == len(set(result))
