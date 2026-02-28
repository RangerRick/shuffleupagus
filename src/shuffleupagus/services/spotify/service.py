import copy
from concurrent.futures import ThreadPoolExecutor, as_completed

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from ...core.model import Album, Artist, Service, Track
from ...core.util import logger
from .model import SpotifyAlbum, SpotifyArtist, SpotifyTrack, sanitize_id

# Fingerprint TTL: how long before we re-check whether a new album has been released.
# Short enough to catch new releases, long enough to avoid hammering the API every run.
_FINGERPRINT_TTL = 60 * 60 * 24  # 24 hours


class SpotifyService(Service):
    name = "spotify"

    spotify: spotipy.Spotify

    def login(self):
        creds = SpotifyOAuth(
            client_id=self.config["client-id"],
            client_secret=self.config["client-secret"],
            scope=self.config["scope"],
            redirect_uri="http://localhost:9090/",
        )
        self.spotify = spotipy.Spotify(auth_manager=creds)

    def close(self):
        self.cache.save()

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
                ret = self.spotify.artist(artist_id)
            self.cache.write(cache_key, ret)

        return SpotifyArtist.from_dict(ret)

    def get_album_by_id(self, album_id: str) -> Album:
        album_id = self.sanitize_id(album_id)

        cache_key = "album:" + album_id
        ret = self.cache.read(cache_key)
        if not ret:
            ret = self.spotify.album(album_id)
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
                    latest = self.spotify.artist_albums(artist.id, limit=1)
                    if latest and "items" in latest and latest["items"]:
                        latest_id = latest["items"][0]["id"]
                        cached_fp = self.cache.read_stale(fp_key)
                        if cached_fp == latest_id:
                            logger.debug(f"* fingerprint match for {artist.name}, extending cache")
                            self.cache.touch(cache_key)
                            self.cache.write(fp_key, latest_id, ttl=_FINGERPRINT_TTL)
                            ret = stale
                except Exception as e:
                    logger.debug(f"* fingerprint check failed for {artist.id}: {e}")

        if ret is None:
            album = self.spotify.artist_albums(artist.id)
            if album is not None and "items" in album:
                ret = album["items"]
            self.cache.write(cache_key, ret if ret is not None else [])
            if ret:
                self.cache.write(fp_key, ret[0]["id"], ttl=_FINGERPRINT_TTL)

        albums = []
        if ret:
            for album in ret:
                albums.append(SpotifyAlbum.from_dict(album))

        return albums

    def get_album_tracks(self, album: Album) -> list[Track]:
        cache_key = "album:" + album.id + ":tracks"

        ret = self.cache.read(cache_key)
        if not ret:
            t = self.spotify.album_tracks(album.id)
            if t is not None and "items" in t:
                ret = t["items"]
            self.cache.write(cache_key, ret)

        tracks: list[Track] = []
        if ret:
            for track in ret:
                isrc = None
                if "external_ids" in track and "isrc" in track["external_ids"]:
                    isrc = str(track["external_ids"]["isrc"])

                spotifyTrack = SpotifyTrack(
                    id=track["id"],
                    name=track["name"],
                    duration_ms=track["duration_ms"],
                    isrc=isrc,
                    album=album,
                )
                for artist in track["artists"]:
                    spotifyTrack.artists.append(self.get_artist(artist["id"]))
                tracks.append(spotifyTrack)

        return tracks

    def get_artist_tracks(self, artist: Artist) -> list[Track]:
        albums = self.get_artist_albums(artist)
        if not albums:
            return []

        tracks: list[Track] = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self.get_album_tracks, album): album for album in albums}
            for future in as_completed(futures):
                tracks += future.result()

        return tracks

    def get_artist_top_tracks(self, artist: Artist) -> list[Track]:
        cache_key = "top-tracks:" + artist.id

        ret = self.cache.read(cache_key)
        if not ret:
            ret = self.spotify.artist_top_tracks(artist.id)
            self.cache.write(cache_key, ret)

        tracks = []
        if ret is not None and "tracks" in ret:
            for track in ret["tracks"]:
                album = self.get_album_by_id(track["album"]["id"])

                isrc = None
                if "external_ids" in track and "isrc" in track["external_ids"]:
                    isrc = track["external_ids"]["isrc"]

                artists = []
                for a in track["artists"]:
                    a = self.get_artist(a["id"])
                    artists.append(a)

                spotifyTrack = SpotifyTrack(
                    id=track["id"],
                    name=track["name"],
                    duration_ms=track["duration_ms"],
                    isrc=isrc,
                    album=album,
                    artists=artists,
                )
                tracks.append(spotifyTrack)

        return tracks

    def get_playlist_id_for_name(self, playlist_name: str) -> str:
        offset = 0
        while True:
            results = self.spotify.current_user_playlists(limit=50, offset=offset)
            if results and "items" in results:
                items = results["items"]
                for item in items:
                    if item["name"] == playlist_name:
                        return item["id"]
            if not results or len(results["items"]) < 50:
                break
            offset += 50
        raise ValueError(f"Playlist not found: {playlist_name}")

    def sync(self, playlist_name: str, tracks: list[str] | None = None):
        playlist_id = self.get_playlist_id_for_name(playlist_name)

        playlist_tracks = copy.deepcopy(tracks or [])
        self.spotify.playlist_replace_items(playlist_id, playlist_tracks[0:80])
        del playlist_tracks[0:80]
        while len(playlist_tracks) > 0:
            self.spotify.playlist_add_items(playlist_id, playlist_tracks[0:80])
            del playlist_tracks[0:80]
