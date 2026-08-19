"""Tests for YoutubeService with all network/ytmusicapi calls mocked."""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from shuffleupagus.core.apiresponse import ApiResponseError
from shuffleupagus.core.cache import Cache
from shuffleupagus.core.model import Album, Artist
from shuffleupagus.services.youtube.model import YoutubeArtist
from shuffleupagus.services.youtube.service import YoutubeService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    s = YoutubeService.__new__(YoutubeService)
    cache = Cache("youtube")
    s.cache = cache
    s.config = {}
    s.client = MagicMock()
    s.tag = "[youtube] "
    yield s
    # Close the cache this fixture built, not s.cache — a test may have swapped
    # s.cache for a mock, which would orphan the real sqlite connection.
    cache.close()


def _mock_token(value: str = "test-token") -> MagicMock:
    """Build a mock token object with access_token set (avoids S105 false positive)."""
    tok = MagicMock()
    tok.access_token = value
    return tok


def _wire_oauth_token(svc, value: str = "test-token") -> None:
    """Give svc a mock OAuth client whose token returns *value*."""
    svc._oauth_client = MagicMock()
    svc._oauth_client._token = _mock_token(value)


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


def _mock_playlist_api(svc, existing_item_ids, verified_video_ids):
    """Mock _data_api_get: the 'id' read lists items, 'contentDetails' lists video IDs."""

    def _get(_url, params):
        if params.get("part") == "contentDetails":
            return {"items": [{"contentDetails": {"videoId": v}} for v in verified_video_ids]}
        return {"items": [{"id": i} for i in existing_item_ids]}

    svc._data_api_get = MagicMock(side_effect=_get)


