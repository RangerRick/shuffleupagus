import datetime
import random
import string
import unicodedata

from .cache import Cache
from .config import Config
from .util import logger, spread_artist_playlists

MAX_TOP_TRACKS = 5
MAX_ARTIST_TRACKS = 10
MAX_TRACK_LENGTH_MS = 8 * 60 * 1000 # 8 minutes

class ShufObject:
    id:str
    name:str

    def __init__(self, id:str, name:str):
        self.id = id
        self.name = name

    def matches(self, val) -> bool:
        return val == self.id

    def is_excluded(self, ids:list[str]) -> bool:
        return any(self.matches(id) for id in ids)

    @staticmethod
    def sanitize_id(id:str) -> str:
        return id

    @staticmethod
    def from_dict(obj:any): # type: ignore
        raise NotImplementedError

class Artist(ShufObject):
    def __str__(self) -> str:
        return f"Artist({self.id}): {self.name}"

    def __repr__(self):
        return f"artist[id={self.id}, name={self.name}]"

class Album(ShufObject):
    release_date:datetime.date|None = None

    def __init__(self, id:str, name:str, release_date=None):
        super().__init__(id, name)

        if release_date:
            if isinstance(release_date, str):
                if '-' not in release_date:
                    release_date = release_date + '-01-01'
                self.release_date = datetime.date.fromisoformat(release_date)
            elif isinstance(release_date, datetime.date):
                self.release_date = release_date
            else:
                raise ValueError

    def __str__(self) -> str:
        return f"Album({self.id}): {self.name}"

    def __repr__(self):
        return f"album[id={self.id}, name={self.name}]"

class Track(ShufObject):
    duration_ms = 0
    isrc:str|None = None
    album:Album|None = None
    artists:list[Artist] = []
    dedupe_hash = None

    def __init__(self, id:str, name:str, duration_ms:int, isrc:str|None=None, album:Album|None=None, artists:list[Artist]=[]):
        super().__init__(id, name)
        self.duration_ms = duration_ms
        self.isrc = isrc
        self.album = album
        self.artists = artists

        # create a dedupe hash based on name and duration rounded to nearest second
        cleaned_name = unicodedata.normalize('NFKD', name).casefold().strip()
        cleaned_name = ''.join(c for c in cleaned_name if unicodedata.category(c) != 'Mn' and c not in string.punctuation)
        rounded_ms = str(int(duration_ms - (duration_ms % 2000)))
        self.dedupe_hash = f"{cleaned_name}:{rounded_ms}"

    def __str__(self) -> str:
        return f"Track({self.id}): {self.name}"

    def __repr__(self):
        return f"track[id={self.id}, name={self.name}]"

    def longer_than(self, duration_ms:int) -> bool:
        return self.duration_ms > duration_ms

class Service:
    name:str
    cache:Cache

    def __init__(self, config:Config):
        self.cache = Cache(self.name)
        self.config = config.service(self.name)

    def sanitize_id(self, id:str) -> str:
        return id

    def login(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def get_artist(self, artist:str|Artist) -> Artist|None:
        raise NotImplementedError

    def get_album_by_id(self, album_id:str) -> Album|None:
        raise NotImplementedError

    def get_artist_albums(self, artist:Artist) -> list[Album]:
        raise NotImplementedError

    def get_album_tracks(self, album:Album) -> list[Track]:
        raise NotImplementedError

    def get_artist_tracks(self, artist:Artist) -> list[Track]:
        raise NotImplementedError

    def get_artist_top_tracks(self, artist:Artist) -> list[Track]:
        raise NotImplementedError

    def get_playlist_id_for_name(self, playlist_name:str) -> str:
        raise NotImplementedError

    def generate_playlist(self, artist_ids:list[str]=[],
                            excluded_album_ids:list[str]=[],
                            excluded_track_ids:list[str]=[],
                            vip_artist_ids:list[str]=[]) -> list[str]:
        artist_playlists = {}

        artist_count = 0
        for artistId in artist_ids:
            # logger.debug(f"* processing artist {artistId} ({artist_count+1}/{len(artist_ids)})")
            artist = self.get_artist(artistId)
            if artist is None:
                logger.error(f"* artist {artistId} not found, skipping ({artist_count+1}/{len(artist_ids)})")
                raise ValueError(f"Artist {artistId} not found")
            logger.info(f"* processing artist {artist.name} ({artist_count+1}/{len(artist_ids)})")

            logger.debug("  * getting top tracks")
            top_tracks = self.get_artist_top_tracks(artist)
            top_tracks = list(filter(lambda track :
                                     not track.is_excluded(excluded_track_ids)
                                     and not track.longer_than(MAX_TRACK_LENGTH_MS)
                                     and track.album is not None
                                     and not track.album.is_excluded(excluded_album_ids), top_tracks))
            top_track_ids = [track.id for track in top_tracks]

            seen_hashes = set(map(lambda track : track.dedupe_hash, top_tracks))

            logger.debug("  * getting remaining tracks")
            artist_tracks = self.get_artist_tracks(artist)
            artist_tracks = list(filter(lambda track :
                                        track.id not in top_track_ids
                                        and not track.is_excluded(excluded_track_ids)
                                        and not track.longer_than(MAX_TRACK_LENGTH_MS)
                                        and track.album is not None
                                        and not track.album.is_excluded(excluded_album_ids), artist_tracks))
            logger.debug(f"  * found {len(artist_tracks)} tracks")

            # debug artist tracks by ISRC if available
            logger.debug("  * deduplicating tracks")
            deduped_artist_tracks:list[Track] = []
            for track in artist_tracks:
                dedupe_hash = getattr(track, "dedupe_hash", None)
                if dedupe_hash:
                    if dedupe_hash in seen_hashes:
                        # print(f"* found duplicate track {track.name} ({artist.name}, {track.id})", flush=True)
                        continue
                    seen_hashes.add(dedupe_hash)
                deduped_artist_tracks.append(track)
            artist_tracks = deduped_artist_tracks

            # shuffle so that we only pick a random subset of artist tracks if they have many
            random.shuffle(artist_tracks)

            # Take up to MAX_TOP_TRACKS top tracks, append all shuffled artist tracks,
            # then append the remaining top tracks.
            # 
            # This means that when we truncate, if someone has no album tracks,
            # it will get the rest of the top tracks to fill out up to MAX+MAX length

            artist_playlist = top_tracks[0:MAX_TOP_TRACKS] + artist_tracks + top_tracks[MAX_TOP_TRACKS:-1]
            artist_playlist = artist_playlist[0:MAX_TOP_TRACKS+MAX_ARTIST_TRACKS]
            logger.info(f"  * found {len(artist_playlist)} valid tracks for {artist.name}")

            # then we shuffle the whole truncated playlist of MAX+MAX length and save it
            random.shuffle(artist_playlist)

            artist_playlists[artist.id] = artist_playlist

            artist_count += 1

        return spread_artist_playlists(artist_playlists, vip_artist_ids)

    def sync(self, playlist_name:str, tracks:list[str]=[]):
        raise NotImplementedError
