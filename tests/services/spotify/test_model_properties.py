"""Property-based tests for shuffleupagus.services.spotify.model.sanitize_id."""

from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.services.spotify.model import sanitize_id

# ---------------------------------------------------------------------------
# Strategies for Spotify ID shapes
# ---------------------------------------------------------------------------

# Plain base-62 Spotify IDs (alphanumeric, 22 chars in practice, but we test generically)
_plain_id = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
)

_prefix = st.sampled_from(["spotify:artist:", "spotify:album:", "spotify:track:", "spotify:"])

_url_path = st.builds(
    lambda base, id_: f"{base}{id_}",
    base=st.sampled_from(
        [
            "https://open.spotify.com/artist/",
            "https://open.spotify.com/album/",
            "https://open.spotify.com/track/",
        ]
    ),
    id_=_plain_id,
)

_url_with_query = st.builds(
    lambda url, param: f"{url}?si={param}",
    url=_url_path,
    param=_plain_id,
)


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@given(raw=_plain_id)
def test_sanitize_plain_id_is_idempotent(raw):
    """Calling sanitize_id twice on a plain ID is the same as calling it once."""
    assert sanitize_id(sanitize_id(raw)) == sanitize_id(raw)


@given(prefix=_prefix, id_=_plain_id)
def test_sanitize_prefixed_id_is_idempotent(prefix, id_):
    """sanitize_id is idempotent for prefixed forms."""
    raw = prefix + id_
    assert sanitize_id(sanitize_id(raw)) == sanitize_id(raw)


@given(url=_url_path)
def test_sanitize_url_is_idempotent(url):
    """sanitize_id is idempotent for URL forms."""
    assert sanitize_id(sanitize_id(url)) == sanitize_id(url)


@given(url=_url_with_query)
def test_sanitize_url_with_query_is_idempotent(url):
    """sanitize_id is idempotent for URLs with query parameters."""
    assert sanitize_id(sanitize_id(url)) == sanitize_id(url)


# ---------------------------------------------------------------------------
# Format invariants
# ---------------------------------------------------------------------------


@given(raw=st.one_of(_url_path, _url_with_query))
def test_sanitized_id_never_starts_with_url_scheme(raw):
    """For URL inputs, the sanitized result never starts with a URL scheme."""
    result = sanitize_id(raw)
    assert not result.startswith(("http://", "https://"))


@given(raw=st.one_of(_plain_id, _prefix.flatmap(lambda p: _plain_id.map(lambda i: p + i)), _url_path, _url_with_query))
def test_sanitized_id_has_no_spotify_prefix(raw):
    """The sanitized result never starts with 'spotify:'."""
    assert not sanitize_id(raw).startswith("spotify:")


@given(raw=st.one_of(_plain_id, _url_path, _url_with_query))
def test_sanitized_id_contains_no_query_string(raw):
    """The sanitized result contains no '?' character."""
    assert "?" not in sanitize_id(raw)


@given(raw=_url_path)
def test_sanitize_url_extracts_last_path_segment(raw):
    """sanitize_id on a URL returns the last path segment."""
    expected = raw.rsplit("/", maxsplit=1)[-1]
    assert sanitize_id(raw) == expected


@given(id_=_plain_id)
def test_sanitize_plain_id_unchanged(id_):
    """A plain ID with no prefix or URL is returned unchanged."""
    assert sanitize_id(id_) == id_


# ---------------------------------------------------------------------------
# URL scheme precision and malformed input
# ---------------------------------------------------------------------------

# Scheme look-alikes. Only "http://" and "https://" mark a URL, so these must
# pass through untouched — a bare startswith("http") check would mangle them.
_not_a_url = st.builds(
    lambda scheme, id_: f"{scheme}{id_}",
    scheme=st.sampled_from(["httpd://", "ftp://", "sftp://", "http:", "https:", "http", "//", "HTTP://"]),
    id_=_plain_id,
)


@given(raw=_not_a_url)
def test_non_http_scheme_is_left_alone(raw):
    """Only http:// and https:// are URLs; look-alikes keep their full text."""
    assert sanitize_id(raw) == raw


@given(raw=st.text())
def test_sanitize_never_raises_on_arbitrary_text(raw):
    """sanitize_id handles any string without raising."""
    sanitize_id(raw)


_messy_url = st.builds(
    lambda base, userinfo, port, id_, query: f"{base}{userinfo}x.com{port}/{id_}{query}",
    base=st.sampled_from(["https://", "http://"]),
    userinfo=st.sampled_from(["", "user:pass@"]),
    port=st.sampled_from(["", ":8080"]),
    id_=_plain_id,
    query=st.sampled_from(["", "?si=abc", "?a=1&b=2"]),
)


@given(raw=_messy_url)
def test_messy_url_yields_bare_id(raw):
    """Userinfo, ports, and query strings do not leak into the extracted ID."""
    result = sanitize_id(raw)
    assert "?" not in result
    assert "/" not in result
    assert "@" not in result
    assert ":" not in result


def test_doubled_prefix_strips_only_one_layer():
    """Documented limitation: prefix stripping is single-pass, not repeated.

    No real Spotify input carries a doubled prefix, so this is recorded as
    actual behaviour rather than fixed — do not write an idempotence test over
    arbitrary text, it will fail on inputs like this one.
    """
    assert sanitize_id("spotify:spotify:abc") == "spotify:abc"
