"""Property-based tests for the Apple Music fetch-failure triage.

`_absent_or_raise` decides whether a failed fetch means "the catalogue does not
have this" or "we could not find out". Getting that wrong in the permissive
direction drops an artist from the playlist with nothing said, so the claim
worth testing is about every exception shape, not the three a unit test picks.

It reads `.response.status_code` off whatever it is handed, and exceptions in
the wild carry that attribute in every combination: absent, present but None,
present but not a number.
"""

from unittest.mock import MagicMock

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.services.appleMusic.service import AppleMusicService

# 404 is the only status that means the thing is genuinely not there.
_ABSENT = 404
# _reraise_if_fatal owns these and raises its own messages for them.
_FATAL = (401, 403, 429)


def _svc() -> AppleMusicService:
    """A bare service, built by hand rather than by a fixture.

    Hypothesis rejects a function-scoped fixture under @given, and the triage
    reads nothing off the instance except self.tag, so there is no state to
    share between examples anyway.
    """
    service = AppleMusicService.__new__(AppleMusicService)
    service.tag = "[apple] "
    return service


def _with_status(status):
    response = MagicMock()
    response.status_code = status
    response.headers = {}
    return requests.exceptions.HTTPError("boom", response=response)


@given(status=st.integers(min_value=100, max_value=599))
def test_only_a_404_is_treated_as_absent(status):
    """Every other status is a failure to find out, and must not answer None."""
    exc = _with_status(status)
    if status == _ABSENT:
        assert _svc()._absent_or_raise(exc, "artist", "a1") is None
    else:
        with pytest.raises(RuntimeError):
            _svc()._absent_or_raise(exc, "artist", "a1")


@given(status=st.sampled_from(_FATAL))
def test_fatal_statuses_keep_their_own_message(status):
    """_reraise_if_fatal owns 401/403/429 and says something different."""
    with pytest.raises(RuntimeError) as excinfo:
        _svc()._absent_or_raise(_with_status(status), "artist", "a1")
    message = str(excinfo.value)
    assert "could not fetch" not in message


@given(exc=st.sampled_from([ValueError("no response"), OSError("network"), Exception("bare")]))
def test_an_exception_with_no_response_raises(exc):
    """A connection error carries no response, so there is no status to read."""
    with pytest.raises(RuntimeError, match="could not fetch"):
        _svc()._absent_or_raise(exc, "artist", "a1")


@given(status=st.one_of(st.none(), st.text(max_size=5), st.floats(allow_nan=False), st.booleans()))
def test_a_non_integer_status_raises_rather_than_reading_as_absent(status):
    """A status that is not 404 — including one that is not a number — is a failure.

    Guessing "absent" from a shape we do not understand is the dangerous
    direction to guess.
    """
    with pytest.raises(RuntimeError, match="could not fetch"):
        _svc()._absent_or_raise(_with_status(status), "artist", "a1")


@given(
    what=st.text(min_size=1, max_size=2000),
    which=st.text(min_size=1, max_size=2000),
    detail=st.text(min_size=1, max_size=2000),
)
def test_the_message_stays_bounded_whatever_it_names(what, which, detail):
    """`which` is built from API response data at half the call sites."""
    with pytest.raises(RuntimeError) as excinfo:
        _svc()._absent_or_raise(Exception(detail), what[:40], which)
    assert len(str(excinfo.value)) < 600


@given(which=st.text(min_size=1, max_size=200))
def test_the_message_escapes_what_it_names(which):
    """A control character in an identifier must not reach the log raw."""
    with pytest.raises(RuntimeError) as excinfo:
        _svc()._absent_or_raise(Exception("failed"), "artist", "\x1b[31m" + which)
    assert "\x1b" not in str(excinfo.value)
