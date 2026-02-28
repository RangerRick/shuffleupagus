"""Property-based tests for shuffleupagus.core.util.spread_artist_playlists."""

from unittest.mock import patch

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from shuffleupagus.core.model import Album, Track
from shuffleupagus.core.util import spread_artist_playlists

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_track_id = st.text(
    min_size=1,
    max_size=20,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
)


def _make_track(track_id: str) -> Track:
    return Track(id=track_id, name=track_id, duration_ms=60_000, album=Album("a", "A"))


def _make_playlist(ids: list[str]) -> list[Track]:
    return [_make_track(i) for i in ids]


@st.composite
def artist_playlists(draw, min_artists: int = 0, max_artists: int = 6, max_tracks_per: int = 8):
    """Strategy that produces a valid artist_playlists dict with unique track IDs."""
    num_artists = draw(st.integers(min_value=min_artists, max_value=max_artists))
    total_tracks = num_artists * max_tracks_per
    all_ids = draw(
        st.lists(
            _track_id,
            min_size=total_tracks,
            max_size=total_tracks,
            unique=True,
        )
    )
    playlists = {}
    pos = 0
    for i in range(num_artists):
        count = draw(st.integers(min_value=1, max_value=max_tracks_per))
        artist_id = f"artist_{i}"
        playlists[artist_id] = _make_playlist(all_ids[pos : pos + count])
        pos += count
    return playlists


def _call_spread(playlists, vip_ids=None):
    """Call spread_artist_playlists with deterministic random for reliable assertions."""
    with (
        patch("shuffleupagus.core.util.random.randint", return_value=0),
        patch("shuffleupagus.core.util.random.randrange", return_value=10),
        patch("shuffleupagus.core.util.random.shuffle"),
    ):
        return spread_artist_playlists(playlists, vip_ids or [])


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(playlists=artist_playlists(min_artists=1))  # type: ignore[call-arg]  # ty doesn't understand @st.composite
@settings(max_examples=100)
def test_output_is_subset_of_input_ids(playlists):
    """Every ID in the output was present in some artist's playlist."""
    all_input_ids = {t.id for tracks in playlists.values() for t in tracks}
    result = _call_spread(playlists)
    assert set(result).issubset(all_input_ids)


@given(playlists=artist_playlists(min_artists=1))  # type: ignore[call-arg]
@settings(max_examples=100)
def test_output_has_no_duplicate_ids(playlists):
    """The output list contains no repeated track IDs."""
    result = _call_spread(playlists)
    assert len(result) == len(set(result))


@given(playlists=artist_playlists(min_artists=1))  # type: ignore[call-arg]
@settings(max_examples=100)
def test_output_preserves_all_tracks_when_zero_offset(playlists):
    """With offset forced to 0, every input track appears in the output."""
    all_input_ids = {t.id for tracks in playlists.values() for t in tracks}
    result = _call_spread(playlists)
    assert set(result) == all_input_ids


def test_empty_input_returns_empty_list():
    """An empty artist_playlists dict produces an empty output."""
    assert spread_artist_playlists({}, []) == []


@given(playlists=artist_playlists(min_artists=2, max_artists=4))  # type: ignore[call-arg]
@settings(max_examples=50)
def test_output_is_a_list_of_strings(playlists):
    """The output is always a list of strings (track IDs)."""
    result = _call_spread(playlists)
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, str)


@given(
    regular_playlists=artist_playlists(min_artists=1, max_artists=3),  # type: ignore[call-arg]
    vip_tracks=st.lists(_track_id, min_size=1, max_size=5, unique=True),
)
@settings(max_examples=50)
def test_vip_tracks_appear_in_output(regular_playlists, vip_tracks):
    """VIP artist tracks are included in the output."""
    regular_ids = {t.id for tracks in regular_playlists.values() for t in tracks}
    assume(not regular_ids.intersection(vip_tracks))

    vip_artist_id = "vip_artist"
    playlists = dict(regular_playlists)
    playlists[vip_artist_id] = _make_playlist(vip_tracks)

    result = _call_spread(playlists, vip_ids=[vip_artist_id])
    assert set(vip_tracks).issubset(set(result))
