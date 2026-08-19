from collections.abc import Sequence

from ...core.apiresponse import api_int, api_str
from ...core.model import Album, Artist, Track

# Named in every message raised from an unexpected API response.
_SERVICE_LABEL = "Apple Music"


def sanitize_id(id: str) -> str:
    if id.startswith(("http://", "https://")):
        id = id.rsplit("/", maxsplit=1)[-1]
        id = id.split("?")[0]

    return id


class AppleMusicArtist(Artist):
    def __init__(self, id: str, name: str):
        super().__init__(id, name)

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return AppleMusicArtist(
            id=api_str(obj, ("id",), _SERVICE_LABEL),
            name=api_str(obj, ("attributes", "name"), _SERVICE_LABEL),
        )


class AppleMusicAlbum(Album):
    def __init__(self, id: str, name: str, release_date=None):
        super().__init__(id, name, release_date)

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return AppleMusicAlbum(
            id=api_str(obj, ("id",), _SERVICE_LABEL),
            name=api_str(obj, ("attributes", "name"), _SERVICE_LABEL),
            release_date=api_str(obj, ("attributes", "releaseDate"), _SERVICE_LABEL),
        )


class AppleMusicTrack(Track):
    def __init__(
        self,
        id: str,
        name: str,
        duration_ms: int,
        isrc: str,
        album: Album | None = None,
        artists: Sequence[Artist] | None = None,
    ):
        super().__init__(id=id, name=name, duration_ms=duration_ms, isrc=isrc, album=album, artists=artists)

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj, album: Album | None = None, artists: Sequence[Artist] | None = None):
        return AppleMusicTrack(
            id=api_str(obj, ("id",), _SERVICE_LABEL),
            name=api_str(obj, ("attributes", "name"), _SERVICE_LABEL),
            duration_ms=api_int(obj, ("attributes", "durationInMillis"), _SERVICE_LABEL),
            isrc=api_str(obj, ("attributes", "isrc"), _SERVICE_LABEL),
            album=album,
            artists=artists,
        )
