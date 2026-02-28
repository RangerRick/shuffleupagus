"""Property-based tests for shuffleupagus.services.youtube.model.sanitize_id."""

from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.services.youtube.model import sanitize_id

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# YouTube channel IDs look like "UCxxxxxxxx" but the sanitizer is generic.
_plain_id = st.text(
    min_size=1,
    max_size=40,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
)

_prefix = st.sampled_from([
    "youtube:artist:",
    "youtube:album:",
    "youtube:track:",
    "youtube:",
])

_url_base = st.sampled_from([
    "https://www.youtube.com/channel/",
    "https://music.youtube.com/channel/",
])

_url_path = st.builds(lambda base, id_: f"{base}{id_}", base=_url_base, id_=_plain_id)
_url_with_query = st.builds(lambda url, p: f"{url}?si={p}", url=_url_path, p=_plain_id)

# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@given(raw=_plain_id)
def test_sanitize_plain_is_idempotent(raw):
    """sanitize_id is idempotent on plain IDs."""
    assert sanitize_id(sanitize_id(raw)) == sanitize_id(raw)


@given(prefix=_prefix, id_=_plain_id)
def test_sanitize_prefixed_is_idempotent(prefix, id_):
    """sanitize_id is idempotent on prefixed IDs."""
    raw = prefix + id_
    assert sanitize_id(sanitize_id(raw)) == sanitize_id(raw)


@given(url=_url_path)
def test_sanitize_url_is_idempotent(url):
    """sanitize_id is idempotent on YouTube URLs."""
    assert sanitize_id(sanitize_id(url)) == sanitize_id(url)


@given(url=_url_with_query)
def test_sanitize_url_with_query_is_idempotent(url):
    """sanitize_id is idempotent on YouTube URLs with query strings."""
    assert sanitize_id(sanitize_id(url)) == sanitize_id(url)


# ---------------------------------------------------------------------------
# Format invariants
# ---------------------------------------------------------------------------


@given(raw=st.one_of(_plain_id, _url_path, _url_with_query))
def test_sanitized_id_never_starts_with_http(raw):
    """The sanitized result never starts with 'http'."""
    assert not sanitize_id(raw).startswith("http")


@given(raw=st.one_of(_plain_id, _prefix.flatmap(lambda p: _plain_id.map(lambda i: p + i)), _url_path, _url_with_query))
def test_sanitized_id_has_no_youtube_prefix(raw):
    """The sanitized result never starts with 'youtube:'."""
    assert not sanitize_id(raw).startswith("youtube:")


@given(raw=st.one_of(_plain_id, _url_path, _url_with_query))
def test_sanitized_id_contains_no_query_string(raw):
    """The sanitized result never contains a '?'."""
    assert "?" not in sanitize_id(raw)


@given(id_=_plain_id)
def test_plain_id_unchanged(id_):
    """A plain ID (no URL, no prefix) is returned as-is."""
    assert sanitize_id(id_) == id_


@given(url=_url_path)
def test_url_extracts_last_path_segment(url):
    """sanitize_id on a plain URL (no query string) returns the last path segment."""
    expected = url.rsplit("/", maxsplit=1)[-1]
    assert sanitize_id(url) == expected
