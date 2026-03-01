"""Tests for YoutubeService with all network/ytmusicapi calls mocked."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from shuffleupagus.core.cache import Cache
from shuffleupagus.services.youtube.service import YoutubeService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    s = YoutubeService.__new__(YoutubeService)
    s.cache = Cache("youtube")
    s.config = {}
    s.client = MagicMock()
    s.tag = "[youtube] "
    return s


# ---------------------------------------------------------------------------
# sanitize_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("UCabc", "UCabc"),
        ("@handle", "@handle"),
        ("https://www.youtube.com/channel/UCabc", "UCabc"),
        ("https://www.youtube.com/@myband", "@myband"),
        ("youtube.com/channel/UCabc", "UCabc"),
        ("youtube.com/@myband", "@myband"),
        ("www.youtube.com/channel/UCabc?si=x", "UCabc"),
    ],
)
def test_sanitize_id(svc, raw, expected):
    assert svc.sanitize_id(raw) == expected


# ---------------------------------------------------------------------------
# __get_channel_id (via get_artist to avoid name-mangling in tests)
# ---------------------------------------------------------------------------


def test_get_channel_id_bare_id_no_fetch(svc):
    """Bare channel IDs skip the HTTP request entirely."""
    with patch("shuffleupagus.services.youtube.service.requests.get") as mock_get:
        svc.client.get_artist.return_value = {"channelId": "UCabc", "name": "Band"}
        svc.get_artist("UCabc")
        mock_get.assert_not_called()


def test_get_channel_id_cached(svc):
    svc.cache.write("channel:@band", "UCabc")
    svc.cache.write("channel:handle:UCabc", "@band")
    with patch("shuffleupagus.services.youtube.service.requests.get") as mock_get:
        svc.client.get_artist.return_value = {"channelId": "UCabc", "name": "Band"}
        svc.get_artist("@band")
        mock_get.assert_not_called()


def test_get_channel_id_fetches_handle_page(svc):
    html = (
        '<html><link rel="canonical" href="https://www.youtube.com/channel/UCabc">"canonicalBaseUrl":"/@theband"</html>'
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    with patch("shuffleupagus.services.youtube.service.requests.get", return_value=mock_resp):
        svc.client.get_artist.return_value = {"channelId": "UCabc", "name": "Band"}
        artist = svc.get_artist("@theband")
    assert artist.id == "UCabc"
    assert artist.handle == "@theband"
    assert svc.cache.read("channel:@theband") == "UCabc"
    assert svc.cache.read("channel:handle:UCabc") == "@theband"


def test_get_channel_id_extracts_handle_from_html(svc):
    # URL with @handle gets sanitized to "@newband"; handle is set from the input,
    # not from canonicalBaseUrl (which only fires when there's no @-prefixed input).
    html = '<html>"browseId":"UCxyz""canonicalBaseUrl":"/@newband"</html>'
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    with patch("shuffleupagus.services.youtube.service.requests.get", return_value=mock_resp):
        svc.client.get_artist.return_value = {"channelId": "UCxyz", "name": "New Band"}
        artist = svc.get_artist("https://www.youtube.com/@newband")
    assert artist.handle == "@newband"


def test_get_channel_id_http_error_returns_none(svc):
    # HTTP errors are caught; get_artist returns None instead of raising.
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "Not Found"
    with patch("shuffleupagus.services.youtube.service.requests.get", return_value=mock_resp):
        artist = svc.get_artist("@missing")
    assert artist is None


def test_get_channel_id_not_found_in_html_returns_none(svc):
    # Missing channel ID in page is caught; get_artist returns None.
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>no channel info here</html>"
    with patch("shuffleupagus.services.youtube.service.requests.get", return_value=mock_resp):
        artist = svc.get_artist("@unknown")
    assert artist is None


# ---------------------------------------------------------------------------
# get_artist
# ---------------------------------------------------------------------------


def test_get_artist_cache_hit(svc):
    svc.cache.write("channel:@band", "UCabc")
    svc.cache.write("artist:UCabc", {"channelId": "UCabc", "name": "Cached Band"})
    artist = svc.get_artist("@band")
    assert artist.name == "Cached Band"
    svc.client.get_artist.assert_not_called()


def test_get_artist_no_yt_music_page_returns_none(svc):
    from ytmusicapi.exceptions import YTMusicServerError

    svc.client.get_artist.side_effect = YTMusicServerError("500")
    svc.cache.write("channel:@ghost", "UCghost")
    artist = svc.get_artist("@ghost")
    assert artist is None


def test_get_artist_400_logs_oauth_warning(svc, caplog):
    from ytmusicapi.exceptions import YTMusicServerError

    svc.client.get_artist.side_effect = YTMusicServerError("400 Bad Request")
    svc.cache.write("channel:@oauth", "UCoauth")
    import logging

    with caplog.at_level(logging.WARNING):
        svc.get_artist("@oauth")
    assert "400" in caplog.text or "OAuth" in caplog.text


def test_get_artist_resolution_failure_returns_none(svc):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("shuffleupagus.services.youtube.service.requests.get", return_value=mock_resp):
        artist = svc.get_artist("@nope")
    assert artist is None


# ---------------------------------------------------------------------------
# get_artist_albums
# ---------------------------------------------------------------------------


def test_get_artist_albums_with_browse_id(svc):
    from shuffleupagus.services.youtube.model import YoutubeArtist

    artist = YoutubeArtist("UCabc", "Band")
    artist.browseIds["albums"] = "MPLA123"
    artist.params["albums"] = "PQ=="
    svc.client.get_artist_albums.return_value = [
        {"browseId": "MPL1", "title": "Album 1", "year": "2020"},
        {"browseId": "MPL2", "title": "Album 2", "year": "2021"},
    ]
    albums = svc.get_artist_albums(artist)
    assert len(albums) == 2
    svc.client.get_artist_albums.assert_called_once_with("MPLA123", "PQ==", limit=100)


def test_get_artist_albums_inline(svc):
    from shuffleupagus.services.youtube.model import YoutubeArtist

    artist = YoutubeArtist("UCabc", "Band")
    artist.inlineAlbums = [{"browseId": "MPL1", "title": "Inline Album", "year": "2019"}]
    albums = svc.get_artist_albums(artist)
    assert len(albums) == 1
    assert albums[0].name == "Inline Album"


def test_get_artist_albums_none(svc):
    from shuffleupagus.services.youtube.model import YoutubeArtist

    artist = YoutubeArtist("UCabc", "Band")
    albums = svc.get_artist_albums(artist)
    assert albums == []


def test_get_artist_albums_cache_hit(svc):
    from shuffleupagus.services.youtube.model import YoutubeArtist

    artist = YoutubeArtist("UCabc", "Band")
    artist.browseIds["albums"] = "MPLA"
    artist.params["albums"] = "P"
    svc.cache.write("artist:UCabc:albums", [{"browseId": "MPL1", "title": "Cached", "year": "2020"}])
    albums = svc.get_artist_albums(artist)
    assert len(albums) == 1
    svc.client.get_artist_albums.assert_not_called()


def test_get_artist_albums_fingerprint_match_uses_stale(svc):
    """Stale cached albums + matching inline fingerprint → no API call."""
    import time

    from shuffleupagus.services.youtube.model import YoutubeArtist

    stale = [{"browseId": "MPL1", "title": "Album", "year": "2020"}]
    svc.cache.write("artist:UCabc:albums", stale, ttl=60.0)
    svc.cache._conn.execute("UPDATE cache SET stored_at = ? WHERE key = ?", (time.time() - 3600, "artist:UCabc:albums"))
    svc.cache._conn.commit()

    artist = YoutubeArtist("UCabc", "Band")
    artist.inlineAlbums = [{"browseId": "MPL1", "title": "Album", "year": "2020"}]

    albums = svc.get_artist_albums(artist)
    assert len(albums) == 1
    svc.client.get_artist_albums.assert_not_called()


def test_get_artist_albums_fingerprint_mismatch_refetches(svc):
    """Stale cached albums + inline fingerprint mismatch → full API call."""
    import time

    from shuffleupagus.services.youtube.model import YoutubeArtist

    stale = [{"browseId": "MPL1", "title": "Old Album", "year": "2020"}]
    svc.cache.write("artist:UCabc:albums", stale, ttl=60.0)
    svc.cache._conn.execute("UPDATE cache SET stored_at = ? WHERE key = ?", (time.time() - 3600, "artist:UCabc:albums"))
    svc.cache._conn.commit()

    artist = YoutubeArtist("UCabc", "Band")
    # inlineAlbums[0] has a different browseId — new release detected
    artist.inlineAlbums = [{"browseId": "MPL2", "title": "New Album", "year": "2024"}]
    artist.browseIds["albums"] = "MPLA"
    artist.params["albums"] = "P"
    svc.client.get_artist_albums.return_value = [
        {"browseId": "MPL1", "title": "Old Album", "year": "2020"},
        {"browseId": "MPL2", "title": "New Album", "year": "2024"},
    ]

    albums = svc.get_artist_albums(artist)
    assert len(albums) == 2
    svc.client.get_artist_albums.assert_called_once()


# ---------------------------------------------------------------------------
# get_album_tracks
# ---------------------------------------------------------------------------


def test_get_album_tracks(svc):
    from shuffleupagus.core.model import Album

    album = Album("MPL1", "Album 1")
    svc.client.get_album.return_value = {
        "tracks": [
            {"videoId": "vid1", "title": "Song 1", "duration_seconds": 200, "artists": []},
            {"videoId": "vid2", "title": "Song 2", "duration_seconds": 180, "artists": []},
        ]
    }
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 2
    assert tracks[0].id == "vid1"
    assert tracks[0].duration_ms == 200_000


def test_get_album_tracks_cache_hit(svc):
    from shuffleupagus.core.model import Album

    album = Album("MPL1", "Album")
    svc.cache.write("album:MPL1", {"tracks": [{"videoId": "v1", "title": "T", "duration_seconds": 100, "artists": []}]})
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 1
    svc.client.get_album.assert_not_called()


def test_get_album_tracks_empty(svc):
    from shuffleupagus.core.model import Album

    svc.client.get_album.return_value = {"tracks": []}
    tracks = svc.get_album_tracks(Album("MPL1", "A"))
    assert tracks == []


# ---------------------------------------------------------------------------
# get_playlist_id_for_name / sync
# ---------------------------------------------------------------------------


def test_get_playlist_id_found(svc):
    svc._data_api_get = MagicMock(
        return_value={
            "items": [
                {"id": "pl1", "snippet": {"title": "My YT Playlist"}},
                {"id": "pl2", "snippet": {"title": "Other"}},
            ]
        }
    )
    assert svc.get_playlist_id_for_name("My YT Playlist") == "pl1"


def test_get_playlist_id_not_found(svc):
    svc._data_api_get = MagicMock(return_value={"items": []})
    with pytest.raises(ValueError, match="not found"):
        svc.get_playlist_id_for_name("Missing")


def test_get_playlist_id_paginates(svc):
    svc._data_api_get = MagicMock(
        side_effect=[
            {"items": [{"id": "pl1", "snippet": {"title": "Other"}}], "nextPageToken": "tok"},
            {"items": [{"id": "pl2", "snippet": {"title": "Target"}}]},
        ]
    )
    assert svc.get_playlist_id_for_name("Target") == "pl2"


def test_sync_deletes_and_inserts(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    svc._data_api_get = MagicMock(return_value={"items": [{"id": "item1"}, {"id": "item2"}]})
    svc._data_api_delete = MagicMock()
    svc._data_api_post = MagicMock()

    svc.sync("Playlist", ["vid1", "vid2", "vid3"])

    assert svc._data_api_delete.call_count == 2
    assert svc._data_api_post.call_count == 3


# ---------------------------------------------------------------------------
# Browser auth: validation and setup
# ---------------------------------------------------------------------------


@pytest.fixture
def browser_svc(tmp_path, monkeypatch):
    """YoutubeService wired for browser-cookie auth (no client_id/secret)."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    s = YoutubeService.__new__(YoutubeService)
    s.cache = Cache("youtube")
    s.config = {"auth-file": str(tmp_path / "browser_auth.json")}
    s.tag = "[youtube] "
    return s, tmp_path


