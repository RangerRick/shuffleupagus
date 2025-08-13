from ...core import model

def sanitize_id(id: str) -> str:
    if id.startswith("http"):
        id = id.split('/')[-1]
        id = id.split('?')[0]
        return id

    return id.removeprefix("spotify:").removeprefix("artist:").removeprefix("album:").removeprefix("track:")

class SpotifyArtist(model.Artist):
    def __init__(self, id:str, name:str):
        super().__init__(sanitize_id(id), name)

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return SpotifyArtist(obj['id'], obj['name'])

class SpotifyAlbum(model.Album):
    def __init__(self, id:str, name:str, release_date=None):
        super().__init__(sanitize_id(id), name, release_date)

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
      return SpotifyAlbum(obj['id'], obj['name'], obj['release_date'])

class SpotifyTrack(model.Track):
    def __init__(self, id:str, name:str, duration_ms:int, isrc:str|None=None, album:model.Album|None=None, artists:list[model.Artist]=[]):
        super().__init__(
            id=sanitize_id(id),
            name=name,
            duration_ms=duration_ms,
            isrc=isrc,
            album=album,
            artists=artists
        )

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return SpotifyTrack(
            id=obj['id'],
            name=obj['name'],
            duration_ms=obj['duration_ms'],
            isrc=obj.get('isrc'),
            album=SpotifyAlbum.from_dict(obj['album']),
            artists=[SpotifyArtist.from_dict(artist) for artist in obj.get('artists', [])]
        )