def test_sync_deletes_and_inserts(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    _mock_playlist_api(svc, ["item1", "item2"], ["vid1", "vid2", "vid3"])
    svc._data_api_delete = MagicMock()
    svc._data_api_post = MagicMock()

    svc.sync("Playlist", ["vid1", "vid2", "vid3"])

    assert svc._data_api_delete.call_count == 2
    assert svc._data_api_post.call_count == 3


def test_sync_readds_missing_videos(svc):
    """A video absent from the read-back is re-added, then verification passes."""
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    reads = [["vid1"], ["vid1", "vid2"]]

    def _get(_url, params):
        if params.get("part") == "contentDetails":
            page = reads[0] if len(reads) == 1 else reads.pop(0)
            return {"items": [{"contentDetails": {"videoId": v}} for v in page]}
        return {"items": []}

    svc._data_api_get = MagicMock(side_effect=_get)
    svc._data_api_delete = MagicMock()
    svc._data_api_post = MagicMock()

    svc.sync("Playlist", ["vid1", "vid2"])

    # 2 initial inserts + 1 re-add of the missing vid2
    assert svc._data_api_post.call_count == 3


def test_sync_raises_when_videos_never_verify(svc):
    """A video the API never persists raises instead of retrying forever."""
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    _mock_playlist_api(svc, [], ["vid1"])
    svc._data_api_delete = MagicMock()
    svc._data_api_post = MagicMock()

    with pytest.raises(RuntimeError, match="could not be verified"):
        svc.sync("Playlist", ["vid1", "vid2"])


# ---------------------------------------------------------------------------
# Browser auth: validation and setup
# ---------------------------------------------------------------------------


@pytest.fixture
def browser_svc(tmp_path, monkeypatch):
    """YoutubeService wired for browser-cookie auth (no client_id/secret)."""
    monkeypatch.setattr(Cache, "_db_path", lambda self: str(tmp_path / f"{self.name}.db"))
    s = YoutubeService.__new__(YoutubeService)
    cache = Cache("youtube")
    s.cache = cache
    s.config = {"auth-file": str(tmp_path / "browser_auth.json")}
    s.tag = "[youtube] "
    yield s, tmp_path
    cache.close()


def _valid_browse_response():
    """Minimal browse response indicating successful authentication."""
    return {
        "responseContext": {
            "serviceTrackingParams": [
                {"service": "CSI", "params": [{"key": "logged_in", "value": "1"}]},
            ]
        }
    }


def _invalid_browse_response():
    """Browse response indicating unauthenticated/expired session."""
    return {"responseContext": {"serviceTrackingParams": []}}


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
        mock_client._send_request.return_value = _valid_browse_response()
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
        fail_client._send_request.side_effect = YTMusicServerError("400")
        ok_client = MagicMock()
        ok_client._send_request.return_value = _valid_browse_response()
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
        fail_client._send_request.side_effect = YTMusicServerError("400")
        ok_client = MagicMock()
        ok_client._send_request.return_value = _valid_browse_response()
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
        mock_client._send_request.side_effect = YTMusicServerError("400")
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
        mock_client._send_request.return_value = _valid_browse_response()
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
        bad_client._send_request.return_value = _invalid_browse_response()
        ok_client = MagicMock()
        ok_client._send_request.return_value = _valid_browse_response()
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
        bad_client._send_request.return_value = _invalid_browse_response()
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
        mock_client._send_request.return_value = _valid_browse_response()
        mock_yt.return_value = mock_client
        svc.preflight()

    mock_yt.assert_called_once_with(str(browser_file))


# ---------------------------------------------------------------------------
# _require_config
# ---------------------------------------------------------------------------


def test_require_config_missing_raises(svc):
    svc.config = {}
    with pytest.raises(ValueError, match="Missing required config key"):
        svc._require_config("auth-file")


# ---------------------------------------------------------------------------
# _try_validate_browser_file — exception paths
# ---------------------------------------------------------------------------


def test_try_validate_browser_file_user_error(svc, tmp_path):
    auth_file = tmp_path / "bad.json"
    auth_file.write_text("{}")
    with patch(
        "shuffleupagus.services.youtube.service.YTMusic",
        side_effect=YTMusicUserError("bad cookie"),
    ):
        assert svc._try_validate_browser_file(auth_file) is False


def test_try_validate_browser_file_key_error(svc, tmp_path):
    auth_file = tmp_path / "bad.json"
    auth_file.write_text("{}")
    with patch(
        "shuffleupagus.services.youtube.service.YTMusic",
        side_effect=KeyError("missing"),
    ):
        assert svc._try_validate_browser_file(auth_file) is False


# ---------------------------------------------------------------------------
# _validate_browser_auth — network error path
# ---------------------------------------------------------------------------


def test_validate_browser_auth_network_error(svc):
    mock_client = MagicMock()
    mock_client._send_request.side_effect = requests.RequestException("timeout")
    assert svc._validate_browser_auth(mock_client) is False


# ---------------------------------------------------------------------------
# _setup_browser_auth — exception during setup
# ---------------------------------------------------------------------------


def test_setup_browser_auth_exception_returns_false(svc, tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setenv("YTMUSIC_HEADERS_RAW", "headers")
    with patch(
        "shuffleupagus.services.youtube.service.ytmusicapi.setup",
        side_effect=RuntimeError("setup boom"),
    ):
        assert svc._setup_browser_auth(auth_file) is False


# ---------------------------------------------------------------------------
# _load_oauth_token
# ---------------------------------------------------------------------------


def test_load_oauth_token_valid(svc, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"refresh_token":"r","access_token":"a"}')
    result = svc._load_oauth_token(token_file)
    assert result == {"refresh_token": "r", "access_token": "a"}


def test_load_oauth_token_missing_fields(svc, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text('{"some_key":"value"}')
    assert svc._load_oauth_token(token_file) is None


def test_load_oauth_token_missing_file(svc, tmp_path):
    assert svc._load_oauth_token(tmp_path / "missing.json") is None


def test_load_oauth_token_missing_file_is_quiet(svc, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        assert svc._load_oauth_token(tmp_path / "missing.json") is None
    assert caplog.text == ""


def test_load_oauth_token_invalid_json(svc, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("not json")
    assert svc._load_oauth_token(token_file) is None


@pytest.mark.parametrize("body", ["null", "0", "true", '"a string"', "[1, 2]"])
def test_load_oauth_token_non_object_json_returns_none(svc, tmp_path, body):
    """Valid JSON that is not an object is not a token, and must not raise."""
    token_file = tmp_path / "token.json"
    token_file.write_text(body)
    assert svc._load_oauth_token(token_file) is None


def test_load_oauth_token_invalid_json_warns_with_path(svc, tmp_path, caplog):
    token_file = tmp_path / "token.json"
    token_file.write_text("not json")
    with caplog.at_level(logging.WARNING):
        assert svc._load_oauth_token(token_file) is None
    assert str(token_file) in caplog.text
    assert caplog.records[0].levelno == logging.WARNING


def test_load_oauth_token_unreadable_raises_with_path_and_remedy(svc, tmp_path):
    """An unreadable token file aborts. It must not look like a missing one."""
    token_file = tmp_path / "token.json"
    token_file.write_text('{"refresh_token":"r","access_token":"a"}')
    token_file.chmod(0o000)
    try:
        with pytest.raises(RuntimeError) as caught:
            svc._load_oauth_token(token_file)
    finally:
        token_file.chmod(0o600)
    message = str(caught.value)
    assert str(token_file) in message
    assert "auth-file" in message
    assert "delete" in message


def test_load_oauth_token_undecodable_raises_rather_than_crashing(svc, tmp_path):
    """A non-UTF-8 token file is unreadable too. UnicodeDecodeError is a ValueError,
    not an OSError, so it would otherwise escape past every handler here."""
    token_file = tmp_path / "token.json"
    token_file.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(RuntimeError) as caught:
        svc._load_oauth_token(token_file)
    assert str(token_file) in str(caught.value)


def test_login_does_not_reauth_when_the_token_file_is_unreadable(svc, tmp_path):
    """The bug: a valid token got overwritten by a re-auth triggered by an I/O error."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"refresh_token":"r","access_token":"a"}')
    auth_file.chmod(0o000)
    svc.config = {"auth-file": str(auth_file), "client-id": "cid", "client-secret": "csec"}
    svc._prompt_for_oauth = MagicMock()
    try:
        with (
            patch("shuffleupagus.services.youtube.service.get_filepath", return_value=str(auth_file)),
            patch("shuffleupagus.services.youtube.service.YTMusic"),
            pytest.raises(RuntimeError, match="cannot read"),
        ):
            svc.login()
    finally:
        auth_file.chmod(0o600)
    svc._prompt_for_oauth.assert_not_called()


# ---------------------------------------------------------------------------
# _get_access_token
# ---------------------------------------------------------------------------


def test_get_access_token_from_oauth_client(svc):
    _wire_oauth_token(svc, "oauth-token-123")
    assert svc._get_access_token() == "oauth-token-123"


def test_get_access_token_from_browser_client(svc):
    svc._oauth_client = None
    svc.client._token = _mock_token("browser-token")
    assert svc._get_access_token() == "browser-token"


def test_get_access_token_none_when_no_token(svc):
    svc._oauth_client = None
    svc.client._token = None
    assert svc._get_access_token() is None


# ---------------------------------------------------------------------------
# _check_api_response
# ---------------------------------------------------------------------------


def test_check_api_response_quota_exceeded(svc):
    resp = MagicMock()
    resp.status_code = 403
    resp.content = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'
    resp.json.return_value = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    with pytest.raises(RuntimeError, match="quota exceeded"):
        svc._check_api_response(resp)


def test_check_api_response_rate_limited(svc):
    resp = MagicMock()
    resp.status_code = 429
    resp.content = b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}'
    resp.json.return_value = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
    with pytest.raises(RuntimeError, match="quota exceeded"):
        svc._check_api_response(resp)


def test_check_api_response_403_other_reason(svc):
    resp = MagicMock()
    resp.status_code = 403
    resp.content = b'{"error":{"errors":[{"reason":"forbidden"}]}}'
    resp.json.return_value = {"error": {"errors": [{"reason": "forbidden"}]}}
    with pytest.raises(RuntimeError, match="forbidden"):
        svc._check_api_response(resp)


def test_check_api_response_403_empty_body(svc):
    resp = MagicMock()
    resp.status_code = 403
    resp.content = b""
    resp.text = "Forbidden"
    resp.json.return_value = {}
    with pytest.raises(RuntimeError, match="403"):
        svc._check_api_response(resp)


def test_check_api_response_non_json_body(svc):
    """An HTML or truncated error body still reports the real HTTP failure."""
    resp = MagicMock()
    resp.status_code = 403
    resp.content = b"<html><body>Forbidden by proxy</body></html>"
    resp.text = "<html><body>Forbidden by proxy</body></html>"
    resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    with pytest.raises(RuntimeError, match="403"):
        svc._check_api_response(resp)


def test_check_api_response_json_body_not_an_object(svc):
    """A JSON body that decodes to a non-object does not crash the error path."""
    resp = MagicMock()
    resp.status_code = 429
    resp.content = b'"rate limited"'
    resp.text = "rate limited"
    resp.json.return_value = "rate limited"
    with pytest.raises(RuntimeError, match="429"):
        svc._check_api_response(resp)


def test_check_api_response_ok_passes(svc):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    svc._check_api_response(resp)
    resp.raise_for_status.assert_called_once()


# ---------------------------------------------------------------------------
# _data_api_get / _data_api_post / _data_api_delete
# ---------------------------------------------------------------------------


def test_data_api_get_no_token_raises(svc):
    svc._oauth_client = None
    svc.client._token = None
    with pytest.raises(ValueError, match="OAuth authentication"):
        svc._data_api_get("https://example.com", {})


def test_data_api_get_success(svc):
    _wire_oauth_token(svc)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"items": []}
    mock_resp.raise_for_status.return_value = None
    with patch(
        "shuffleupagus.services.youtube.service.requests.get",
        return_value=mock_resp,
    ) as mock_get:
        result = svc._data_api_get("https://api.example.com", {"key": "val"})
    assert result == {"items": []}
    mock_get.assert_called_once()


def test_data_api_post_no_token_raises(svc):
    svc._oauth_client = None
    svc.client._token = None
    with pytest.raises(ValueError, match="OAuth authentication"):
        svc._data_api_post("https://example.com", {})


def test_data_api_post_success(svc):
    _wire_oauth_token(svc)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "new"}
    mock_resp.raise_for_status.return_value = None
    with patch(
        "shuffleupagus.services.youtube.service.requests.post",
        return_value=mock_resp,
    ):
        result = svc._data_api_post("https://api.example.com", {"body": 1})
    assert result == {"id": "new"}


def test_data_api_delete_no_token_raises(svc):
    svc._oauth_client = None
    svc.client._token = None
    with pytest.raises(ValueError, match="OAuth authentication"):
        svc._data_api_delete("https://example.com", {})


def test_data_api_delete_success(svc):
    _wire_oauth_token(svc)
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.raise_for_status.return_value = None
    with patch(
        "shuffleupagus.services.youtube.service.requests.delete",
        return_value=mock_resp,
    ):
        svc._data_api_delete("https://api.example.com", {"id": "x"})


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def test_close_saves_cache(svc):
    svc.cache = MagicMock()
    svc.close()
    svc.cache.save.assert_called_once()


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_browser_only(svc, tmp_path):
    svc.config = {"auth-file": str(tmp_path / "auth.json")}
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    with (
        patch(
            "shuffleupagus.services.youtube.service.get_filepath",
            return_value=str(auth_file),
        ),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
    ):
        mock_yt.return_value = MagicMock()
        svc.login()
    assert svc.client is not None
    assert svc._oauth_client is None


def test_login_oauth_with_existing_token(svc, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"refresh_token":"r","access_token":"a"}')
    browser_file = tmp_path / "auth_browser.json"
    browser_file.write_text("{}")
    svc.config = {
        "auth-file": str(auth_file),
        "client-id": "cid",
        "client-secret": "csec",
    }
    with (
        patch(
            "shuffleupagus.services.youtube.service.get_filepath",
            return_value=str(auth_file),
        ),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
    ):
        mock_browser = MagicMock()
        mock_oauth = MagicMock()
        mock_yt.side_effect = [mock_browser, mock_oauth]
        svc.login()
    assert svc.client is mock_browser
    assert svc._oauth_client is mock_oauth


def test_login_oauth_token_missing_prompts(svc, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    svc.config = {
        "auth-file": str(auth_file),
        "client-id": "cid",
        "client-secret": "csec",
    }
    with (
        patch(
            "shuffleupagus.services.youtube.service.get_filepath",
            return_value=str(auth_file),
        ),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch.object(YoutubeService, "_prompt_for_oauth") as mock_prompt,
    ):
        mock_browser = MagicMock()
        mock_oauth = MagicMock()
        mock_yt.side_effect = [mock_browser, mock_oauth]
        svc.login()
    mock_prompt.assert_called_once()


def test_login_oauth_invalid_token_reprompts(svc, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"refresh_token":"r","access_token":"a"}')
    svc.config = {
        "auth-file": str(auth_file),
        "client-id": "cid",
        "client-secret": "csec",
    }
    with (
        patch(
            "shuffleupagus.services.youtube.service.get_filepath",
            return_value=str(auth_file),
        ),
        patch("shuffleupagus.services.youtube.service.YTMusic") as mock_yt,
        patch.object(YoutubeService, "_prompt_for_oauth") as mock_prompt,
    ):
        mock_browser = MagicMock()
        # First OAuth attempt raises, second succeeds
        mock_yt.side_effect = [
            mock_browser,
            YTMusicServerError("invalid"),
            MagicMock(),
        ]
        svc.login()
    mock_prompt.assert_called_once()


# ---------------------------------------------------------------------------
# get_artist — Artist object input path
# ---------------------------------------------------------------------------


def test_get_artist_with_artist_object_cached(svc):
    """Passing an Artist object uses artist.id for cache lookup."""
    artist_obj = Artist("UCabc", "My Artist")
    svc.cache.write("artist:UCabc", {"channelId": "UCabc", "name": "Cached"})
    result = svc.get_artist(artist_obj)
    assert result is not None
    assert result.id == "UCabc"
    assert result.name == "Cached"
    svc.client.get_artist.assert_not_called()


def test_get_artist_with_artist_object_handle_attr(svc):
    """Passing an Artist object with a handle attribute preserves it."""
    artist_obj = YoutubeArtist("UCabc", "My Artist", handle="@myband")
    svc.cache.write("artist:UCabc", {"channelId": "UCabc", "name": "Cached"})
    result = svc.get_artist(artist_obj)
    assert result is not None
    assert result.handle == "@myband"


def test_get_artist_with_artist_object_none_id_raises(svc):
    # Deliberately invalid: exercises the runtime guard, so the type error is the point.
    artist_obj = Artist(None, "Bad")  # ty: ignore[invalid-argument-type]  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="Artist ID is missing"):
        svc.get_artist(artist_obj)


# ---------------------------------------------------------------------------
# get_album_by_id
# ---------------------------------------------------------------------------


def test_get_album_by_id_success(svc):
    svc.client.get_album.return_value = {
        "browseId": "ALB1",
        "title": "Test Album",
        "year": "2023",
    }
    album = svc.get_album_by_id("ALB1")
    assert album.id == "ALB1"
    assert album.name == "Test Album"


def test_get_album_by_id_cache_hit(svc):
    svc.cache.write(
        "album:ALB1",
        {
            "browseId": "ALB1",
            "title": "Cached Album",
            "year": "2022",
        },
    )
    album = svc.get_album_by_id("ALB1")
    assert album.name == "Cached Album"
    svc.client.get_album.assert_not_called()


def test_get_album_by_id_400_raises(svc):
    svc.client.get_album.side_effect = YTMusicServerError("400 Bad Request")
    with pytest.raises(ValueError, match="HTTP 400"):
        svc.get_album_by_id("BAD")


def test_get_album_by_id_other_error_raises(svc):
    svc.client.get_album.side_effect = YTMusicServerError("500 Server Error")
    with pytest.raises(YTMusicServerError):
        svc.get_album_by_id("ERR")


def test_get_album_by_id_sanitizes_url(svc):
    svc.client.get_album.return_value = {
        "browseId": "ALB1",
        "title": "Album",
        "year": "2023",
    }
    album = svc.get_album_by_id("https://www.youtube.com/playlist/ALB1")
    svc.client.get_album.assert_called_once()
    assert album.id == "ALB1"


# ---------------------------------------------------------------------------
# get_artist_albums — error handling
# ---------------------------------------------------------------------------


def test_get_artist_albums_api_error_returns_empty(svc):
    artist = YoutubeArtist("UCabc", "Band")
    artist.browseIds["albums"] = "MPLA"
    artist.params["albums"] = "P"
    svc.client.get_artist_albums.side_effect = YTMusicServerError("500")
    albums = svc.get_artist_albums(artist)
    assert albums == []


def test_get_artist_albums_fingerprint_touch_extends_cache(svc):
    """Stale cache with matching fingerprint: cache is touched AND fingerprint is refreshed."""
    import time

    stale = [{"browseId": "MPL1", "title": "Album", "year": "2020"}]
    svc.cache.write("artist:UCabc:albums", stale, ttl=60.0)
    svc.cache.write("fingerprint:artist:UCabc", "MPL1", ttl=60.0)
    # Make both entries stale
    svc.cache._conn.execute(
        "UPDATE cache SET stored_at = ? WHERE key = ?",
        (time.time() - 3600, "artist:UCabc:albums"),
    )
    svc.cache._conn.commit()

    artist = YoutubeArtist("UCabc", "Band")
    artist.inlineAlbums = [{"browseId": "MPL1", "title": "Album", "year": "2020"}]

    albums = svc.get_artist_albums(artist)
    assert len(albums) == 1
    svc.client.get_artist_albums.assert_not_called()


# ---------------------------------------------------------------------------
# get_album_tracks — error and artist resolution paths
# ---------------------------------------------------------------------------


def test_get_album_tracks_error_returns_empty(svc):
    album = Album("MPL1", "Album")
    svc.client.get_album.side_effect = KeyError("bad")
    tracks = svc.get_album_tracks(album)
    assert tracks == []


def test_get_album_tracks_400_logs_not_on_youtube(svc, caplog):
    """An HTTP 400 is reported as a missing album, not as a generic error."""
    album = Album("MPL1", "Album")
    svc.client.get_album.side_effect = YTMusicServerError("HTTP 400: Bad Request")
    with caplog.at_level(logging.WARNING):
        assert svc.get_album_tracks(album) == []
    assert "is not on YouTube Music" in caplog.text


def test_get_album_tracks_non_400_reports_real_error(svc, caplog):
    """A non-400 failure reports the underlying error instead of claiming HTTP 400."""
    album = Album("MPL1", "Album")
    svc.client.get_album.side_effect = YTMusicServerError("quotaExceeded")
    with caplog.at_level(logging.WARNING):
        assert svc.get_album_tracks(album) == []
    assert "quotaExceeded" in caplog.text
    assert "400" not in caplog.text


def test_get_album_tracks_resolves_artists(svc):
    album = Album("MPL1", "Album")
    svc.client.get_album.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Song",
                "duration_seconds": 200,
                "artists": [{"id": "UCabc", "name": "Artist"}],
            },
        ]
    }
    svc.client.get_artist.return_value = {
        "channelId": "UCabc",
        "name": "Artist",
    }
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 1
    assert len(tracks[0].artists) == 1
    assert tracks[0].artists[0].name == "Artist"


def test_get_album_tracks_artist_none_id_skipped(svc):
    album = Album("MPL1", "Album")
    svc.client.get_album.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Song",
                "duration_seconds": 200,
                "artists": [{"id": None, "name": "Unknown"}],
            },
        ]
    }
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 1
    assert len(tracks[0].artists) == 0


def test_get_album_tracks_artist_resolution_returns_none(svc):
    album = Album("MPL1", "Album")
    svc.client.get_album.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Song",
                "duration_seconds": 200,
                "artists": [{"id": "UCgone", "name": "Gone"}],
            },
        ]
    }
    svc.client.get_artist.side_effect = YTMusicServerError("500")
    tracks = svc.get_album_tracks(album)
    assert len(tracks) == 1
    assert len(tracks[0].artists) == 0


# ---------------------------------------------------------------------------
# get_artist_tracks
# ---------------------------------------------------------------------------


def test_get_artist_tracks_aggregates_albums(svc):
    artist = YoutubeArtist("UCabc", "Band")
    artist.inlineAlbums = [{"browseId": "MPL1", "title": "Album 1", "year": "2020"}]

    svc.client.get_album.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Song 1",
                "duration_seconds": 200,
                "artists": [],
            },
        ]
    }

    svc._album_pool = ThreadPoolExecutor(max_workers=1)
    tracks = svc.get_artist_tracks(artist)
    assert len(tracks) == 1
    assert tracks[0].id == "vid1"


def test_get_artist_tracks_no_albums_returns_empty(svc):
    artist = YoutubeArtist("UCabc", "Band")
    tracks = svc.get_artist_tracks(artist)
    assert tracks == []


def test_get_artist_tracks_handles_album_error(svc):
    artist = YoutubeArtist("UCabc", "Band")
    artist.inlineAlbums = [{"browseId": "MPL1", "title": "Album 1", "year": "2020"}]
    svc.client.get_album.side_effect = RuntimeError("boom")
    svc._album_pool = ThreadPoolExecutor(max_workers=1)
    tracks = svc.get_artist_tracks(artist)
    assert tracks == []


# ---------------------------------------------------------------------------
# get_artist_top_tracks
# ---------------------------------------------------------------------------


def test_get_artist_top_tracks_returns_empty(svc):
    artist = Artist("UCabc", "Band")
    assert svc.get_artist_top_tracks(artist) == []


# ---------------------------------------------------------------------------
# sync — pagination path
# ---------------------------------------------------------------------------


def test_sync_paginates_existing_items(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")

    def _get(_url, params):
        if params.get("part") == "contentDetails":
            return {"items": [{"contentDetails": {"videoId": "vid1"}}]}
        if params.get("pageToken") == "page2":
            return {"items": [{"id": "item2"}]}
        return {"items": [{"id": "item1"}], "nextPageToken": "page2"}

    svc._data_api_get = MagicMock(side_effect=_get)
    svc._data_api_delete = MagicMock()
    svc._data_api_post = MagicMock()
    svc.sync("Playlist", ["vid1"])
    assert svc._data_api_delete.call_count == 2
    assert svc._data_api_post.call_count == 1


def test_sync_no_tracks(svc):
    svc.get_playlist_id_for_name = MagicMock(return_value="pl1")
    svc._data_api_get = MagicMock(return_value={"items": []})
    svc._data_api_delete = MagicMock()
    svc._data_api_post = MagicMock()
    svc.sync("Playlist")
    svc._data_api_delete.assert_not_called()
    svc._data_api_post.assert_not_called()


# ---------------------------------------------------------------------------
# get_channel_id — cached handle for bare channel ID
# ---------------------------------------------------------------------------


def test_get_channel_id_bare_id_returns_cached_handle(svc):
    """Bare channel ID with a cached handle returns the handle."""
    svc.cache.write("channel:handle:UCabc", "@cached_handle")
    svc.client.get_artist.return_value = {"channelId": "UCabc", "name": "Band"}
    artist = svc.get_artist("UCabc")
    assert artist is not None
    assert artist.handle == "@cached_handle"


# ---------------------------------------------------------------------------
# _prompt_for_oauth
# ---------------------------------------------------------------------------


def test_prompt_for_oauth_delegates_to_refreshing_token(svc, tmp_path):
    auth_file = tmp_path / "oauth.json"
    creds = MagicMock()
    with patch("shuffleupagus.services.youtube.service.RefreshingToken.prompt_for_token") as mock_prompt:
        svc._prompt_for_oauth(creds, auth_file)
    mock_prompt.assert_called_once_with(creds, open_browser=True, to_file=str(auth_file))


# ---------------------------------------------------------------------------
# Malformed responses (#59)
# ---------------------------------------------------------------------------


def test_get_album_tracks_rejects_a_non_list_tracks(svc):
    svc.client.get_album.return_value = {"tracks": {"not": "a list"}}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="tracks is not a list"):
        svc.get_album_tracks(album)


def test_get_album_tracks_rejects_a_non_object_entry(svc):
    svc.client.get_album.return_value = {"tracks": ["oops"]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="not an object"):
        svc.get_album_tracks(album)


def test_get_album_tracks_rejects_a_missing_video_id(svc):
    svc.client.get_album.return_value = {"tracks": [{"title": "T", "duration_seconds": 10}]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="videoId is missing"):
        svc.get_album_tracks(album)


def test_get_album_tracks_rejects_a_non_numeric_duration(svc):
    svc.client.get_album.return_value = {"tracks": [{"videoId": "v1", "title": "T", "duration_seconds": {"s": 10}}]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="not a number"):
        svc.get_album_tracks(album)


def test_get_album_tracks_missing_tracks_is_empty(svc):
    svc.client.get_album.return_value = {"other": []}
    album = MagicMock(id="alb1", name="Album")
    assert svc.get_album_tracks(album) == []


def test_get_playlist_id_for_name_rejects_a_non_object_entry(svc):
    svc._data_api_get = MagicMock(return_value={"items": ["oops"]})
    with pytest.raises(ApiResponseError, match="not an object"):
        svc.get_playlist_id_for_name("My Playlist")


def test_get_playlist_id_for_name_rejects_a_missing_title(svc):
    svc._data_api_get = MagicMock(return_value={"items": [{"id": "p1", "snippet": {}}]})
    with pytest.raises(ApiResponseError, match=r"snippet\.title is missing"):
        svc.get_playlist_id_for_name("My Playlist")


def test_malformed_response_names_the_service(svc):
    svc.client.get_album.return_value = {"tracks": ["oops"]}
    album = MagicMock(id="alb1", name="Album")
    with pytest.raises(ApiResponseError, match="YouTube"):
        svc.get_album_tracks(album)


@pytest.mark.parametrize("cached", [42, "a string", {"not": "a list"}])
def test_get_artist_albums_rejects_a_non_list_cache_hit(svc, cached):
    """A cache hit is the same untrusted JSON as the response it came from."""
    artist = YoutubeArtist("c1", "Artist")
    svc.cache.write("artist:" + artist.id + ":albums", cached)
    with pytest.raises(ApiResponseError, match="not a list"):
        svc.get_artist_albums(artist)