def test_preflight_browser_auth_valid(browser_svc):
    """Valid cookies → preflight passes, no re-auth prompt."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"
    auth_file.write_text("{}")

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.ytmusicapi.setup") as mock_setup,
    ):
        mock_client = MagicMock()
        mock_client.get_artist.return_value = {"channelId": "UCtest", "name": "Test"}
        mock_yt.return_value = mock_client
        svc.preflight()

    mock_setup.assert_not_called()


def test_preflight_browser_auth_expired_interactive_reauth(browser_svc):
    """Expired cookies + interactive TTY → preflight triggers re-auth."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"
    auth_file.write_text("{}")

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.ytmusicapi.setup") as mock_setup,
        patch("shuffleupagus.services.youtube.service.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = True
        # First call (initial validate) fails; second call (post-setup validate) succeeds
        fail_client = MagicMock()
        fail_client.get_artist.side_effect = YTMusicServerError("400")
        ok_client = MagicMock()
        ok_client.get_artist.return_value = {"channelId": "UCtest", "name": "Test"}
        mock_yt.side_effect = [fail_client, ok_client]

        svc.preflight()

    mock_setup.assert_called_once_with(filepath=str(auth_file))


def test_preflight_browser_auth_expired_env_var(browser_svc, monkeypatch):
    """Expired cookies + YTMUSIC_HEADERS_RAW → setup(headers_raw=...) called."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"
    auth_file.write_text("{}")
    monkeypatch.setenv("YTMUSIC_HEADERS_RAW", "fake-raw-headers")

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.ytmusicapi.setup") as mock_setup,
    ):
        fail_client = MagicMock()
        fail_client.get_artist.side_effect = YTMusicServerError("400")
        ok_client = MagicMock()
        ok_client.get_artist.return_value = {"channelId": "UCtest", "name": "Test"}
        mock_yt.side_effect = [fail_client, ok_client]

        svc.preflight()

    mock_setup.assert_called_once_with(filepath=str(auth_file), headers_raw="fake-raw-headers")


def test_preflight_browser_auth_expired_no_tty(browser_svc, monkeypatch):
    """Expired cookies + no TTY + no env var → preflight raises ValueError."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"
    auth_file.write_text("{}")
    monkeypatch.delenv("YTMUSIC_HEADERS_RAW", raising=False)

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = False
        mock_client = MagicMock()
        mock_client.get_artist.side_effect = YTMusicServerError("400")
        mock_yt.return_value = mock_client

        with pytest.raises(ValueError, match="setup failed"):
            svc.preflight()


def test_preflight_browser_auth_missing_file(browser_svc):
    """No auth file exists → preflight prompts setup."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"
    # Don't create the file — it shouldn't exist

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.ytmusicapi.setup") as mock_setup,
        patch("shuffleupagus.services.youtube.service.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = True
        # setup creates the file as a side effect
        mock_setup.side_effect = lambda **kw: Path(kw["filepath"]).write_text("{}")
        mock_client = MagicMock()
        mock_client.get_artist.return_value = {"channelId": "UCtest", "name": "Test"}
        mock_yt.return_value = mock_client

        svc.preflight()

    mock_setup.assert_called_once_with(filepath=str(auth_file))


def test_preflight_retries_on_bad_cookies(browser_svc):
    """Bad cookies after setup → retry up to max attempts, succeed on second try."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"
    # File doesn't exist — triggers setup path

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.ytmusicapi.setup") as mock_setup,
        patch("shuffleupagus.services.youtube.service.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = True
        mock_setup.side_effect = lambda **kw: Path(kw["filepath"]).write_text("{}")

        # First post-setup validate fails (bad cookies), second succeeds
        bad_client = MagicMock()
        bad_client.get_artist.side_effect = YTMusicUserError("missing __Secure-3PAPISID")
        ok_client = MagicMock()
        ok_client.get_artist.return_value = {"channelId": "UCtest", "name": "Test"}
        mock_yt.side_effect = [bad_client, ok_client]

        svc.preflight()

    assert mock_setup.call_count == 2


def test_preflight_exhausts_retries(browser_svc):
    """Bad cookies on every attempt → raises after max attempts."""
    svc, tmp_path = browser_svc
    auth_file = tmp_path / "browser_auth.json"

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch("shuffleupagus.services.youtube.service.ytmusicapi.setup") as mock_setup,
        patch("shuffleupagus.services.youtube.service.sys") as mock_sys,
    ):
        mock_sys.stdin.isatty.return_value = True
        mock_setup.side_effect = lambda **kw: Path(kw["filepath"]).write_text("{}")
        bad_client = MagicMock()
        bad_client.get_artist.side_effect = YTMusicUserError("missing cookie")
        mock_yt.return_value = bad_client

        with pytest.raises(ValueError, match="failed after 3 attempts"):
            svc.preflight()

    assert mock_setup.call_count == 3


def test_preflight_validates_browser_auth_in_oauth_mode(browser_svc):
    """OAuth mode (client-id + client-secret set) → preflight still validates browser cookies
    using a separate browser cookie file (auth-file stem + '_browser')."""
    svc, tmp_path = browser_svc
    svc.config["client-id"] = "some-id"
    svc.config["client-secret"] = "some-secret"

    auth_file = tmp_path / "browser_auth.json"
    browser_file = tmp_path / "browser_auth_browser.json"
    browser_file.write_text("{}")

    with (
        patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
    ):
        mock_client = MagicMock()
        mock_client.get_artist.return_value = {"channelId": "UCtest", "name": "Test"}
        mock_yt.return_value = mock_client
        svc.preflight()

    mock_yt.assert_called_once_with(str(browser_file))
