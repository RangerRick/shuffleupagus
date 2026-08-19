import json
import os
import re
import sys
from concurrent.futures import as_completed
from pathlib import Path

import requests
import ytmusicapi
from ytmusicapi import YTMusic
from ytmusicapi.auth.oauth import OAuthCredentials, RefreshingToken
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from ...core.apiresponse import api_array, api_has, api_int, api_list, api_object, api_str
from ...core.config import get_filepath
from ...core.model import Album, Artist, Service, Track
from ...core.util import logger
from .model import YoutubeAlbum, YoutubeArtist, YoutubeTrack

# Fingerprint TTL: how often to re-check whether a new album has appeared.
_FINGERPRINT_TTL = 60 * 60 * 24  # 24 hours

# Named in every message raised from an unexpected API response.
_SERVICE_LABEL = "YouTube"

channelUrl = re.compile(
    r'^.*<link rel="canonical" href="https://www\.youtube\.com/channel/([^\"]+)".*$',
    re.MULTILINE | re.DOTALL,
)
browseId = re.compile(r'^.*"browseId":"([^\"]+)".*$', re.MULTILINE | re.DOTALL)
channelHandle = re.compile(r'"canonicalBaseUrl":"(/@[^"]+)"')


class YoutubeService(Service):
    name = "youtube"
    cache_cutoff = 60 * 60 * 24 * 90  # 90 days — artist/album data is stable; keeps cache warm across OAuth refreshes

    client: YTMusic  # browser-auth client for browsing artists/albums
    _oauth_client: YTMusic | None = None  # OAuth client for Data API (playlist sync)

    def _require_config(self, key: str) -> str:
        val = self.config.get(key)
        if val is None:
            raise ValueError(f"Missing required config key 'services.youtube.{key}'")
        return val

    def _is_browser_auth(self) -> bool:
        return not (self.config.get("client-id") and self.config.get("client-secret"))

    def _auth_file(self) -> Path:
        return Path(get_filepath(self._require_config("auth-file")))

    def _browser_auth_file(self) -> Path:
        """Browser cookie file — same as auth-file for browser-only auth,
        separate file when OAuth is configured (since auth-file holds the
        OAuth token)."""
        base = self._auth_file()
        if self._is_browser_auth():
            return base
        return base.with_stem(base.stem + "_browser")

    _MAX_AUTH_ATTEMPTS = 3
    _MAX_VERIFY_ROUNDS = 3

    def preflight(self):
        browser_file = self._browser_auth_file()
        if not browser_file.exists():
            logger.warning(
                f"{self.tag}* browser cookie file not found ({browser_file.name}), starting setup",
            )
            self._setup_browser_auth_with_retry(browser_file)
            logger.info(f"{self.tag}* browser cookies validated successfully")
            return

        if not self._try_validate_browser_file(browser_file):
            logger.warning(
                f"{self.tag}* browser cookies expired or invalid, starting re-auth",
            )
            self._setup_browser_auth_with_retry(browser_file)

        logger.info(f"{self.tag}* browser cookies validated successfully")

    def _try_validate_browser_file(self, browser_file: Path) -> bool:
        """Try to load and validate browser cookies. Returns False on any failure."""
        try:
            client = YTMusic(str(browser_file))
        except (YTMusicUserError, YTMusicServerError, KeyError) as exc:
            logger.warning(f"{self.tag}* browser cookie file is invalid: {exc}")
            return False
        return self._validate_browser_auth(client)

    def _setup_browser_auth_with_retry(self, browser_file: Path) -> None:
        """Run browser auth setup, validate, and retry on failure."""
        for attempt in range(1, self._MAX_AUTH_ATTEMPTS + 1):
            if not self._setup_browser_auth(browser_file):
                raise ValueError("YouTube browser auth setup failed")
            if self._try_validate_browser_file(browser_file):
                return
            logger.warning(
                f"{self.tag}* browser cookies invalid after setup"
                f" (attempt {attempt}/{self._MAX_AUTH_ATTEMPTS}),"
                " please try again",
            )
            browser_file.unlink(missing_ok=True)
        raise ValueError(
            f"YouTube browser auth failed after {self._MAX_AUTH_ATTEMPTS} attempts",
        )

    def login(self):
        # Browser-auth client for browsing artists/albums (always needed)
        browser_file = self._browser_auth_file()
        self.client = YTMusic(str(browser_file))

        # OAuth client for Data API playlist management (optional)
        if not self._is_browser_auth():
            auth_file = self._auth_file()
            client_id = self._require_config("client-id")
            client_secret = self._require_config("client-secret")
            creds = OAuthCredentials(client_id, client_secret)
            if self._load_oauth_token(auth_file) is None:
                logger.warning(f"{self.tag}* YouTube auth token missing or not OAuth format; starting login flow")
                self._prompt_for_oauth(creds, auth_file)
            try:
                self._oauth_client = YTMusic(str(auth_file), oauth_credentials=creds)
            except YTMusicServerError:
                logger.warning(f"{self.tag}* YouTube auth token invalid; starting login flow")
                self._prompt_for_oauth(creds, auth_file)
                self._oauth_client = YTMusic(str(auth_file), oauth_credentials=creds)
        logger.info(f"{self.tag}* logged in")

    def _validate_browser_auth(self, client: YTMusic) -> bool:
        """Check whether browser cookies are still valid by hitting the browse endpoint."""
        try:
            response = client._send_request("browse", {"browseId": "UCMDQxm7cUx3yXkFeHa5zrBA"})
        except YTMusicServerError as exc:
            logger.warning(f"{self.tag}* browser auth validation failed (server error): {exc}")
            return False
        except requests.RequestException as exc:
            logger.warning(f"{self.tag}* browser auth validation failed (network error): {exc}")
            return False
        # A successful authenticated response includes tracking params with
        # logged_in=1. The response body structure varies by account type
        # (brand/creator accounts get a different format), so we only check
        # that the server accepted our auth — not that parsing succeeds.
        tracking = response.get("responseContext", {}).get("serviceTrackingParams", [])
        for service in tracking:
            for param in service.get("params", []):
                if param.get("key") == "logged_in" and param.get("value") == "1":
                    return True
        logger.warning(f"{self.tag}* browser auth validation failed: not logged in")
        return False

    _BROWSER_AUTH_INSTRUCTIONS = (
        "Browser cookie auth is required to browse YouTube Music artist pages.\n"
        "\n"
        "To get your raw request headers:\n"
        "  1. Open https://music.youtube.com in Firefox or Chrome\n"
        "  2. Make sure you are logged in\n"
        "  3. Open DevTools (F12) → Network tab\n"
        "  4. Click on any request to music.youtube.com\n"
        "  5. Right-click the request → Copy → Copy Request Headers (raw)\n"
        "     The raw headers look like 'key: value' pairs, one per line.\n"
        "\n"
        "Paste the raw headers below, then press Enter followed by Ctrl-D (EOF).\n"
        "\n"
        "Tip: if pasting stalls in your terminal, copy the headers to your\n"
        "clipboard and use the env var instead:\n"
        '  export YTMUSIC_HEADERS_RAW="$(pbpaste)" && shuffleupagus\n'
    )

    def _setup_browser_auth(self, auth_file: Path) -> bool:
        """Walk the user through browser cookie setup. Returns True on success."""
        headers_raw = os.environ.get("YTMUSIC_HEADERS_RAW")
        try:
            if headers_raw:
                ytmusicapi.setup(filepath=str(auth_file), headers_raw=headers_raw)
            elif sys.stdin.isatty():
                print(self._BROWSER_AUTH_INSTRUCTIONS)
                ytmusicapi.setup(filepath=str(auth_file))
            else:
                logger.error(
                    f"{self.tag}* browser cookies expired and stdin is not"
                    " a TTY — set YTMUSIC_HEADERS_RAW or re-run interactively"
                )
                return False
        except Exception:
            logger.exception(f"{self.tag}* browser auth setup failed")
            return False
        return True

    def _load_oauth_token(self, auth_file: Path) -> dict | None:
        """Return the token dict, or None when there is no usable token.

        None means "start the login flow": either no file, or a file whose
        contents are not an OAuth token. A file that exists but cannot be read
        raises instead, because re-authenticating would overwrite it.
        """
        try:
            data = json.loads(auth_file.read_text())
            # A truncated or zeroed file parses as null/0/true, and the membership
            # test below raises TypeError on those. That is the same corruption
            # the JSONDecodeError branch exists for, arriving through another door.
            if isinstance(data, dict) and "refresh_token" in data and "access_token" in data:
                return data
        except FileNotFoundError:
            pass
        except json.JSONDecodeError as exc:
            logger.warning(f"{self.tag}* ignoring corrupt OAuth token file {auth_file}: {exc}")
        except UnicodeDecodeError as exc:
            # Also unreadable, and also not recoverable by re-authenticating.
            # UnicodeDecodeError is a ValueError, so it would otherwise slip past
            # both the JSONDecodeError and the OSError branch.
            raise RuntimeError(
                f"OAuth token file {auth_file} is not valid UTF-8: {exc}. Delete it to force a fresh login."
            ) from exc
        except OSError as exc:
            # Returning None here would be indistinguishable from "no token
            # file", and the caller answers that by running the whole device-code
            # flow and writing the result back over this same path. On a
            # transient I/O error that destroys a working refresh token; on a
            # permissions error the write fails too and it loops.
            raise RuntimeError(
                f"cannot read OAuth token file {auth_file}: {exc}. "
                f"Check the file's permissions and the 'auth-file' config value, "
                f"or delete it to force a fresh login."
            ) from exc
        return None

    def _prompt_for_oauth(self, creds: OAuthCredentials, auth_file: Path) -> None:
        """Run the device-code OAuth flow and save token to auth_file."""
        RefreshingToken.prompt_for_token(creds, open_browser=True, to_file=str(auth_file))

    def _get_access_token(self) -> str | None:
        """Return the current OAuth access token, auto-refreshing if needed. None if not OAuth."""
        client = self._oauth_client or self.client
        token = getattr(client, "_token", None)
        if token is not None:
            return token.access_token
        return None

    def _check_api_response(self, resp: requests.Response) -> None:
        """Raise RuntimeError with details for YouTube API errors."""
        if resp.status_code in (403, 429):
            # An error body is not guaranteed to be JSON — a proxy or gateway can
            # answer with HTML, and a truncated response fails to parse. Treat any
            # parse failure as an empty body so the status/resp.text path below
            # still reports the real HTTP failure instead of a JSONDecodeError.
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            reason = ""
            for err in body.get("error", {}).get("errors", []):
                reason = err.get("reason", "")
            if reason in ("quotaExceeded", "rateLimitExceeded"):
                raise RuntimeError(f"YouTube API quota exceeded ({reason}). Quota resets at midnight Pacific Time.")
            raise RuntimeError(f"YouTube API {resp.status_code}: {reason or resp.text}")
        resp.raise_for_status()

    def _data_api_get(self, url: str, params: dict) -> dict:
        """Authenticated GET to YouTube Data API v3."""
        access_token = self._get_access_token()
        if not access_token:
            raise ValueError("YouTube Data API v3 requires OAuth authentication")
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=params, timeout=30)
        self._check_api_response(resp)
        return resp.json()

    def _data_api_post(self, url: str, body: dict, params: dict | None = None) -> dict:
        """Authenticated POST to YouTube Data API v3."""
        access_token = self._get_access_token()
        if not access_token:
            raise ValueError("YouTube Data API v3 requires OAuth authentication")
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=body,
            params=params or {},
            timeout=30,
        )
        self._check_api_response(resp)
        return resp.json()

    def _data_api_delete(self, url: str, params: dict) -> None:
        """Authenticated DELETE to YouTube Data API v3."""
        access_token = self._get_access_token()
        if not access_token:
            raise ValueError("YouTube Data API v3 requires OAuth authentication")
        resp = requests.delete(url, headers={"Authorization": f"Bearer {access_token}"}, params=params, timeout=30)
        self._check_api_response(resp)

    def sanitize_id(self, id: str) -> str:
        """Normalize a user-supplied YouTube reference to a bare ID or @handle.

        Deliberately NOT the same as youtube.model.sanitize_id(), which strips
        the "youtube:"/"artist:"/"album:"/"track:" prefixes carried by cached
        model IDs. This one understands youtube.com URLs and @handles, which is
        what users actually paste into the config. Editing one does not change
        the other — pick the one matching the input you have.
        """
        if id.startswith(("http://", "https://", "youtube.com", "www.youtube.com")):
            url = id.removeprefix("https://").removeprefix("http://").removeprefix("www.")
            if url.startswith("youtube.com/@"):
                return "@" + url.removeprefix("youtube.com/@").split("?")[0].split("/")[0]
            return url.rsplit("/", 1)[-1].split("?")[0]
        return id

    def __get_channel_id(self, artist: str) -> tuple[str, str | None]:
        """Resolve an artist handle/ID to (channel_id, handle). Results are cached."""
        artist = self.sanitize_id(artist)

        # Bare channel IDs (UCxxxx…) need no HTTP fetch
        if not artist.startswith("@") and not artist.startswith("http"):
            handle = self.cache.read("channel:handle:" + artist)
            return artist, handle

        cached_id = self.cache.read("channel:" + artist)
        if cached_id:
            handle = self.cache.read("channel:handle:" + cached_id)
            if not handle and artist.startswith("@"):
                handle = artist
            return cached_id, handle

        artist_url = "https://www.youtube.com/" + artist if artist.startswith("@") else artist

        logger.info(f"{self.tag}* resolving artist handle: {artist}")
        response = requests.get(artist_url, timeout=30)
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError(
                f"Failed to fetch channel page for artist handle: {artist} (status code: {response.status_code})",
            )

        html = response.text
        channel_id = None
        for r in [channelUrl, browseId]:
            m = r.match(html)
            if m:
                channel_id = m.group(1)
                break

        if not channel_id or not channel_id.startswith("UC"):
            raise ValueError(f"Could not extract a valid channel ID for artist handle: {artist} (got: {channel_id!r})")

        handle = artist if artist.startswith("@") else None
        if not handle:
            m = channelHandle.search(html)
            if m:
                handle = m.group(1)  # e.g. "/@artistname"

        permanent = 60 * 60 * 24 * 365 * 10.0  # ~10 years
        self.cache.write("channel:" + artist, channel_id, ttl=permanent)
        if handle:
            self.cache.write("channel:handle:" + channel_id, handle, ttl=permanent)

        logger.debug(f"{self.tag}* resolved {artist} to channel ID: {channel_id} (handle: {handle})")
        return channel_id, handle

    def get_artist(self, artist: str | Artist) -> YoutubeArtist | None:
        if isinstance(artist, str):
            original = artist
            try:
                artist_id, handle = self.__get_channel_id(artist)
            except ValueError as e:
                logger.warning(f"{self.tag}* could not resolve artist handle '{original}': {e}, skipping")
                return None
            artist_obj = None
        else:
            artist_id = artist.id
            handle = getattr(artist, "handle", None)
            original = handle or artist.name
            artist_obj = artist

        if artist_id is None:
            raise ValueError("Artist ID is missing")

        cache_key = "artist:" + artist_id
        logger.debug(f"{self.tag}* fetching artist info for ID: {artist_id} (cache key: {cache_key})")
        ret = self.cache.read(cache_key)
        if not ret:
            if artist_obj:
                ret = artist_obj
            else:
                try:
                    ret = self.client.get_artist(artist_id)
                except (KeyError, YTMusicServerError) as e:
                    if "400" in str(e):
                        logger.warning(
                            f"{self.tag}* {original} (channel: {artist_id}): YouTube Music API returned "
                            f"HTTP 400 — this artist may not have a YouTube Music page, or your "
                            f"browser cookies may lack access to browse artist pages.",
                        )
                    else:
                        logger.warning(
                            f"{self.tag}* {original} has no YouTube Music page ({e}), skipping (channel: {artist_id})",
                        )
                    return None
                self.cache.write(cache_key, ret)

        ya = YoutubeArtist.from_dict(ret)
        ya.handle = handle
        return ya

    def get_album_by_id(self, album_id: str) -> Album:
        album_id = self.sanitize_id(album_id)

        cache_key = "album:" + album_id
        ret = self.cache.read(cache_key)
        if not ret:
            try:
                ret = self.client.get_album(album_id)
            except (KeyError, YTMusicServerError) as e:
                if "400" in str(e):
                    raise ValueError(
                        f"YouTube Music API returned HTTP 400 for album {album_id}",
                    ) from e
                raise
            self.cache.write(cache_key, ret)

        return YoutubeAlbum.from_dict(ret)

    def get_artist_albums(self, artist: Artist) -> list[Album]:
        cache_key = "artist:" + artist.id + ":albums"
        fp_key = "fingerprint:artist:" + artist.id

        assert isinstance(artist, YoutubeArtist)
        albums_browse_id = artist.browseIds.get("albums")
        albums_params = artist.params.get("albums")

        logger.debug(f"{self.tag}* fetching albums for artist ID: {artist.id} (cache key: {cache_key})")
        albums = []

        ret = self.cache.read(cache_key)
        if ret is None:
            # Derive the current "latest album" fingerprint from the get_artist response,
            # which is already in memory — no extra API call required.
            inline = artist.inlineAlbums
            current_fp = api_object(inline[0], "inline albums[0]", _SERVICE_LABEL).get("browseId") if inline else None

            stale = self.cache.read_stale(cache_key)
            if stale is not None and current_fp is not None:
                cached_fp = self.cache.read_stale(fp_key)
                if cached_fp == current_fp:
                    logger.debug(f"{self.tag}* fingerprint match for {artist.name}, extending cache")
                    self.cache.touch(cache_key)
                    self.cache.write(fp_key, current_fp, ttl=_FINGERPRINT_TTL)
                    ret = stale

        if ret is None:
            if albums_browse_id is not None and albums_params is not None:
                try:
                    ret = self.client.get_artist_albums(albums_browse_id, albums_params, limit=100)
                except (KeyError, YTMusicServerError) as e:
                    logger.warning(
                        f"{self.tag}* HTTP error fetching album list for {artist.name}: {e}",
                    )
                    return []
            elif artist.inlineAlbums:
                # all albums are already embedded in the get_artist response
                ret = artist.inlineAlbums
            else:
                logger.debug(f"{self.tag}* artist {artist.name} has no albums, skipping")
                return []
            self.cache.write(cache_key, ret)
            inline = artist.inlineAlbums
            if inline:
                first = api_object(inline[0], "inline albums[0]", _SERVICE_LABEL)
                self.cache.write(fp_key, api_str(first, ("browseId",), _SERVICE_LABEL), ttl=_FINGERPRINT_TTL)

        if ret:
            # Checked after the branches merge: ret is either a cache hit, a
            # get_artist_albums response, or the inline albums off the artist.
            for album in api_array(ret, "artist albums", _SERVICE_LABEL):
                albums.append(YoutubeAlbum.from_dict(album))

        return albums

    def get_album_tracks(self, album: Album) -> list[Track]:
        cache_key = "album:" + album.id
        ret = self.cache.read(cache_key)
        if not ret:
            try:
                ret = self.client.get_album(album.id)
            except (KeyError, YTMusicServerError) as e:
                # Report what actually failed. A blanket "HTTP 400" here hid
                # quota and auth errors behind a routine "album not found".
                if "400" in str(e):
                    logger.warning(
                        f"{self.tag}* album '{album.name}' ({album.id}) is not on YouTube Music, skipping",
                    )
                else:
                    logger.warning(
                        f"{self.tag}* error fetching album '{album.name}' ({album.id}): {e}, skipping",
                    )
                return []
            self.cache.write(cache_key, ret)

        tracks: list[Track] = []
        # get_album always carries "tracks"; an absent one is a malformed
        # response, not an album with no songs, and caching it as empty would
        # hide the album for the life of the entry.
        if ret is not None:
            for track in api_list(ret, ("tracks",), _SERVICE_LABEL):
                track = api_object(track, "tracks[] entry", _SERVICE_LABEL)
                youtubeTrack = YoutubeTrack(
                    id=api_str(track, ("videoId",), _SERVICE_LABEL),
                    name=api_str(track, ("title",), _SERVICE_LABEL),
                    duration_ms=api_int(track, ("duration_seconds",), _SERVICE_LABEL) * 1000,
                    album=album,
                )
                raw_artists = api_list(track, ("artists",), _SERVICE_LABEL) if api_has(track, "artists") else []
                for artist in raw_artists:
                    entry = api_object(artist, "tracks[].artists[] entry", _SERVICE_LABEL)
                    if entry.get("id") is not None:
                        resolved_artist = self.get_artist(api_str(entry, ("id",), _SERVICE_LABEL))
                        if resolved_artist is not None:
                            youtubeTrack.artists.append(resolved_artist)
                tracks.append(youtubeTrack)

        return tracks

    def get_artist_tracks(self, artist: Artist) -> list[Track]:
        albums = self.get_artist_albums(artist)
        if not albums:
            return []

        tracks: list[Track] = []
        futures = {self.album_pool.submit(self.get_album_tracks, album): album for album in albums}
        for future in as_completed(futures):
            album = futures[future]
            try:
                tracks += future.result()
            except Exception:
                logger.exception(
                    f"{self.tag}  ! error fetching tracks for album '{album.name}' (artist: {artist.name}), skipping"
                )

        return tracks

    # TODO: build a popularity list by pulling all albums; skipped for now due to complexity vs. value
    def get_artist_top_tracks(self, artist: Artist) -> list[Track]:
        return []

    def get_playlist_id_for_name(self, playlist_name: str) -> str:
        """Find a playlist by exact name in the user's YouTube library via Data API v3."""
        page_token = None
        while True:
            params: dict = {"part": "snippet", "mine": "true", "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            data = self._data_api_get("https://www.googleapis.com/youtube/v3/playlists", params)
            data = api_object(data, "playlists response", _SERVICE_LABEL)
            for item in api_list(data, ("items",), _SERVICE_LABEL):
                entry = api_object(item, "items[] entry", _SERVICE_LABEL)
                if api_str(entry, ("snippet", "title"), _SERVICE_LABEL) == playlist_name:
                    return api_str(entry, ("id",), _SERVICE_LABEL)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        raise ValueError(f"Playlist not found: {playlist_name}")

    def __get_playlist_items(self, playlist_id: str, part: str) -> list[dict]:
        """Read every playlistItems entry for a playlist, following pagination."""
        items: list[dict] = []
        page_token = None
        while True:
            params: dict = {"part": part, "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            data = self._data_api_get("https://www.googleapis.com/youtube/v3/playlistItems", params)
            # Checked before either read: api_has answers False for a list, and
            # the .get below would then raise AttributeError — the raw failure
            # these helpers replace, one line after the guard.
            data = api_object(data, "playlistItems response", _SERVICE_LABEL)
            # "items" is mandatory, and load-bearing: __verify_playlist treats
            # what this returns as the complete playlist. A page read as empty
            # ends the loop early, and every entry past that point then looks
            # missing and gets re-added as a duplicate in the user's playlist.
            page = api_list(data, ("items",), _SERVICE_LABEL)
            items.extend(api_object(item, "items[] entry", _SERVICE_LABEL) for item in page)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    def __add_video(self, playlist_id: str, video_id: str) -> None:
        self._data_api_post(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet"},
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )

    def __verify_playlist(self, playlist_id: str, expected_ids: list[str]) -> None:
        """Confirm every video reached the playlist, re-adding any that did not."""
        expected = set(expected_ids)
        for round_num in range(self._MAX_VERIFY_ROUNDS + 1):
            # No api_has filter: part=contentDetails was requested, so an entry
            # without contentDetails.videoId is malformed. Dropping it made the
            # video count as missing and get re-added, duplicating it.
            actual = {
                api_str(item, ("contentDetails", "videoId"), _SERVICE_LABEL)
                for item in self.__get_playlist_items(playlist_id, "contentDetails")
            }
            missing = expected - actual
            if not missing:
                logger.info(f"{self.tag}  * verified {len(expected)} tracks in playlist")
                return
            if round_num == self._MAX_VERIFY_ROUNDS:
                raise RuntimeError(
                    f"{len(missing)} tracks could not be verified in the YouTube playlist "
                    f"after {self._MAX_VERIFY_ROUNDS} re-add rounds: {sorted(missing)}"
                )
            logger.warning(
                f"{self.tag}  ! {len(missing)} tracks missing after sync, "
                f"re-adding (round {round_num + 1}/{self._MAX_VERIFY_ROUNDS})"
            )
            for video_id in sorted(missing):
                self.__add_video(playlist_id, video_id)

    def sync(self, playlist_name: str, tracks: list[str] | None = None):
        playlist_id = self.get_playlist_id_for_name(playlist_name)

        existing_item_ids = [
            api_str(item, ("id",), _SERVICE_LABEL) for item in self.__get_playlist_items(playlist_id, "id")
        ]
        logger.debug(f"{self.tag}  * removing {len(existing_item_ids)} existing items from playlist")
        for item_id in existing_item_ids:
            self._data_api_delete(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params={"id": item_id},
            )

        expected_ids = list(tracks or [])
        logger.debug(f"{self.tag}  * adding {len(expected_ids)} new items to playlist")
        for video_id in expected_ids:
            self.__add_video(playlist_id, video_id)

        if expected_ids:
            self.__verify_playlist(playlist_id, expected_ids)
