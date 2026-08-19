"""Property-based tests for the API response boundary helpers.

The invariant these helpers exist to provide: for ANY decoded-JSON input, they
either return a value of the promised type or raise ApiResponseError. A bare
KeyError, TypeError, AttributeError, or IndexError escaping one of them is the
exact failure the helpers replace, so it is the thing worth testing over
arbitrary input rather than a handful of chosen examples.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shuffleupagus.core.apiresponse import (
    ApiResponseError,
    api_field,
    api_has,
    api_int,
    api_list,
    api_object,
    api_str,
)

SERVICE = "Test Service"

# Anything json.loads can produce, nested a couple of levels deep.
_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)

# Paths that look like the ones real call sites use.
_path = st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=3).map(tuple)

# The errors these helpers exist to stop escaping.
_RAW = (KeyError, TypeError, AttributeError, IndexError)


@given(payload=_json, path=_path)
def test_api_field_raises_only_api_response_error(payload, path):
    try:
        api_field(payload, path, SERVICE)
    except ApiResponseError:
        pass
    except _RAW as exc:
        pytest.fail(f"api_field leaked {type(exc).__name__}: {exc}")


@given(payload=_json, path=_path)
def test_api_str_returns_a_string_or_raises(payload, path):
    try:
        assert isinstance(api_str(payload, path, SERVICE), str)
    except ApiResponseError:
        pass
    except _RAW as exc:
        pytest.fail(f"api_str leaked {type(exc).__name__}: {exc}")


@given(payload=_json, path=_path)
def test_api_int_returns_an_int_or_raises(payload, path):
    try:
        assert isinstance(api_int(payload, path, SERVICE), int)
    except ApiResponseError:
        pass
    except _RAW as exc:
        pytest.fail(f"api_int leaked {type(exc).__name__}: {exc}")


@given(payload=_json, path=_path)
def test_api_list_returns_a_list_or_raises(payload, path):
    try:
        assert isinstance(api_list(payload, path, SERVICE), list)
    except ApiResponseError:
        pass
    except _RAW as exc:
        pytest.fail(f"api_list leaked {type(exc).__name__}: {exc}")


@given(value=_json)
def test_api_object_returns_a_dict_or_raises(value):
    try:
        assert isinstance(api_object(value, "thing", SERVICE), dict)
    except ApiResponseError:
        pass
    except _RAW as exc:
        pytest.fail(f"api_object leaked {type(exc).__name__}: {exc}")


@given(payload=_json, key=st.text(max_size=8))
def test_api_has_always_answers_a_bool(payload, key):
    """api_has is total: it answers for every input and never raises."""
    assert isinstance(api_has(payload, key), bool)


@given(payload=_json, key=st.text(max_size=8))
def test_api_has_agrees_with_a_dict_membership_test(payload, key):
    expected = isinstance(payload, dict) and key in payload
    assert api_has(payload, key) is expected


@given(payload=_json, key=st.text(min_size=1, max_size=8))
def test_api_has_true_implies_api_field_succeeds(payload, key):
    """The point of api_has: a True answer means the read will not raise."""
    if api_has(payload, key):
        api_field(payload, (key,), SERVICE)


@given(text=st.text(), key=st.text(min_size=1, max_size=4))
def test_api_has_never_matches_inside_a_string(text, key):
    """`key in some_string` does substring matching; a string has no fields."""
    assert api_has(text, key) is False


@given(payload=_json, path=_path)
def test_an_error_message_names_the_service(payload, path):
    try:
        api_field(payload, path, SERVICE)
    except ApiResponseError as exc:
        message = str(exc)
    else:
        return
    assert SERVICE in message
