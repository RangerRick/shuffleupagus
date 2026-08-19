"""Property-based tests for every service's from_dict, over arbitrary JSON.

Issue #59 is about one invariant: a changed or truncated API response must
produce an ApiResponseError that names the service and the field, never a bare
KeyError or TypeError raised frames away from the request. That is a claim about
every possible response, not about the handful of malformed payloads a unit test
can enumerate, so it is checked here against arbitrary decoded JSON.

The from_dict methods live in three modules; they are tested together because
the invariant is one shared contract rather than three separate ones.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.core.apiresponse import ApiResponseError
from shuffleupagus.services.appleMusic.model import (
    AppleMusicAlbum,
    AppleMusicArtist,
    AppleMusicTrack,
)
from shuffleupagus.services.spotify.model import SpotifyAlbum, SpotifyArtist, SpotifyTrack
from shuffleupagus.services.youtube.model import YoutubeAlbum, YoutubeArtist, YoutubeTrack

# Anything json.loads can produce.
_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)

# Keys the real responses use, so generated payloads sometimes look plausible
# enough to reach the code past the first check rather than failing at it.
_KNOWN_KEYS = [
    "id",
    "name",
    "title",
    "attributes",
    "artists",
    "album",
    "albums",
    "singles",
    "songs",
    "results",
    "browseId",
    "audioPlaylistId",
    "params",
    "channelId",
    "videoId",
    "videoDetails",
    "duration_seconds",
    "duration_ms",
    "lengthSeconds",
    "durationInMillis",
    "isrc",
    "release_date",
    "releaseDate",
    "year",
    "type",
    "external_ids",
]

_plausible = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text() | st.lists(st.text(), max_size=3),
    lambda children: (
        st.dictionaries(st.sampled_from(_KNOWN_KEYS), children, max_size=6) | st.lists(children, max_size=3)
    ),
    max_leaves=14,
)

_payload = _json | _plausible

_FROM_DICTS = [
    AppleMusicArtist.from_dict,
    AppleMusicAlbum.from_dict,
    AppleMusicTrack.from_dict,
    SpotifyArtist.from_dict,
    SpotifyAlbum.from_dict,
    SpotifyTrack.from_dict,
    YoutubeArtist.from_dict,
    YoutubeAlbum.from_dict,
    YoutubeTrack.from_dict,
]

# The raw errors #59 exists to stop escaping.
_RAW = (KeyError, TypeError, AttributeError, IndexError)


@pytest.mark.parametrize("from_dict", _FROM_DICTS, ids=lambda f: f.__qualname__)
@given(payload=_payload)
def test_from_dict_raises_only_api_response_error(from_dict, payload):
    try:
        from_dict(payload)
    except ApiResponseError:
        pass
    except _RAW as exc:
        pytest.fail(f"{from_dict.__qualname__} leaked {type(exc).__name__} for {payload!r:.200}: {exc}")


@pytest.mark.parametrize("from_dict", _FROM_DICTS, ids=lambda f: f.__qualname__)
@given(payload=_payload)
def test_from_dict_error_names_a_service(from_dict, payload):
    try:
        from_dict(payload)
    except ApiResponseError as exc:
        message = str(exc)
    else:
        return
    assert any(name in message for name in ("Apple Music", "Spotify", "YouTube"))


@pytest.mark.parametrize("from_dict", _FROM_DICTS, ids=lambda f: f.__qualname__)
@given(payload=st.one_of(st.none(), st.booleans(), st.integers(), st.text(), st.lists(_json, max_size=3)))
def test_from_dict_rejects_a_non_object_payload(from_dict, payload):
    """No response that is not an object can produce a model."""
    with pytest.raises(ApiResponseError):
        from_dict(payload)
