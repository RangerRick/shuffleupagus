import os
import time
from concurrent.futures import as_completed

import applemusicpy
import applescript

from ...core.apiresponse import api_int, api_list, api_str
from ...core.model import Album, Artist, Service, Track
from ...core.util import logger, parse_retry_after
from .model import AppleMusicAlbum, AppleMusicArtist, AppleMusicTrack, sanitize_id

_REQUEST_TIMEOUT = 30

# Named in every message raised from an unexpected API response.
_SERVICE_LABEL = "Apple Music"

# applemusicpy defaults to 10 retries with an escalating sleep (~65s per call) and
# ignores Retry-After entirely. Keep enough retries for a transient 5xx, few enough
# that a rate-limited run surfaces the 429 instead of stalling on every call.
_MAX_CLIENT_RETRIES = 2


def _applescript_str(value: str) -> str:
    """Escape a value for embedding in an AppleScript double-quoted string literal.

    Backslashes are doubled before quotes are escaped, otherwise the backslash
    introduced for the quote gets escaped in turn. AppleScript string literals
    cannot hold a raw line break and have no escape sequence for one, so a name
    carrying one is rejected instead of silently producing a broken script.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"Playlist name cannot contain a line break: {value!r}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


# errAEEventNotPermitted. Music.app is scriptable but this process has not been
# granted Automation access, which is the usual state on a machine that has never
# run this before.
_ERR_NOT_PERMITTED = -1743


def _run_applescript(script: applescript.AppleScript, doing: str, playlist_name: str) -> object:
    """Run a script, turning a ScriptError into an actionable RuntimeError.

    py-applescript raises rather than returning a sentinel, and an unhandled
    ScriptError reaches the user as a traceback naming neither the playlist nor
    anything they can do about it.
    """
    try:
        return script.run()
    except applescript.ScriptError as exc:
        detail = f"AppleScript failed {doing} playlist '{playlist_name}': {exc!s:.200}"
        if exc.number == _ERR_NOT_PERMITTED:
            detail += (
                ". Allow this program to control Music under System Settings > "
                "Privacy & Security > Automation, then retry."
            )
        raise RuntimeError(detail) from exc


def _applescript_count(result: object) -> int:
    """Coerce an AppleScript track count to int.

    AppleScript results are untyped at this boundary. A failing script raises
    applescript.ScriptError rather than returning a sentinel, so this guards the
    narrower case of a script that succeeds and answers with something that is
    not a number. Naming the type beats letting int() raise on its own, which
    says nothing about where the value came from.

    bool is excluded deliberately: it is a subclass of int, so True would
    otherwise pass as a count of 1.
    """
    if isinstance(result, bool) or not isinstance(result, int | float):
        # Truncated: the result is untrusted and unbounded, and this message
        # reaches the log.
        raise TypeError(f"AppleScript returned a non-numeric track count: {type(result).__name__} {result!r:.120}")
    return int(result)


class AppleMusicService(Service):
    name = "appleMusic"

    client: applemusicpy.AppleMusic

    def _require_config(self, key: str) -> str:
        val = self.config.get(key)
        if val is None:
            raise ValueError(f"Missing required config key 'services.appleMusic.{key}'")
        return val

    def login(self):
        keyfile_path = os.path.join(os.path.expanduser("~/.config/shuffleupagus"), self._require_config("secret-key"))
        with open(keyfile_path) as keyfile:
            key = keyfile.read().strip()

        self.client = applemusicpy.AppleMusic(
            secret_key=key,
            key_id=self._require_config("key-id"),
            team_id=self._require_config("team-id"),
            requests_timeout=_REQUEST_TIMEOUT,
            max_retries=_MAX_CLIENT_RETRIES,
        )
        self._check_rate_limit("Apple Music")

    def sanitize_id(self, id: str) -> str:
        return sanitize_id(id)

    def _reraise_if_fatal(self, e: Exception, what: str) -> None:
        """Abort the run on auth/rate-limit failures instead of masking them per-item.

        applemusicpy exhausts its own internal retries on 429/5xx before raising, so
        a requests.exceptions.HTTPError reaching here with status 401/403/429 means
        the credentials are bad or the API is actively rate-limiting us — every
        subsequent call will fail the same way, so continuing would just log the
        same error hundreds of times instead of stopping the run.
        """
        response = getattr(e, "response", None)
        status = getattr(response, "status_code", None)
        if status not in (401, 403, 429):
            return
        if status == 429:
            # Honour Retry-After, which applemusicpy's own retry loop ignores, and
            # persist the window so the next run fails fast instead of hammering.
            headers = getattr(response, "headers", None) or {}
            retry_after = parse_retry_after(headers.get("Retry-After"))
            if retry_after > 0:
                raise RuntimeError(self._record_rate_limit("Apple Music", retry_after)) from e
            raise RuntimeError("Apple Music rate-limited (no Retry-After header). Try again later.") from e
        raise RuntimeError(f"Apple Music API error ({status}) fetching {what}: {e}") from e

    # model: https://developer.apple.com/documentation/applemusicapi/artists
    def get_artist(self, artist) -> AppleMusicArtist | None:
        if isinstance(artist, str):
            artist_id = self.sanitize_id(artist)
            artist_obj = None
        else:
            artist_id = artist.id
            artist_obj = artist

        cache_key = "artist:" + artist_id

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                artist_obj = self.client.artist(artist_id)
                if artist_obj is not None:
                    ret = artist_obj
                    self.cache.write(cache_key, ret)
            except Exception as e:
                self._reraise_if_fatal(e, "artist")
                logger.error(f"{self.tag}  ! error fetching artist: {e}")

        if ret is not None and ret["data"] and len(ret["data"]) > 0:
            return AppleMusicArtist.from_dict(ret["data"][0])

        return None

    # model: https://developer.apple.com/documentation/applemusicapi/albums
    def get_album_by_id(self, album_id: str) -> AppleMusicAlbum | None:
        album_id = self.sanitize_id(album_id)
        cache_key = "album:" + album_id

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                album_obj = self.client.album(album_id)
                if album_obj is not None:
                    ret = album_obj
                    self.cache.write(cache_key, ret)
            except Exception as e:
                self._reraise_if_fatal(e, "album")
                logger.error(f"{self.tag}  ! error fetching album: {e}")

        if ret is not None and ret["data"] and len(ret["data"]) > 0:
            return AppleMusicAlbum.from_dict(ret["data"][0])

        return None

    # model: https://developer.apple.com/documentation/applemusicapi/albums
    def get_artist_albums(self, artist: Artist) -> list[Album]:
        cache_key = "artist:" + artist.id + ":albums"

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                albums = self.client.artist_relationship(artist.id, "albums")
                if albums is not None:
                    ret = albums
                    self.cache.write(cache_key, ret)
            except Exception as e:
                self._reraise_if_fatal(e, "artist albums")
                logger.warning(f"{self.tag}  ! error fetching albums for {artist.name} ({artist.id}): {e}")

        if ret is None or "data" not in ret or len(ret["data"]) == 0:
            return []

        albums = []
        for album in ret["data"] or []:
            albums.append(AppleMusicAlbum.from_dict(album))
        return albums

    def _get_track_by_id(self, track_id: str) -> Track | None:
        track_id = self.sanitize_id(track_id)
        cache_key = "track:" + track_id

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                track_obj = self.client.song(track_id)
                if track_obj is not None:
                    ret = track_obj
                    self.cache.write(cache_key, ret)
            except Exception as e:
                self._reraise_if_fatal(e, "track")
                logger.error(f"{self.tag}  ! error fetching track: {e}")

        if ret is not None and ret["data"] and len(ret["data"]) > 0:
            track_obj = ret["data"][0]
            artists = []
            album = None

            if "relationships" in track_obj:
                if "artists" in track_obj["relationships"]:
                    for artist in track_obj["relationships"]["artists"]["data"]:
                        resolved_artist = self.get_artist(artist["id"])
                        if resolved_artist is not None:
                            artists.append(resolved_artist)
                if "albums" in track_obj["relationships"]:
                    album = self.get_album_by_id(track_obj["relationships"]["albums"]["data"][0]["id"])

            return AppleMusicTrack.from_dict(track_obj, artists=artists, album=album)

        return None

    # model: https://developer.apple.com/documentation/applemusicapi/songs
    def get_album_tracks(self, album: Album, artist: Artist | None = None) -> list[Track]:
        cache_key = "album:" + album.id + ":tracks"

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                album_tracks = self.client.album_relationship(album.id, "tracks")
                if album_tracks is not None:
                    ret = album_tracks
                    self.cache.write(cache_key, ret)
            except Exception as e:
                self._reraise_if_fatal(e, "album tracks")
                logger.error(f"{self.tag}  ! error fetching album tracks: {e}")

        if ret is None or "data" not in ret or len(ret["data"]) == 0:
            return []

        artists = []
        if artist is not None:
            artists.append(artist)

        tracks = []
        for track in ret["data"] or []:
            appleMusicTrack = AppleMusicTrack.from_dict(track, album=album, artists=artists)
            tracks.append(appleMusicTrack)

        return tracks

    def get_artist_tracks(self, artist: Artist) -> list[Track]:
        albums = self.get_artist_albums(artist)
        if not albums:
            return []

        tracks: list[Track] = []
        futures = {self.album_pool.submit(self.get_album_tracks, album, artist): album for album in albums}
        for future in as_completed(futures):
            album = futures[future]
            try:
                tracks += future.result()
            except Exception:
                logger.exception(
                    f"{self.tag}  ! error fetching tracks for album '{album.name}' (artist: {artist.name}), skipping"
                )

        return tracks

    def get_artist_top_tracks(self, artist: Artist) -> list[Track]:
        cache_key = "top-tracks:" + artist.id

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                top_tracks = self.client.artist_relationship_view(artist.id, "top-songs")
                if top_tracks is not None:
                    ret = top_tracks
                self.cache.write(cache_key, ret)
            except Exception as e:
                self._reraise_if_fatal(e, "top tracks")
                logger.error(f"{self.tag}  ! error fetching top tracks: {e}")

        if ret is None or "data" not in ret or len(ret["data"]) == 0:
            return []

        tracks = []
        for track in ret["data"] or []:
            resolved = self._get_track_by_id(track["id"])
            if resolved is not None:
                tracks.append(resolved)
        return tracks

    def __get_media_headers(self) -> dict:
        headers = self.client._auth_headers()
        headers["Media-User-Token"] = self._require_config("media-user-token")
        return headers

    def __get_playlist_length(self, playlist_id: str) -> int:
        retries = 3
        while retries > 0:
            retries -= 1
            r = self.client._session.get(
                f"https://api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks",
                headers=self.__get_media_headers(),
                proxies=self.client.proxies,
                timeout=self.client.session_length,
            )
            if r.status_code >= 200 and r.status_code < 300:
                j = r.json()
                if "meta" in j:
                    return api_int(j, ("meta", "total"), _SERVICE_LABEL)
            else:
                # instead of returning 0 values, the API throws a 404 with a "No related resources" error
                if r.json()["errors"] and len(r.json()["errors"]) > 0:
                    if "code" in r.json()["errors"][0] and r.json()["errors"][0]["code"] == "40403":
                        return 0

                logger.error(f"{self.tag}  ! error fetching playlist: {r.status_code} {r.reason}")
                if len(r.text.strip()) > 0:
                    logger.error(f"{self.tag}{r.text}")
        raise Exception("Failed to fetch playlist length after 3 retries")

    def get_playlist_id_for_name(self, playlist_name: str) -> str:
        retries = 3
        while retries > 0:
            retries -= 1
            r = self.client._session.get(
                "https://api.music.apple.com/v1/me/library/playlists",
                headers=self.__get_media_headers(),
                proxies=self.client.proxies,
                timeout=self.client.session_length,
            )
            if r.status_code >= 200 and r.status_code < 300:
                for playlist in api_list(r.json(), ("data",), _SERVICE_LABEL):
                    attrs = playlist.get("attributes", {}) if isinstance(playlist, dict) else {}
                    if attrs.get("name") == playlist_name:
                        return api_str(playlist, ("id",), _SERVICE_LABEL)
            else:
                logger.error(f"{self.tag}  ! error fetching playlists: {r.status_code} {r.reason}")
                if len(r.text.strip()) > 0:
                    logger.error(f"{self.tag}{r.text}")

        raise Exception(f"Failed to fetch playlist ID for {playlist_name} after 3 retries")

    def __get_playlist_tracks(self, playlist_id: str) -> list[str]:
        """Read all catalog IDs from a library playlist via the cloud API."""
        catalog_ids: list[str] = []
        url = f"https://api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks?limit=100"
        while url:
            r = self.client._session.get(
                url,
                headers=self.__get_media_headers(),
                proxies=self.client.proxies,
                timeout=self.client.session_length,
            )
            if r.status_code == 404:
                body = r.json()
                errors = body.get("errors", [])
                if errors and errors[0].get("code") == "40403":
                    return []
                raise RuntimeError(f"Failed to read playlist tracks: {r.status_code}")
            if not (200 <= r.status_code < 300):
                raise RuntimeError(f"Failed to read playlist tracks: {r.status_code}")
            body = r.json()
            for item in body.get("data", []):
                cat_id = item.get("attributes", {}).get("playParams", {}).get("catalogId")
                if cat_id:
                    catalog_ids.append(str(cat_id))
            url = body.get("next")
            if url and not url.startswith("http"):
                url = f"https://api.music.apple.com{url}"
        return catalog_ids

    def __clear_playlist(self, playlist_name: str) -> None:
        """Delete all tracks from a playlist via AppleScript and wait."""
        name = _applescript_str(playlist_name)
        scpt = applescript.AppleScript(
            f"""
            tell application "Music" to run
            tell application "Music"
                set thePlaylist to (get playlist "{name}")
                delete every track of thePlaylist
            end tell
        """
        )
        _run_applescript(scpt, "clearing", playlist_name)

        logger.info(f"{self.tag}  * waiting for Music.app to process the deletion")
        count_scpt = applescript.AppleScript(
            f"""
            tell application "Music"
                set thePlaylist to (get playlist "{name}")
                return count of tracks of thePlaylist
            end tell
        """
        )
        count = _applescript_count(_run_applescript(count_scpt, "counting tracks in", playlist_name))
        for _ in range(150):
            if count == 0:
                break
            time.sleep(2)
            count = _applescript_count(_run_applescript(count_scpt, "counting tracks in", playlist_name))
            logger.info(f"{self.tag}    * {count} track(s) remaining...")
        else:
            raise RuntimeError(
                f"Music.app still reports {count} tracks in '{playlist_name}' after 5 minutes. "
                "Check that Music.app is responsive and is not mid-sync, then retry."
            )

    def __post_batch(self, playlist_id: str, track_ids: list[str]) -> None:
        """POST a single batch of tracks (≤80) with retries."""
        url = f"https://api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks"
        payload = {"data": [{"id": tid, "type": "songs"} for tid in track_ids]}
        retries = 3
        while retries > 0:
            retries -= 1
            r = self.client._session.post(
                url,
                headers=self.__get_media_headers(),
                proxies=self.client.proxies,
                timeout=self.client.session_length,
                json=payload,
            )
            if 200 <= r.status_code < 300:
                logger.debug(f"{self.tag}  * added batch of {len(track_ids)} tracks")
                return
            logger.warning(f"{self.tag}  ! request failed ({r.status_code} {r.reason})")
            if r.text.strip():
                logger.warning(f"{self.tag}  ! {r.text}")
            if retries == 0:
                raise RuntimeError(f"Failed to add tracks after 3 retries ({r.status_code} {r.reason})")

    _MAX_REQUEUE_ROUNDS = 5

    def __add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        """Add tracks in batches of 80, verifying after each batch.

        Uses the cloud API count as a fast check after each batch. When
        the count is short, reads the full track list to identify which
        tracks are missing and re-queues them, up to _MAX_REQUEUE_ROUNDS
        times before giving up on the still-missing tracks.
        """
        remaining = list(track_ids)
        expected_count = 0
        batch_num = 0
        requeue_rounds = 0

        while remaining:
            batch = remaining[:80]
            remaining = remaining[80:]
            batch_num += 1
            self.__post_batch(playlist_id, batch)
            expected_count += len(batch)

            verified = False
            for attempt in range(3):
                time.sleep(5)
                cloud_count = self.__get_playlist_length(playlist_id)
                if cloud_count == expected_count:
                    logger.info(f"{self.tag}  * verified batch {batch_num}: {cloud_count} tracks in cloud")
                    verified = True
                    break

                logger.warning(
                    f"{self.tag}  ! batch {batch_num}"
                    f" verify attempt {attempt + 1}:"
                    f" cloud has {cloud_count},"
                    f" expected {expected_count}"
                )
                expected_so_far = set(track_ids[:expected_count])
                actual = set(self.__get_playlist_tracks(playlist_id))
                missing = expected_so_far - actual
                if missing:
                    self.__post_batch(playlist_id, list(missing))

            if not verified:
                expected_so_far = set(track_ids[:expected_count])
                actual = set(self.__get_playlist_tracks(playlist_id))
                still_missing = expected_so_far - actual
                if still_missing:
                    requeue_rounds += 1
                    if requeue_rounds > self._MAX_REQUEUE_ROUNDS:
                        raise RuntimeError(
                            f"{len(still_missing)} tracks could not be verified in the cloud "
                            f"playlist after {self._MAX_REQUEUE_ROUNDS} re-queue rounds: "
                            f"{sorted(still_missing)}"
                        )
                    logger.warning(
                        f"{self.tag}  ! batch {batch_num}: {len(still_missing)} tracks still missing, "
                        f"re-queuing (round {requeue_rounds}/{self._MAX_REQUEUE_ROUNDS})"
                    )
                    remaining = list(still_missing) + remaining
                    expected_count -= len(still_missing)

    def sync(self, playlist_name: str, tracks: list[str] | None = None):
        if not tracks:
            logger.warning(f"{self.tag}  ! sync called with no tracks, skipping")
            return

        logger.info(f"{self.tag}  * determining playlist id for {playlist_name}")
        playlist_id = self.get_playlist_id_for_name(playlist_name)

        # Check if playlist already matches desired state
        current = self.__get_playlist_tracks(playlist_id)
        if current == tracks:
            logger.info(f"{self.tag}  * playlist already matches ({len(tracks)} tracks), skipping sync")
            return

        # Clear if non-empty
        if current:
            logger.info(f"{self.tag}  * clearing existing tracks in playlist '{playlist_name}'")
            self.__clear_playlist(playlist_name)

            # Wait for cloud API to reflect the deletion — this can
            # take minutes on slow connections.  Poll every 10s for up
            # to 5 minutes.
            logger.info(f"{self.tag}  * waiting for cloud to process deletion")
            for i in range(30):
                time.sleep(10)
                cloud_count = self.__get_playlist_length(playlist_id)
                if cloud_count == 0:
                    logger.info(f"{self.tag}  * cloud confirmed empty after {(i + 1) * 10}s")
                    break
                logger.info(f"{self.tag}    * cloud still has {cloud_count} tracks ({(i + 1) * 10}s elapsed)")
            else:
                # range(30) is never empty, so the loop body always binds
                # cloud_count before this else branch runs. Pyright cannot prove
                # a range is non-empty.
                raise RuntimeError(
                    f"Cloud still reports {cloud_count} tracks after 5 minutes — aborting to avoid duplicates"  # pyright: ignore[reportPossiblyUnboundVariable]
                )

        logger.info(f"{self.tag}  * publishing {len(tracks)} songs to the playlist")
        self.__add_tracks(playlist_id, tracks)
