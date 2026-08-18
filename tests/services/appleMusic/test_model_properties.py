"""Property-based tests for shuffleupagus.services.appleMusic.model.sanitize_id."""

from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.services.appleMusic.model import sanitize_id

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Apple Music IDs are numeric strings in practice, but the sanitizer doesn't care.
_plain_id = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
)

_url_base = st.sampled_from(
    [
        "https://music.apple.com/us/artist/",
        "https://music.apple.com/us/album/my-album/",
        "https://music.apple.com/gb/album/title/",
    ]
)

_url_path = st.builds(lambda base, id_: f"{base}{id_}", base=_url_base, id_=_plain_id)
_url_with_query = st.builds(lambda url, p: f"{url}?l=en&{p}", url=_url_path, p=_plain_id)

# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@given(raw=_plain_id)
def test_sanitize_plain_is_idempotent(raw):
    """sanitize_id is idempotent on plain IDs."""
    assert sanitize_id(sanitize_id(raw)) == sanitize_id(raw)


@given(url=_url_path)
def test_sanitize_url_is_idempotent(url):
    """sanitize_id is idempotent on Apple Music URL paths."""
    assert sanitize_id(sanitize_id(url)) == sanitize_id(url)


@given(url=_url_with_query)
def test_sanitize_url_with_query_is_idempotent(url):
    """sanitize_id is idempotent on Apple Music URLs with query strings."""
    assert sanitize_id(sanitize_id(url)) == sanitize_id(url)


# ---------------------------------------------------------------------------
# Format invariants
# ---------------------------------------------------------------------------


@given(raw=st.one_of(_url_path, _url_with_query))
def test_sanitized_id_never_starts_with_url_scheme(raw):
    """For URL inputs, the sanitized result never starts with a URL scheme."""
    result = sanitize_id(raw)
    assert not result.startswith(("http://", "https://"))


@given(raw=st.one_of(_plain_id, _url_path, _url_with_query))
def test_sanitized_id_contains_no_query_string(raw):
    """The sanitized result never contains a '?'."""
    assert "?" not in sanitize_id(raw)


@given(id_=_plain_id)
def test_plain_id_unchanged(id_):
    """A plain ID (no URL) is returned as-is."""
    assert sanitize_id(id_) == id_


@given(url=_url_path)
def test_url_extracts_last_path_segment(url):
    """sanitize_id on a URL returns the last path segment."""
    expected = url.rsplit("/", maxsplit=1)[-1]
    assert sanitize_id(url) == expected


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
