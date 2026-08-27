import copy
import sys
import threading
from concurrent.futures import as_completed
from typing import cast

import requests.adapters
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from urllib3.util.retry import Retry

from ...core.apiresponse import api_array, api_has, api_int, api_list, api_object, api_str
from ...core.model import Album, Artist, Service, Track
from ...core.util import logger, parse_retry_after
from .model import SpotifyAlbum, SpotifyArtist, SpotifyTrack, sanitize_id

# Fingerprint TTL: how long before we re-check whether a new album has been released.
# Short enough to catch new releases, long enough to avoid hammering the API every run.
_FINGERPRINT_TTL = 60 * 60 * 24  # 24 hours

# Named in every message raised from an unexpected API response.
_SERVICE_LABEL = "Spotify"

_REQUEST_TIMEOUT = 30


_NO_RETRY_AFTER_MESSAGE = "Spotify rate-limited (no Retry-After header). Try again later."


def _retry_after_seconds(exc: Exception) -> int:
    """Read the Retry-After header off a rate-limit exception, 0 when absent."""
    headers = getattr(exc, "headers", None) or {}
    return parse_retry_after(headers.get("Retry-After"))


class SpotifyService(Service):
    name = "spotify"

    spotify: spotipy.Spotify
    _api_lock: threading.Lock

    _MAX_VERIFY_ROUNDS = 3

    def _require_config(self, key: str) -> str:
        val = self.config.get(key)
        if val is None:
            raise ValueError(f"Missing required config key 'services.spotify.{key}'")
        return val

    def login(self):
        self._api_lock = threading.Lock()
        self._rate_limited: str | None = None
        creds = SpotifyOAuth(
            client_id=self._require_config("client-id"),
            client_secret=self._require_config("client-secret"),
            scope=self._require_config("scope"),
            redirect_uri="http://127.0.0.1:9090/",
            requests_timeout=_REQUEST_TIMEOUT,
        )
        self.spotify = spotipy.Spotify(
            auth_manager=creds,
            requests_timeout=_REQUEST_TIMEOUT,
            retries=0,
            status_retries=0,
        )
        # Disable urllib3's special 429 handling so the response (with
        # Retry-After header) flows through as a normal HTTPError instead
        # of being swallowed into a headerless MaxRetryError.
        retry = Retry(total=0, respect_retry_after_header=False)
        adapter = requests.adapters.HTTPAdapter(max_retries=retry)
        # spotipy declares _session as Session | requests.api, but only ever assigns a Session.
        session = cast("requests.Session", self.spotify._session)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self._acquire_token(creds)
        self._check_rate_limit("Spotify")

    def _acquire_token(self, creds: SpotifyOAuth):
        """Force token acquisition on the main thread.

        Interactive auth (browser) works here; in worker threads it would
        block forever.  When there is no TTY, fail fast instead of hanging.
        """
        token_info = creds.validate_token(
            creds.cache_handler.get_cached_token(),
        )
        if token_info is not None and not creds.is_token_expired(token_info):
            return

        if token_info is not None:
            try:
                creds.refresh_access_token(token_info["refresh_token"])
            except Exception as e:
                if not sys.stdin.isatty():
                    raise RuntimeError(
                        "Spotify token expired and refresh failed. Run interactively to re-authenticate."
                    ) from e
                logger.warning(f"{self.tag}token refresh failed ({e}), re-authenticating")
            else:
                return

        if not sys.stdin.isatty():
            raise RuntimeError("No valid Spotify token. Run interactively to authenticate.")

        creds.get_access_token(as_dict=False)

    def _call(self, method, *args, **kwargs):
        """Serialize Spotify API access and detect rate limiting."""
        if self._rate_limited:
            raise RuntimeError(self._rate_limited)
        with self._api_lock:
            if self._rate_limited:
                raise RuntimeError(self._rate_limited)
            try:
                return method(*args, **kwargs)
            # Deliberately broad. A 429 reaches here as a spotipy.SpotifyException
            # or as a requests error carrying the status in its message, and this
            # clause only converts that one case — everything else is re-raised
            # unchanged by the bare `raise` below, so nothing is swallowed.
            except Exception as e:
                status = getattr(e, "http_status", None)
                msg = str(e)
                if status == 429 or "429" in msg:
                    retry_after = _retry_after_seconds(e)
                    if retry_after > 0:
                        rate_msg = self._record_rate_limit("Spotify", retry_after)
                    else:
                        rate_msg = _NO_RETRY_AFTER_MESSAGE
                    self._rate_limited = rate_msg
                    raise RuntimeError(rate_msg) from e
                raise

    def sanitize_id(self, id: str) -> str:
        return sanitize_id(id)

    def get_artist(self, artist: str | Artist) -> SpotifyArtist:
        if isinstance(artist, str):
            artist_id = self.sanitize_id(artist)
            artist_obj = None
        else:
            artist_id = artist.id
            artist_obj = artist

        if artist_id is None:
            raise ValueError("Artist ID is missing")

        cache_key = "artist:" + artist_id
        ret = self.cache.read(cache_key)
        if not ret:
            if artist_obj:
                ret = artist_obj
            else:
                ret = self._call(self.spotify.artist, artist_id)
            self.cache.write(cache_key, ret)

        return SpotifyArtist.from_dict(ret)

    def get_album_by_id(self, album_id: str) -> Album:
        album_id = self.sanitize_id(album_id)

        cache_key = "album:" + album_id
        ret = self.cache.read(cache_key)
        if not ret:
            ret = self._call(self.spotify.album, album_id)
            self.cache.write(cache_key, ret)

        return SpotifyAlbum.from_dict(ret)

    def get_artist_albums(self, artist: Artist) -> list[Album]:
        cache_key = "artist:" + artist.id + ":albums"
        fp_key = "fingerprint:artist:" + artist.id

        ret = self.cache.read(cache_key)
        if ret is None:
            # Cache miss — check fingerprint before doing a full catalog fetch.
            stale = self.cache.read_stale(cache_key)
            if stale is not None:
                try:
                    latest = self._call(self.spotify.artist_albums, artist.id, limit=1)
                    items = api_list(latest, ("items",), _SERVICE_LABEL) if api_has(latest, "items") else []
                    if items:
                        latest_id = api_str(api_object(items[0], "items[0]", _SERVICE_LABEL), ("id",), _SERVICE_LABEL)
                        cached_fp = self.cache.read_stale(fp_key)
                        if cached_fp == latest_id:
                            logger.debug(f"{self.tag}* fingerprint match for {artist.name}, extending cache")
                            self.cache.touch(cache_key)
                            self.cache.write(fp_key, latest_id, ttl=_FINGERPRINT_TTL)
                            ret = stale
                except RuntimeError:
                    # _call() raises RuntimeError for rate limiting. Let it
                    # propagate so collect_tracks() aborts the run, instead of
                    # burying a 429 in a debug-level "fingerprint check failed".
                    raise
                except Exception as e:
                    logger.debug(f"{self.tag}* fingerprint check failed for {artist.id}: {e}")

        if ret is None:
            album = self._call(self.spotify.artist_albums, artist.id)
            # "items" is not optional: /artists/{id}/albums answers a paging
            # object, which always carries it. Treating an absent one as "no
            # albums" cached [] for a week and dropped the artist from the
            # playlist silently on every run after that.
            ret = api_list(album, ("items",), _SERVICE_LABEL)
            self.cache.write(cache_key, ret)
            # Spotify returns albums newest-first; ret[0] is the latest release.
            if ret:
                first = api_object(ret[0], "items[0]", _SERVICE_LABEL)
                self.cache.write(fp_key, api_str(first, ("id",), _SERVICE_LABEL), ttl=_FINGERPRINT_TTL)

        albums = []
        if ret:
            # Checked after the branches merge, not inside the fetch branch: ret
            # can also come from the cache, which holds the same untrusted JSON.
            for album in api_array(ret, "artist albums", _SERVICE_LABEL):
                albums.append(SpotifyAlbum.from_dict(api_object(album, "items[] entry", _SERVICE_LABEL)))

        return albums

    def get_album_tracks(self, album: Album) -> list[Track]:
        cache_key = "album:" + album.id + ":tracks"

        ret = self.cache.read(cache_key)
        if not ret:
            t = self._call(self.spotify.album_tracks, album.id)
            # Also a paging object; see get_artist_albums.
            ret = api_list(t, ("items",), _SERVICE_LABEL)
            self.cache.write(cache_key, ret)

        tracks: list[Track] = []
        if ret:
            # See get_artist_albums: ret can be a cache hit, so the container is
            # checked here rather than only on the fetch branch.
            for track in api_array(ret, "album tracks", _SERVICE_LABEL):
                track = api_object(track, "items[] entry", _SERVICE_LABEL)
                isrc = None
                # "external_ids" is optional, but a present one that is not an
                # object is malformed rather than absent. api_has answers False
                # for both, which would mask the second silently.
                if "external_ids" in track:
                    external_ids = api_object(track["external_ids"], "external_ids", _SERVICE_LABEL)
                    if "isrc" in external_ids:
                        isrc = api_str(external_ids, ("isrc",), _SERVICE_LABEL)

                spotifyTrack = SpotifyTrack(
                    id=api_str(track, ("id",), _SERVICE_LABEL),
                    name=api_str(track, ("name",), _SERVICE_LABEL),
                    duration_ms=api_int(track, ("duration_ms",), _SERVICE_LABEL),
                    isrc=isrc,
                    album=album,
                )
                for artist in api_list(track, ("artists",), _SERVICE_LABEL):
                    entry = api_object(artist, "artists[] entry", _SERVICE_LABEL)
                    spotifyTrack.artists.append(self.get_artist(api_str(entry, ("id",), _SERVICE_LABEL)))
                tracks.append(spotifyTrack)

        return tracks

    def get_artist_tracks(self, artist: Artist) -> list[Track]:
        albums = self.get_artist_albums(artist)
        if not albums:
            return []

        tracks: list[Track] = []
        futures = {self.album_pool.submit(self.get_album_tracks, album): album for album in albums}
        fatal_error = None
        for future in as_completed(futures):
            album = futures[future]
            try:
                tracks += future.result()
            except RuntimeError as exc:
                # RuntimeError is the convention's "abort this service" signal —
                # a rate-limit window, an unusable cache, a fetch that failed
                # rather than found nothing. Logging it here and carrying on
                # turns it straight back into a silently missing album, which
                # is the thing raising it was meant to stop.
                fatal_error = exc
                break
            except Exception:
                logger.exception(
                    f"{self.tag}  ! error fetching tracks for album '{album.name}' (artist: {artist.name}), skipping"
                )

        if fatal_error is not None:
            # Drop whatever has not started yet. We are aborting because the
            # service told us to stop — often a rate limit — so letting queued
            # work carry on calling the same API is both pointless and rude.
            # Already-running tasks cannot be interrupted; this is the same
            # guarantee Service._shutdown_pools gives.
            for pending in futures:
                pending.cancel()
            raise fatal_error

        return tracks

    def get_artist_top_tracks(self, artist: Artist) -> list[Track]:
        cache_key = "top-tracks:" + artist.id

        ret = self.cache.read(cache_key)
        if not ret:
            ret = self._call(self.spotify.artist_top_tracks, artist.id)
            self.cache.write(cache_key, ret)

        tracks = []
        # artist_top_tracks always carries "tracks"; see get_artist_albums.
        if ret is not None:
            for track in api_list(ret, ("tracks",), _SERVICE_LABEL):
                track = api_object(track, "tracks[] entry", _SERVICE_LABEL)
                album = self.get_album_by_id(api_str(track, ("album", "id"), _SERVICE_LABEL))

                isrc = None
                # See get_album_tracks: present-but-malformed is not absent.
                if "external_ids" in track:
                    external_ids = api_object(track["external_ids"], "external_ids", _SERVICE_LABEL)
                    if "isrc" in external_ids:
                        isrc = api_str(external_ids, ("isrc",), _SERVICE_LABEL)

                artists = []
                for a in api_list(track, ("artists",), _SERVICE_LABEL):
                    entry = api_object(a, "tracks[].artists[] entry", _SERVICE_LABEL)
                    artists.append(self.get_artist(api_str(entry, ("id",), _SERVICE_LABEL)))

                spotifyTrack = SpotifyTrack(
                    id=api_str(track, ("id",), _SERVICE_LABEL),
                    name=api_str(track, ("name",), _SERVICE_LABEL),
                    duration_ms=api_int(track, ("duration_ms",), _SERVICE_LABEL),
                    isrc=isrc,
                    album=album,
                    artists=artists,
                )
                tracks.append(spotifyTrack)

        return tracks

    def get_playlist_id_for_name(self, playlist_name: str) -> str:
        offset = 0
        while True:
            results = self._call(self.spotify.current_user_playlists, limit=50, offset=offset)
            items = api_list(results, ("items",), _SERVICE_LABEL)
            for item in items:
                entry = api_object(item, "items[] entry", _SERVICE_LABEL)
                if entry.get("name") == playlist_name:
                    return api_str(entry, ("id",), _SERVICE_LABEL)
            # A page read as empty would end the loop and raise "Playlist not
            # found" for a playlist that exists, so "items" is required here.
            if len(items) < 50:
                break
            offset += 50
        raise ValueError(f"Playlist not found: {playlist_name}")

    def __get_playlist_track_ids(self, playlist_id: str) -> list[str]:
        """Read every track ID currently in the playlist."""
        ids: list[str] = []
        offset = 0
        while True:
            results = self._call(
                self.spotify.playlist_items,
                playlist_id,
                fields="items(track(id))",
                limit=100,
                offset=offset,
            )
            items = api_list(results, ("items",), _SERVICE_LABEL)
            for item in items:
                entry = api_object(item, "items[] entry", _SERVICE_LABEL)
                # A null "track" is normal here: Spotify sends one for a track
                # that has been removed from the catalogue but is still listed.
                # A "track" that is present and is not an object is not normal,
                # so it is reported rather than skipped along with the nulls.
                raw_track = entry.get("track")
                if raw_track is None:
                    continue
                track = api_object(raw_track, "items[].track", _SERVICE_LABEL)
                # A null ID means a local file, which has no catalogue ID to match.
                if track.get("id") is not None:
                    ids.append(api_str(track, ("id",), _SERVICE_LABEL))
            if len(items) < 100:
                break
            offset += 100
        return ids

    def __verify_playlist(self, playlist_id: str, expected_ids: list[str]) -> None:
        """Confirm every track reached the playlist, re-adding any that did not.

        Spotify's API is read-after-write consistent, so no settle delay is
        needed here — a track absent from the read-back was genuinely dropped.
        """
        expected = set(expected_ids)
        for round_num in range(self._MAX_VERIFY_ROUNDS + 1):
            missing = expected - set(self.__get_playlist_track_ids(playlist_id))
            if not missing:
                logger.info(f"{self.tag}  * verified {len(expected)} tracks in playlist")
                return
            if round_num == self._MAX_VERIFY_ROUNDS:
                raise RuntimeError(
                    f"{len(missing)} tracks could not be verified in the Spotify playlist "
                    f"after {self._MAX_VERIFY_ROUNDS} re-add rounds: {sorted(missing)}"
                )
            logger.warning(
                f"{self.tag}  ! {len(missing)} tracks missing after sync, "
                f"re-adding (round {round_num + 1}/{self._MAX_VERIFY_ROUNDS})"
            )
            retry = list(missing)
            while retry:
                self._call(self.spotify.playlist_add_items, playlist_id, retry[0:80])
                del retry[0:80]

    def sync(self, playlist_name: str, tracks: list[str] | None = None):
        playlist_id = self.get_playlist_id_for_name(playlist_name)

        expected_ids = list(tracks or [])
        playlist_tracks = copy.deepcopy(expected_ids)
        self._call(self.spotify.playlist_replace_items, playlist_id, playlist_tracks[0:80])
        del playlist_tracks[0:80]
        while len(playlist_tracks) > 0:
            self._call(self.spotify.playlist_add_items, playlist_id, playlist_tracks[0:80])
            del playlist_tracks[0:80]

        if expected_ids:
            self.__verify_playlist(playlist_id, expected_ids)
