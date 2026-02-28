import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from ytmusicapi import YTMusic
from ytmusicapi.auth.oauth import OAuthCredentials, RefreshingToken
from ytmusicapi.exceptions import YTMusicServerError

from ...core.config import get_filepath
from ...core.model import Album, Artist, Service, Track
from ...core.util import logger
from .model import YoutubeAlbum, YoutubeArtist, YoutubeTrack

# Fingerprint TTL: how often to re-check whether a new album has appeared.
_FINGERPRINT_TTL = 60 * 60 * 24  # 24 hours

channelUrl = re.compile(
    r'^.*<link rel="canonical" href="https://www\.youtube\.com/channel/([^\"]+)".*$',
    re.MULTILINE | re.DOTALL,
)
browseId = re.compile(r'^.*"browseId":"([^\"]+)".*$', re.MULTILINE | re.DOTALL)
channelHandle = re.compile(r'"canonicalBaseUrl":"(/@[^"]+)"')


class YoutubeService(Service):
    name = "youtube"
    cache_cutoff = 60 * 60 * 24 * 90  # 90 days — artist/album data is stable; keeps cache warm across OAuth refreshes

    client: YTMusic

    def _require_config(self, key: str) -> str:
        val = self.config.get(key)
        if val is None:
            raise ValueError(f"Missing required config key 'services.youtube.{key}'")
        return val

    def login(self):
        auth_file = Path(get_filepath(self._require_config("auth-file")))
        client_id = self.config.get("client-id")
        client_secret = self.config.get("client-secret")

        if client_id and client_secret:
            creds = OAuthCredentials(client_id, client_secret)
            if self._load_oauth_token(auth_file) is None:
                logger.warning(f"{self.tag}* YouTube auth token missing or not OAuth format; starting login flow")
                self._prompt_for_oauth(creds, auth_file)
            try:
                self.client = YTMusic(str(auth_file), oauth_credentials=creds)
            except YTMusicServerError:
                logger.warning(f"{self.tag}* YouTube auth token invalid; starting login flow")
                self._prompt_for_oauth(creds, auth_file)
                self.client = YTMusic(str(auth_file), oauth_credentials=creds)
        else:
            self.client = YTMusic(str(auth_file))

    def _load_oauth_token(self, auth_file: Path) -> dict | None:
        """Return token dict if file exists and looks like an OAuth token, else None."""
        try:
            data = json.loads(auth_file.read_text())
            if "refresh_token" in data and "access_token" in data:
                return data
        except FileNotFoundError, json.JSONDecodeError, OSError:
            pass
        return None

    def _prompt_for_oauth(self, creds: OAuthCredentials, auth_file: Path) -> None:
        """Run the device-code OAuth flow and save token to auth_file."""
        RefreshingToken.prompt_for_token(creds, open_browser=True, to_file=str(auth_file))

    def _get_access_token(self) -> str | None:
        """Return the current OAuth access token, auto-refreshing if needed. None if not OAuth."""
        token = getattr(self.client, "_token", None)
        if token is not None:
            return token.access_token
        return None

    def _data_api_get(self, url: str, params: dict) -> dict:
        """Authenticated GET to YouTube Data API v3."""
        access_token = self._get_access_token()
        if not access_token:
            raise ValueError("YouTube Data API v3 requires OAuth authentication")
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, params=params, timeout=30)
        resp.raise_for_status()
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
        resp.raise_for_status()
        return resp.json()

    def _data_api_delete(self, url: str, params: dict) -> None:
        """Authenticated DELETE to YouTube Data API v3."""
        access_token = self._get_access_token()
        if not access_token:
            raise ValueError("YouTube Data API v3 requires OAuth authentication")
        resp = requests.delete(url, headers={"Authorization": f"Bearer {access_token}"}, params=params, timeout=30)
        resp.raise_for_status()

    def close(self):
        self.cache.save()

    def sanitize_id(self, id: str) -> str:
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

        logger.debug(f"{self.tag}* fetching channel ID for artist handle: {artist} (URL: {artist_url})")
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

        self.cache.write("channel:" + artist, channel_id)
        if handle:
            self.cache.write("channel:handle:" + channel_id, handle)

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
                            f"HTTP 400 — this artist is not cached and OAuth cannot browse YT Music. "
                            f"Re-run once with browser-cookie auth to warm the cache.",
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
            ret = self.client.get_album(album_id)
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
            current_fp = inline[0]["browseId"] if inline else None

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
                # artist has a paginated album list; fetch all
                ret = self.client.get_artist_albums(albums_browse_id, albums_params, limit=100)
            elif artist.inlineAlbums:
                # all albums are already embedded in the get_artist response
                ret = artist.inlineAlbums
            else:
                logger.debug(f"{self.tag}* artist {artist.name} has no albums, skipping")
                return []
            self.cache.write(cache_key, ret)
            inline = artist.inlineAlbums
            if inline:
                self.cache.write(fp_key, inline[0]["browseId"], ttl=_FINGERPRINT_TTL)

        if ret and len(ret) > 0:
            for album in ret:
                albums.append(YoutubeAlbum.from_dict(album))

        return albums

    def get_album_tracks(self, album: Album) -> list[Track]:
        cache_key = "album:" + album.id
        ret = self.cache.read(cache_key)
        if not ret:
            ret = self.client.get_album(album.id)
            self.cache.write(cache_key, ret)

        tracks: list[Track] = []
        if ret and "tracks" in ret:
            for track in ret["tracks"]:
                youtubeTrack = YoutubeTrack(
                    id=track["videoId"],
                    name=track["title"],
                    duration_ms=track["duration_seconds"] * 1000,
                    album=album,
                )
                for artist in track.get("artists", []):
                    if artist.get("id") is not None:
                        resolved_artist = self.get_artist(artist["id"])
                        if resolved_artist is not None:
                            youtubeTrack.artists.append(resolved_artist)
                tracks.append(youtubeTrack)

        return tracks

    def get_artist_tracks(self, artist: Artist) -> list[Track]:
        albums = self.get_artist_albums(artist)
        if not albums:
            return []

        tracks: list[Track] = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self.get_album_tracks, album): album for album in albums}
            for future in as_completed(futures):
                album = futures[future]
                try:
                    tracks += future.result()
                except Exception:
                    logger.exception(
                        f"{self.tag}  ! error fetching tracks for album"
                        f" '{album.name}' (artist: {artist.name}), skipping"
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
            for item in data.get("items", []):
                if item["snippet"]["title"] == playlist_name:
                    return item["id"]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        raise ValueError(f"Playlist not found: {playlist_name}")

    def sync(self, playlist_name: str, tracks: list[str] | None = None):
        playlist_id = self.get_playlist_id_for_name(playlist_name)

        # Fetch existing playlist items via Data API v3
        existing_item_ids: list[str] = []
        page_token = None
        while True:
            params: dict = {"part": "id", "playlistId": playlist_id, "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            data = self._data_api_get("https://www.googleapis.com/youtube/v3/playlistItems", params)
            existing_item_ids.extend(item["id"] for item in data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        logger.debug(f"{self.tag}  * removing {len(existing_item_ids)} existing items from playlist")
        for item_id in existing_item_ids:
            self._data_api_delete(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params={"id": item_id},
            )

        logger.debug(f"{self.tag}  * adding {len(tracks or [])} new items to playlist")
        for video_id in tracks or []:
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
