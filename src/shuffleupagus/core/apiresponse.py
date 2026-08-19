"""Read fields out of a third-party API response, saying what was wrong.

Every music API here answers with JSON this project does not control. Indexing
straight into it turns a changed or truncated response into a bare KeyError,
TypeError, or ValueError raised three frames from the request, naming a
dictionary key and nothing else — not the service, not the endpoint, not what
was expected.

These helpers are the same idea as the AppleScript boundary guard in the Apple
Music service: check the shape once, at the edge, and name the value that broke
the expectation.
"""

from collections.abc import Sequence


class ApiResponseError(ValueError):
    """An API response did not have the shape this code requires."""


def _describe(service: str, path: Sequence[str]) -> str:
    return f"{service} response field {'.'.join(path)}"


def api_field(payload: object, path: Sequence[str], service: str) -> object:
    """Return payload[path[0]][path[1]]..., or raise naming the first missing step."""
    current = payload
    for depth, key in enumerate(path):
        if not isinstance(current, dict):
            walked = ".".join(path[:depth]) or "<root>"
            raise ApiResponseError(
                f"{_describe(service, path)} is unreadable: {walked} is {type(current).__name__}, not an object"
            )
        if key not in current:
            raise ApiResponseError(f"{_describe(service, path)} is missing")
        current = current[key]
    return current


def api_int(payload: object, path: Sequence[str], service: str) -> int:
    """Return a field coerced to int, or raise naming the field and what it held."""
    value = api_field(payload, path, service)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ApiResponseError(f"{_describe(service, path)} is not a number: {type(value).__name__}")
    try:
        return int(value)
    # json.loads accepts Infinity, -Infinity and NaN by default, and a float
    # literal too large to represent becomes inf. int() answers those with
    # OverflowError, which is not a ValueError.
    except (ValueError, OverflowError) as exc:
        raise ApiResponseError(f"{_describe(service, path)} is not a number: {value!r:.60}") from exc


def api_str(payload: object, path: Sequence[str], service: str) -> str:
    """Return a field that must already be a string.

    str() would happily turn a dict or a list into one, and the result then goes
    into a URL as though it were an identifier.
    """
    value = api_field(payload, path, service)
    if not isinstance(value, str):
        raise ApiResponseError(f"{_describe(service, path)} is not a string: {type(value).__name__}")
    return value


def api_object(value: object, what: str, service: str) -> dict:
    """Return a value that must be an object, naming what it was instead.

    Takes the value rather than a path, for checking an element of a list. There
    is deliberately no default: treating a wrong-typed entry as absent is how a
    malformed response becomes a confusing "not found" three retries later.
    """
    if not isinstance(value, dict):
        raise ApiResponseError(f"{service} response {what} is not an object: {type(value).__name__}")
    return value


def api_list(payload: object, path: Sequence[str], service: str) -> list:
    """Return a field that must be a list, or raise naming what it held instead."""
    value = api_field(payload, path, service)
    if not isinstance(value, list):
        raise ApiResponseError(f"{_describe(service, path)} is not a list: {type(value).__name__}")
    return value
