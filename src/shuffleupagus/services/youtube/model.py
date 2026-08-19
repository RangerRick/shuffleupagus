from collections.abc import Sequence

from ...core import model
from ...core.apiresponse import ApiResponseError, api_int, api_list, api_object, api_str

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
        # Before any `in obj` test — see the note in SpotifyTrack.from_dict.
        obj = api_object(obj, "artist", _SERVICE_LABEL)
        ret = YoutubeArtist(
            api_str(obj, ("channelId",), _SERVICE_LABEL),
            api_str(obj, ("name",), _SERVICE_LABEL),
        )
        # Each of these three sections is optional — ytmusicapi includes only
        # the ones the artist actually has — but a section that is present and
        # is not an object would otherwise fail on .get() with an AttributeError.
        for section in ("albums", "singles", "songs"):
            if section not in obj:
                continue
            entry = api_object(obj[section], section, _SERVICE_LABEL)
            ret.browseIds[section] = entry.get("browseId")
            ret.params[section] = entry.get("params")
            if section != "songs":
                ret.inlineAlbums += api_list(entry, ("results",), _SERVICE_LABEL) if "results" in entry else []
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
        # Before any .get() — see the note in SpotifyTrack.from_dict.
        obj = api_object(obj, "album", _SERVICE_LABEL)
        # ytmusicapi returns different ID fields depending on the context:
        # - get_artist_albums returns 'browseId'
        # - get_album returns 'audioPlaylistId' or 'id'
        album_id = obj.get("browseId") or obj.get("audioPlaylistId") or obj.get("id")

        # ytmusicapi inconsistently uses 'year' and 'type' across different response contexts.
        # In some responses, 'year' contains "Single" or "EP" instead of a year, and
        # 'type' contains the actual year. Validate that any value is a 4-digit number.
        def _as_year(val: str | None) -> str | None:
            return val if (val and val.isdigit() and len(val) == 4) else None

        if not isinstance(album_id, str):
            raise ApiResponseError(
                f"{_SERVICE_LABEL} response album has no usable ID: "
                "none of browseId, audioPlaylistId, or id held a string"
            )

        year = _as_year(obj.get("year")) or _as_year(obj.get("type"))
        return YoutubeAlbum(album_id, api_str(obj, ("title",), _SERVICE_LABEL), year)


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
        # Before any `in obj` test — see the note in SpotifyTrack.from_dict.
        obj = api_object(obj, "track", _SERVICE_LABEL)
        if "videoId" in obj:
            # this track comes from an album track list
            return YoutubeTrack(
                id=api_str(obj, ("videoId",), _SERVICE_LABEL),
                name=api_str(obj, ("title",), _SERVICE_LABEL),
                duration_ms=(api_int(obj, ("duration_seconds",), _SERVICE_LABEL) * 1000),
                # isrc=obj.get('isrc'),
                # album=YoutubeAlbum.from_dict(obj['album']),
                # artists=[YoutubeArtist.from_dict(artist) for artist in obj.get('artists', [])]
            )
        if "videoDetails" in obj:
            # this track comes from a direct track lookup
            return YoutubeTrack(
                id=api_str(obj, ("videoDetails", "videoId"), _SERVICE_LABEL),
                name=api_str(obj, ("videoDetails", "title"), _SERVICE_LABEL),
                duration_ms=(api_int(obj, ("videoDetails", "lengthSeconds"), _SERVICE_LABEL) * 1000),
                # isrc=obj.get('isrc'),
                # album=YoutubeAlbum.from_dict(obj['videoDetails']['album']),
                # artists=[YoutubeArtist.from_dict(artist) for artist in obj['videoDetails'].get('artists', [])]
            )
        raise ApiResponseError(
            f"{_SERVICE_LABEL} response track has neither a videoId nor a videoDetails section, "
            "so it is not a track this code can read"
        )
