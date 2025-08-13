import copy
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from ...core.model import Album, Artist, Service, Track
from .model import SpotifyArtist, SpotifyAlbum, SpotifyTrack, sanitize_id

class SpotifyService(Service):
    name = "spotify"

    spotify:spotipy.Spotify

    def login(self):
        creds = SpotifyOAuth(
            client_id=self.config['client-id'],
            client_secret=self.config['client-secret'],
            scope=self.config['scope'],
            redirect_uri="http://localhost:9090/"
        )
        self.spotify = spotipy.Spotify(auth_manager=creds)

    def close(self):
        self.cache.save()

    def sanitize_id(self, id: str) -> str:
        return sanitize_id(id)

    def get_artist(self, artist:str|Artist) -> SpotifyArtist:
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

    def get_album_by_id(self, album_id:str) -> Album:
        album_id = self.sanitize_id(album_id)

        cache_key = "album:" + album_id
        ret = self.cache.read(cache_key)
        if not ret:
            ret = self.spotify.album(album_id)
            self.cache.write(cache_key, ret)

        return SpotifyAlbum.from_dict(ret)

    def get_artist_albums(self, artist:Artist) -> list[Album]:
        cache_key = "artist:" + artist.id + ":albums"

        ret = self.cache.read(cache_key)
        if not ret:
            album = self.spotify.artist_albums(artist.id)
            if album is not None and 'items' in album:
                ret = album['items']
            self.cache.write(cache_key, ret)

        albums = []
        if ret:
            for album in ret:
                albums.append(SpotifyAlbum.from_dict(album))

        return albums
    
    def get_album_tracks(self, album:Album) -> list[Track]:
        cache_key = "album:" + album.id + ':tracks'

        ret = self.cache.read(cache_key)
        if not ret:
            t = self.spotify.album_tracks(album.id)
            if t is not None and 'items' in t:
                ret = t['items']
            self.cache.write(cache_key, ret)

        tracks:list[Track] = []
        if ret:
            for track in ret:
                isrc = None
                if 'external_ids' in track and 'isrc' in track['external_ids']:
                    isrc = str(track['external_ids']['isrc'])

                spotifyTrack = SpotifyTrack(
                    id=track['id'],
                    name=track['name'],
                    duration_ms=track['duration_ms'],
                    isrc=isrc,
                    album=album,
                )
                for artist in track['artists']:
                    spotifyTrack.artists.append(self.get_artist(artist['id']))
                tracks.append(spotifyTrack)

        return tracks

    def get_artist_tracks(self, artist:Artist) -> list[Track]:
        tracks = []

        albums = self.get_artist_albums(artist)
        for album in albums:
            tracks += self.get_album_tracks(album)

        return tracks

    def get_artist_top_tracks(self, artist:Artist) -> list[Track]:
        cache_key = "top-tracks:" + artist.id

        ret = self.cache.read(cache_key)
        if not ret:
            ret = self.spotify.artist_top_tracks(artist.id)
            self.cache.write(cache_key, ret)
        
        tracks = []
        if ret is not None and 'tracks' in ret:
            for track in ret['tracks']:
                album = self.get_album_by_id(track['album']['id'])

                isrc = None
                if 'external_ids' in track and 'isrc' in track['external_ids']:
                    isrc = track['external_ids']['isrc']

                artists = []
                for a in track['artists']:
                    a = self.get_artist(a['id'])
                    artists.append(a)

                spotifyTrack = SpotifyTrack(
                    id=track['id'],
                    name=track['name'],
                    duration_ms=track['duration_ms'],
                    isrc=isrc,
                    album=album,
                    artists=artists,
                )
                tracks.append(spotifyTrack)

        return tracks

    def get_playlist_id_for_name(self, playlist_name:str) -> str:
        offset = 0
        while True:
            results = self.spotify.current_user_playlists(limit=50,offset=offset)
            if results and 'items' in results:
                items = results['items']
                for item in items:
                    if item['name'] == playlist_name:
                        return item['id']
            if not results or len(results['items']) < 50:
                break
            offset += 50
        raise ValueError(f"Playlist not found: {playlist_name}")

    def sync(self, playlist_name:str, tracks:list[str]=[]):
        playlist_id = self.get_playlist_id_for_name(playlist_name)

        playlist_tracks = copy.deepcopy(tracks)
        self.spotify.playlist_replace_items(playlist_id, playlist_tracks[0:80])
        del playlist_tracks[0:80]
        while len(playlist_tracks) > 0:
            self.spotify.playlist_add_items(playlist_id, playlist_tracks[0:80])
            del playlist_tracks[0:80]
