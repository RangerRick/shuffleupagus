from collections.abc import Sequence

from ...core import model
from ...core.apiresponse import api_int

# Named in every message raised from an unexpected API response.
_SERVICE_LABEL = "YouTube"


def sanitize_id(id: str) -> str:
    """Strip a URL wrapper or a "youtube:"/"artist:"/"album:"/"track:" prefix off a model ID.

    Not the same as YoutubeService.sanitize_id(), which resolves channel URLs
    and @handles from user config. This one normalizes IDs already inside the
    model layer, and matches the equivalent helpers in the Spotify and Apple
    Music model modules.
    """
    if id.startswith(("http://", "https://")):
        id = id.rsplit("/", maxsplit=1)[-1]
        id = id.split("?")[0]
        return id

    return id.removeprefix("youtube:").removeprefix("artist:").removeprefix("album:").removeprefix("track:")


class YoutubeArtist(model.Artist):
    handle: str | None = None
    browseIds: dict[str, str | None] = {}
    params: dict[str, str | None] = {}
    # inline results from get_artist (used when browseId/params are absent)
    inlineAlbums: list = []

    def __init__(self, id: str, name: str, handle: str | None = None):
        super().__init__(sanitize_id(id), name)
        self.handle = handle
        self.browseIds = {}
        self.params = {}
        self.inlineAlbums = []

    def display_name(self) -> str:
        return self.handle or self.name

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        ret = YoutubeArtist(obj["channelId"], obj["name"])
        if "albums" in obj:
            ret.browseIds["albums"] = obj["albums"].get("browseId")
            ret.params["albums"] = obj["albums"].get("params")
            ret.inlineAlbums = obj["albums"].get("results", [])
        if "singles" in obj:
            ret.browseIds["singles"] = obj["singles"].get("browseId")
            ret.params["singles"] = obj["singles"].get("params")
            ret.inlineAlbums += obj["singles"].get("results", [])
        if "songs" in obj:
            ret.browseIds["songs"] = obj["songs"].get("browseId")
            ret.params["songs"] = obj["songs"].get("params")
        return ret


class YoutubeAlbum(model.Album):
    def __init__(self, id: str, name: str, release_date=None):
        super().__init__(sanitize_id(id), name, release_date)

    def matches(self, val) -> bool:
        return sanitize_id(val) == self.id

    @staticmethod
    def sanitize_id(id: str) -> str:
        return sanitize_id(id)

    @staticmethod
    def from_dict(obj):
        # ytmusicapi returns different ID fields depending on the context:
        # - get_artist_albums returns 'browseId'
        # - get_album returns 'audioPlaylistId' or 'id'
        album_id = obj.get("browseId") or obj.get("audioPlaylistId") or obj.get("id")

        # ytmusicapi inconsistently uses 'year' and 'type' across different response contexts.
        # In some responses, 'year' contains "Single" or "EP" instead of a year, and
        # 'type' contains the actual year. Validate that any value is a 4-digit number.
        def _as_year(val: str | None) -> str | None:
            return val if (val and val.isdigit() and len(val) == 4) else None

        year = _as_year(obj.get("year")) or _as_year(obj.get("type"))
        return YoutubeAlbum(album_id, obj["title"], year)


class YoutubeTrack(model.Track):
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
        if "videoId" in obj:
            # this track comes from an album track list
            return YoutubeTrack(
                id=obj["videoId"],
                name=obj["title"],
                duration_ms=(api_int(obj, ("duration_seconds",), _SERVICE_LABEL) * 1000),
                # isrc=obj.get('isrc'),
                # album=YoutubeAlbum.from_dict(obj['album']),
                # artists=[YoutubeArtist.from_dict(artist) for artist in obj.get('artists', [])]
            )
        if "videoDetails" in obj:
            # this track comes from a direct track lookup
            return YoutubeTrack(
                id=obj["videoDetails"]["videoId"],
                name=obj["videoDetails"]["title"],
                duration_ms=(api_int(obj, ("videoDetails", "lengthSeconds"), _SERVICE_LABEL) * 1000),
                # isrc=obj.get('isrc'),
                # album=YoutubeAlbum.from_dict(obj['videoDetails']['album']),
                # artists=[YoutubeArtist.from_dict(artist) for artist in obj['videoDetails'].get('artists', [])]
            )
        raise ValueError("Invalid track object")
