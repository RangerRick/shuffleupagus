from collections.abc import Sequence

from ...core import model
from ...core.apiresponse import api_field, api_int, api_list, api_object, api_str

# Named in every message raised from an unexpected API response.
_SERVICE_LABEL = "Spotify"


def sanitize_id(id: str) -> str:
    if id.startswith(("http://", "https://")):
        id = id.rsplit("/", maxsplit=1)[-1]
        id = id.split("?")[0]
        return id

    return id.removeprefix("spotify:").removeprefix("artist:").removeprefix("album:").removeprefix("track:")


class SpotifyArtist(model.Artist):
    def __init__(self, id: str, name: str):
        super().__init__(sanitize_id(id), name)

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return SpotifyArtist(
            api_str(obj, ("id",), _SERVICE_LABEL),
            api_str(obj, ("name",), _SERVICE_LABEL),
        )


class SpotifyAlbum(model.Album):
    def __init__(self, id: str, name: str, release_date=None):
        super().__init__(sanitize_id(id), name, release_date)

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        return SpotifyAlbum(
            api_str(obj, ("id",), _SERVICE_LABEL),
            api_str(obj, ("name",), _SERVICE_LABEL),
            api_str(obj, ("release_date",), _SERVICE_LABEL),
        )


class SpotifyTrack(model.Track):
    def __init__(
        self,
        id: str,
        name: str,
        duration_ms: int,
        isrc: str | None = None,
        album: model.Album | None = None,
        artists: Sequence[model.Artist] | None = None,
    ):
        super().__init__(
            id=sanitize_id(id), name=name, duration_ms=duration_ms, isrc=isrc, album=album, artists=artists
        )

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        # Checked before anything reads an optional key: `"artists" in obj`
        # raises TypeError rather than answering False when obj is not a
        # container, which is the raw failure this whole helper layer replaces.
        obj = api_object(obj, "track", _SERVICE_LABEL)
        # "isrc" and "artists" stay optional: Spotify omits both on some track
        # shapes, and an absent field is not a malformed response. A present
        # "artists" that is not a list of objects is, so it is still checked.
        raw_artists = api_list(obj, ("artists",), _SERVICE_LABEL) if "artists" in obj else []
        return SpotifyTrack(
            id=api_str(obj, ("id",), _SERVICE_LABEL),
            name=api_str(obj, ("name",), _SERVICE_LABEL),
            duration_ms=api_int(obj, ("duration_ms",), _SERVICE_LABEL),
            isrc=obj.get("isrc"),
            album=SpotifyAlbum.from_dict(
                api_object(api_field(obj, ("album",), _SERVICE_LABEL), "album", _SERVICE_LABEL)
            ),
            artists=[
                SpotifyArtist.from_dict(api_object(artist, "artists[] entry", _SERVICE_LABEL)) for artist in raw_artists
            ],
        )
