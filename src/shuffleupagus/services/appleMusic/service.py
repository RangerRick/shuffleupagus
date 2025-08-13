import copy
import os
import time
import applemusicpy
import applescript

from ...core.model import Album, Artist, Service, Track
from ...core.util import logger
from .model import AppleMusicArtist, AppleMusicAlbum, AppleMusicTrack, sanitize_id

class AppleMusicService(Service):
    name = "appleMusic"

    client:applemusicpy.AppleMusic

    def login(self):
        keyfile_path = os.path.join(os.path.expanduser("~/.config/shuffleupagus"), self.config['secret-key'])
        with open(keyfile_path, 'r') as keyfile:
            key = keyfile.read().strip()

        self.client = applemusicpy.AppleMusic(
            secret_key=key,
            key_id=self.config['key-id'],
            team_id=self.config['team-id'],
        )
        return self.client

    def close(self):
        self.cache.save()

    def sanitize_id(self, id: str) -> str:
        return sanitize_id(id)

    # model: https://developer.apple.com/documentation/applemusicapi/artists
    def get_artist(self, artist) -> AppleMusicArtist|None:
        if isinstance(artist, str):
            artist_id = artist
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
                logger.error(f"  ! error fetching artist: {e}")

        if ret is not None and ret['data'] and len(ret['data']) > 0:
            return AppleMusicArtist.from_dict(ret['data'][0])

        return None

    # model: https://developer.apple.com/documentation/applemusicapi/albums
    def get_album_by_id(self, album_id:str) -> AppleMusicAlbum|None:
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
                logger.error(f"  ! error fetching album: {e}")

        if ret is not None and ret['data'] and len(ret['data']) > 0:
            return AppleMusicAlbum.from_dict(ret['data'][0])

        return None

    # model: https://developer.apple.com/documentation/applemusicapi/albums
    def get_artist_albums(self, artist:Artist) -> list[Album]:
        cache_key = 'artist:' + artist.id + ':albums'

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                albums = self.client.artist_relationship(artist.id, 'albums')
                if albums is not None:
                    ret = albums
                    self.cache.write(cache_key, ret)
            except Exception as e:
                logger.error(f"  ! error fetching artist albums: {e}")

        if ret is None or 'data' not in ret or len(ret['data']) == 0:
            return []

        albums = []
        for album in ret['data'] or []:
            albums.append(AppleMusicAlbum.from_dict(album))
        return albums

    def _get_track_by_id(self, track_id:str) -> Track|None:
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
                logger.error(f"  ! error fetching track: {e}")

        if ret is not None and ret['data'] and len(ret['data']) > 0:
            track_obj = ret['data'][0]
            artists = []
            album = None

            if 'relationships' in track_obj:
                if 'artists' in track_obj['relationships']:
                    for artist in track_obj['relationships']['artists']['data']:
                        artist = self.get_artist(artist['id'])
                        artists.append(artist)
                if 'albums' in track_obj['relationships']:
                    album = self.get_album_by_id(track_obj['relationships']['albums']['data'][0]['id'])

            return AppleMusicTrack.from_dict(track_obj, artists=artists, album=album)

        return None

    # model: https://developer.apple.com/documentation/applemusicapi/songs
    def get_album_tracks(self, album:Album, artist:Artist|None=None) -> list[Track]:
        cache_key = 'album:' + album.id + ':tracks'

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                album_tracks = self.client.album_relationship(album.id, 'tracks')
                if album_tracks is not None:
                    ret = album_tracks
                    self.cache.write(cache_key, ret)
            except Exception as e:
                logger.error(f"  ! error fetching album tracks: {e}")

        if ret is None or 'data' not in ret or len(ret['data']) == 0:
            return []

        artists = []
        if artist is not None:
            artists.append(artist)

        tracks = []
        for track in ret['data'] or []:
            appleMusicTrack = AppleMusicTrack.from_dict(track, album=album, artists=artists)
            tracks.append(appleMusicTrack)

        return tracks

    def get_artist_tracks(self, artist:Artist) -> list[Track]:
        tracks = []
        albums = self.get_artist_albums(artist)
        for album in albums:
            tracks += self.get_album_tracks(album, artist)
        return tracks

    def get_artist_top_tracks(self, artist:Artist) -> list[Track]:
        cache_key = "top-tracks:" + artist.id

        ret = self.cache.read(cache_key)
        if not ret:
            try:
                top_tracks = self.client.artist_relationship_view(artist.id, 'top-songs')
                if top_tracks is not None:
                    ret = top_tracks
                self.cache.write(cache_key, ret)
            except Exception as e:
                logger.error(f"  ! error fetching top tracks: {e}")

        if ret is None or 'data' not in ret or len(ret['data']) == 0:
            return []

        tracks = []
        for track in ret['data'] or []:
            track = self._get_track_by_id(track['id'])
            tracks.append(track)
        return tracks

    def __get_media_headers(self) -> dict:
        headers = self.client._auth_headers()
        headers['Media-User-Token'] = self.config['media-user-token']
        return headers

    def __get_playlist_length(self, playlist_id:str) -> int:
        retries = 3
        while retries > 0:
            retries -= 1
            r = self.client._session.get(
                f'https://api.music.apple.com/v1/me/library/playlists/{playlist_id}/tracks',
                headers=self.__get_media_headers(),
                proxies=self.client.proxies,
                timeout=self.client.session_length,
            )
            if r.status_code >= 200 and r.status_code < 300:
                j = r.json()
                if 'meta' in j:
                    return int(j['meta']['total'])
            else:
                # instead of returning 0 values, the API throws a 404 with a "No related resources" error
                if r.json()['errors'] and len(r.json()['errors']) > 0:
                    if 'code' in r.json()['errors'][0] and r.json()['errors'][0]['code'] == '40403':
                        return 0

                logger.error(f"  ! error fetching playlist: {r.status_code} {r.reason}")
                if len(r.text.strip()) > 0:
                    logger.error(r.text)
        raise Exception("Failed to fetch playlist length after 3 retries")


    def get_playlist_id_for_name(self, playlist_name:str) -> str:
        retries = 3
        while retries > 0:
            retries -= 1
            r = self.client._session.get(
                'https://api.music.apple.com/v1/me/library/playlists',
                headers=self.__get_media_headers(),
                proxies=self.client.proxies,
                timeout=self.client.session_length,
            )
            if r.status_code >= 200 and r.status_code < 300:
                if r.json()['data'] and len(r.json()['data']) > 0:
                    for playlist in r.json()['data']:
                        if 'attributes' in playlist and 'name' in playlist['attributes'] and playlist['attributes']['name'] == playlist_name:
                            return playlist['id']
            else:
                logger.error(f"  ! error fetching playlists: {r.status_code} {r.reason}")
                if len(r.text.strip()) > 0:
                    logger.error(r.text)

        raise Exception(f"Failed to fetch playlist ID for {playlist_name} after 3 retries")

    def sync(self, playlist_name:str, tracks:list[str]=[]):
        apple_tracks = list(map(lambda track : {'id': track, 'type': 'songs' }, tracks))

        logger.info(f"  * determining playlist id for {playlist_name}")
        playlist_id = self.get_playlist_id_for_name(playlist_name)
        url = f'https://api.music.apple.com/v1/me/library/playlists/{playlist_id}'

        logger.info(f"  * clearing existing tracks in playlist '{playlist_name}'")

        # Clear existing tracks in the playlist
        scpt = applescript.AppleScript('''
            tell application "Music" to run
            tell application "Music"
                set thePlaylist to (get playlist "''' + playlist_name + '''")
                delete every track of thePlaylist
            end tell
        ''')
        scpt.run()

        logger.info("  * waiting for Music.app to process the deletion")
        count = self.__get_playlist_length(playlist_id)
        while count > 0:
            time.sleep(2)
            count = self.__get_playlist_length(playlist_id)
            logger.info(f"    * {count} track(s) remaining...")

        playlist_tracks = copy.deepcopy(apple_tracks)

        logger.info(f"  * publishing {len(playlist_tracks)} songs to the playlist")
        while len(playlist_tracks) > 0:
            # Apple Music API allows a maximum of 100 tracks per request, do 80 just in case
            batch = playlist_tracks[0:80]
            del playlist_tracks[0:80]

            payload = {'data': batch}
            retries = 3
            while retries > 0:
                retries -= 1

                r = self.client._session.post(
                    url + "/tracks",
                    headers=self.__get_media_headers(),
                    proxies=self.client.proxies,
                    timeout=self.client.session_length,
                    json=payload,
                )
                if r.status_code >= 200 and r.status_code < 300:
                    break

                logger.warning(f"{r.status_code} {r.reason}")
                if len(r.text.strip()) > 0:
                    logger.warning(r.text)
                if retries == 0:
                    raise Exception("Failed to add tracks to playlist after 3 retries")
