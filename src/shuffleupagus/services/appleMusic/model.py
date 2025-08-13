from ...core.model import Artist, Album, Track


def sanitize_id(id:str) -> str:
    if id.startswith("http"):
        id = id.split('/')[-1]
        id = id.split('?')[0]

    return id

class AppleMusicArtist(Artist):
    def __init__(self, id:str, name:str):
        super().__init__(id, name)

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return AppleMusicArtist(
            id=obj['id'],
            name=obj['attributes']['name'],
        )

class AppleMusicAlbum(Album):
    def __init__(self, id:str, name:str, release_date=None):
        super().__init__(id, name, release_date)

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return AppleMusicAlbum(
            id=obj['id'],
            name=obj['attributes']['name'],
            release_date=obj['attributes']['releaseDate'],
        )

class AppleMusicTrack(Track):
    def __init__(self, id:str, name:str, duration_ms:int, isrc:str, album:Album|None=None, artists:list[Artist]=[]):
        super().__init__(
            id=id,
            name=name,
            duration_ms=duration_ms,
            isrc=isrc,
            album=album,
            artists=artists
        )

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj, album:Album|None=None, artists:list[Artist]=[]):
        return AppleMusicTrack(
            id=obj['id'],
            name=obj['attributes']['name'],
            duration_ms=obj['attributes']['durationInMillis'],
            isrc=obj['attributes']['isrc'],
            album=album,
            artists=artists,
        )