"""Tests for the API response boundary helpers."""

import pytest

from shuffleupagus.core.apiresponse import ApiResponseError, api_field, api_int, api_list

SERVICE = "Apple Music"


# --- api_field ---------------------------------------------------------------


def test_reads_a_top_level_field():
    assert api_field({"total": 7}, ("total",), SERVICE) == 7


def test_reads_a_nested_field():
    assert api_field({"meta": {"total": 7}}, ("meta", "total"), SERVICE) == 7


def test_missing_key_names_the_service_and_the_path():
    with pytest.raises(ApiResponseError) as caught:
        api_field({"meta": {}}, ("meta", "total"), SERVICE)
    message = str(caught.value)
    assert SERVICE in message
    assert "meta.total" in message
    assert "missing" in message


def test_non_object_along_the_path_says_what_it_found():
    with pytest.raises(ApiResponseError) as caught:
        api_field({"meta": [1, 2]}, ("meta", "total"), SERVICE)
    message = str(caught.value)
    assert "meta" in message
    assert "list" in message


def test_non_object_at_the_root_is_reported():
    with pytest.raises(ApiResponseError, match="<root>"):
        api_field(["not", "an", "object"], ("meta",), SERVICE)


def test_a_field_holding_none_is_returned_not_rejected():
    """None is a value the API sent. Only api_int and api_list judge the type."""
    assert api_field({"total": None}, ("total",), SERVICE) is None


# --- api_int -----------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [(7, 7), (7.9, 7), ("7", 7), (0, 0), ("-3", -3)])
def test_int_accepts_numbers_and_numeric_strings(raw, expected):
    assert api_int({"total": raw}, ("total",), SERVICE) == expected


@pytest.mark.parametrize("raw", [None, {"a": 1}, [1], True, False])
def test_int_rejects_non_numeric_types_by_name(raw):
    with pytest.raises(ApiResponseError, match="not a number"):
        api_int({"total": raw}, ("total",), SERVICE)


def test_int_rejects_a_non_numeric_string_showing_the_value():
    with pytest.raises(ApiResponseError) as caught:
        api_int({"total": "lots"}, ("total",), SERVICE)
    assert "lots" in str(caught.value)


def test_int_error_message_is_bounded():
    """The value is untrusted and reaches the log."""
    with pytest.raises(ApiResponseError) as caught:
        api_int({"total": "x" * 5000}, ("total",), SERVICE)
    assert len(str(caught.value)) < 200


@pytest.mark.parametrize("body", ['{"total": Infinity}', '{"total": -Infinity}', '{"total": 1e400}'])
def test_int_rejects_infinities_that_json_accepts(body):
    """json.loads accepts Infinity by default, and int() raises OverflowError on it."""
    import json

    with pytest.raises(ApiResponseError, match="not a number"):
        api_int(json.loads(body), ("total",), SERVICE)


def test_int_rejects_nan():
    import json

    with pytest.raises(ApiResponseError, match="not a number"):
        api_int(json.loads('{"total": NaN}'), ("total",), SERVICE)


def test_int_missing_field_still_names_the_path():
    with pytest.raises(ApiResponseError, match="missing"):
        api_int({}, ("meta", "total"), SERVICE)


# --- api_list ----------------------------------------------------------------


def test_list_returns_the_list():
    assert api_list({"data": [1, 2]}, ("data",), SERVICE) == [1, 2]


def test_list_accepts_an_empty_list():
    assert api_list({"data": []}, ("data",), SERVICE) == []


@pytest.mark.parametrize("raw", [None, {"a": 1}, "text", 3])
def test_list_rejects_other_types_by_name(raw):
    with pytest.raises(ApiResponseError, match="not a list"):
        api_list({"data": raw}, ("data",), SERVICE)


def test_api_response_error_is_a_value_error():
    """Callers already catching ValueError keep working."""
    assert issubclass(ApiResponseError, ValueError)
