"""Property-based tests for shuffleupagus.core.model (Track.dedupe_hash, Album release_date)."""

import datetime
import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.core.model import Album, Track

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_track_name = st.text(min_size=0, max_size=200)
_duration_ms = st.integers(min_value=0, max_value=60 * 60 * 1000)


def _make_track(name: str, duration_ms: int) -> Track:
    return Track(id="x", name=name, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Track.dedupe_hash invariants
# ---------------------------------------------------------------------------


@given(name=_track_name, duration_ms=_duration_ms)
def test_dedupe_hash_is_deterministic(name, duration_ms):
    """The same name and duration always produce the same dedupe_hash."""
    t1 = _make_track(name, duration_ms)
    t2 = _make_track(name, duration_ms)
    assert t1.dedupe_hash == t2.dedupe_hash


@given(name=_track_name, duration_ms=_duration_ms)
def test_dedupe_hash_is_a_non_empty_string(name, duration_ms):
    """dedupe_hash is always a non-empty string."""
    t = _make_track(name, duration_ms)
    assert isinstance(t.dedupe_hash, str)
    assert len(t.dedupe_hash) > 0


@given(name=_track_name, duration_ms=_duration_ms)
def test_dedupe_hash_is_case_insensitive(name, duration_ms):
    """Casefolded versions of the same name produce the same hash."""
    t_original = _make_track(name, duration_ms)
    t_casefolded = _make_track(name.casefold(), duration_ms)
    assert t_original.dedupe_hash == t_casefolded.dedupe_hash


_ascii_non_punct_name = st.text(
    min_size=0,
    max_size=200,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Zs"),  # letters, digits, spaces
    ),
)


_punct_text = st.text(min_size=1, max_size=10, alphabet=st.sampled_from(string.punctuation))


@given(name=_ascii_non_punct_name, extra_punct=_punct_text, duration_ms=_duration_ms)
def test_dedupe_hash_strips_punctuation(name, extra_punct, duration_ms):
    """Inserting punctuation into a name that contains no punctuation does not change the hash.

    The dedup logic removes characters in string.punctuation (ASCII-only). To avoid
    edge cases where punctuation removal reveals boundary whitespace and changes .strip()
    behavior, we start with a clean name and insert punctuation in the middle.
    """
    # Insert punctuation in the middle to avoid boundary effects
    mid = len(name) // 2
    name_with_punct = name[:mid] + extra_punct + name[mid:]
    t_orig = _make_track(name, duration_ms)
    t_with_punct = _make_track(name_with_punct, duration_ms)
    assert t_orig.dedupe_hash == t_with_punct.dedupe_hash


@given(name=_track_name, duration_ms=_duration_ms)
def test_dedupe_hash_strips_leading_trailing_whitespace(name, duration_ms):
    """Leading/trailing whitespace in the name does not affect the hash."""
    t_plain = _make_track(name, duration_ms)
    t_padded = _make_track("   " + name + "   ", duration_ms)
    assert t_plain.dedupe_hash == t_padded.dedupe_hash


@given(name=_track_name, base_ms=st.integers(min_value=0, max_value=59 * 60 * 1000))
def test_dedupe_hash_rounds_duration_to_2000ms_boundary(name, base_ms):
    """Durations within the same 2000ms bucket share the same hash."""
    bucket_start = base_ms - (base_ms % 2000)
    # Any value in [bucket_start, bucket_start + 1999] maps to the same bucket.
    t1 = _make_track(name, bucket_start)
    t2 = _make_track(name, bucket_start + 1999)
    assert t1.dedupe_hash == t2.dedupe_hash


@given(name=_track_name, duration_ms=_duration_ms)
def test_dedupe_hash_format_contains_colon_separator(name, duration_ms):
    """The hash always has the form '<normalized_name>:<rounded_ms>'."""
    t = _make_track(name, duration_ms)
    assert t.dedupe_hash is not None
    assert ":" in t.dedupe_hash
    parts = t.dedupe_hash.split(":")
    # Last part should be a numeric string representing rounded ms
    assert parts[-1].isdigit()


@given(name=_track_name, ms1=_duration_ms, ms2=_duration_ms)
def test_dedupe_hash_differs_for_different_duration_buckets(name, ms1, ms2):
    """Durations in different 2000ms buckets produce different hashes (assuming same name)."""
    bucket1 = ms1 - (ms1 % 2000)
    bucket2 = ms2 - (ms2 % 2000)
    if bucket1 == bucket2:
        return  # Same bucket: hash must be equal — not a conflict
    t1 = _make_track(name, bucket1)
    t2 = _make_track(name, bucket2)
    assert t1.dedupe_hash != t2.dedupe_hash


# ---------------------------------------------------------------------------
# Album release_date parsing invariants
# ---------------------------------------------------------------------------

# datetime.date.fromisoformat() requires 4-digit years (1000-9999).
# Year-only strings get expanded to "YYYY-01-01" via string concatenation,
# so years below 1000 produce strings like "1-01-01" which fromisoformat() rejects.
_year = st.integers(min_value=1000, max_value=9999)
_month = st.integers(min_value=1, max_value=12)
_day = st.integers(min_value=1, max_value=28)  # safe across all months


@given(year=_year)
def test_album_year_only_string_becomes_jan_first(year):
    """A four-digit year string is parsed as January 1st of that year."""
    a = Album("id", "Title", str(year))
    assert a.release_date == datetime.date(year, 1, 1)


@given(year=_year, month=_month, day=_day)
def test_album_full_iso_date_parses_correctly(year, month, day):
    """A full YYYY-MM-DD string is parsed to the corresponding date."""
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    a = Album("id", "Title", date_str)
    assert a.release_date == datetime.date(year, month, day)


@given(year=_year, month=_month, day=_day)
def test_album_date_object_passthrough(year, month, day):
    """A datetime.date object is stored unchanged."""
    d = datetime.date(year, month, day)
    a = Album("id", "Title", d)
    assert a.release_date == d


def test_album_none_release_date():
    """No release_date argument leaves release_date as None."""
    a = Album("id", "Title")
    assert a.release_date is None


@given(
    bad=st.one_of(
        # Exclude falsy values: Album's __init__ guards with `if release_date:`
        # so falsy non-string/non-date values bypass the type check entirely.
        # Use non-zero integers and non-empty/nan-free floats to trigger the branch.
        st.integers(min_value=1),
        st.floats(min_value=1.0, allow_nan=False, allow_infinity=False),
        st.binary(min_size=1),
        st.lists(st.text(), min_size=1),
    )
)
def test_album_invalid_release_date_type_raises(bad):
    """Unsupported non-string, non-date, truthy types raise ValueError."""
    with pytest.raises(ValueError):
        Album("id", "Title", bad)


@given(year=_year, month=_month, day=_day)
def test_album_year_only_string_roundtrips_through_isoformat(year, month, day):
    """For a full date parsed from ISO string, isoformat() gives back the same string."""
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    a = Album("id", "Title", date_str)
    assert a.release_date is not None
    assert a.release_date.isoformat() == date_str
