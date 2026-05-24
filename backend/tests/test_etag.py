"""Pure-unit tests for ``api/_etag.py``.

These exercise the parsing / formatting helpers without standing up a
DB. The async ``etag_for_branch`` function is integration-tested via
mutating endpoints in their own modules.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from bvphoenix.services.etag import format_etag, parse_if_match, require_if_match


def test_format_etag_quotes_value() -> None:
    assert format_etag("deadbeef") == '"deadbeef"'


@pytest.mark.parametrize(
    "header,expected",
    [
        ('"abc"', "abc"),
        ('W/"abc"', "abc"),
        ("abc", "abc"),
        ("", None),
        (None, None),
        ("  ", None),
    ],
)
def test_parse_if_match_normalises(header: str | None, expected: str | None) -> None:
    assert parse_if_match(header) == expected


def test_parse_if_match_returns_wildcard_sentinel() -> None:
    """RFC 9110 §13.1.1: ``If-Match: *`` matches any current
    representation. ``parse_if_match`` returns the literal ``"*"``
    sentinel and ``require_if_match`` honours it as a bypass of the
    optimistic check, so an agent can opt out of concurrency control
    on a deliberately idempotent mutation. Until v3.0.0-beta.6 the
    parser raised 412 unconditionally — fine for a strict policy,
    but it broke the MCP ``update_document`` tool whose retries
    legitimately want the wildcard semantics."""
    assert parse_if_match("*") == "*"


def _request(headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "PATCH",
        "path": "/test",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


def test_require_if_match_missing_returns_428_when_etag_present() -> None:
    req = _request({})
    with pytest.raises(HTTPException) as exc:
        require_if_match(req, "abc")
    assert exc.value.status_code == 428


def test_require_if_match_mismatch_returns_412() -> None:
    req = _request({"if-match": '"stale"'})
    with pytest.raises(HTTPException) as exc:
        require_if_match(req, "fresh")
    assert exc.value.status_code == 412


def test_require_if_match_match_passes() -> None:
    req = _request({"if-match": '"fresh"'})
    require_if_match(req, "fresh")  # no exception


def test_require_if_match_skipped_when_no_current_etag() -> None:
    req = _request({})
    require_if_match(req, None)  # no exception, branch not yet created


def test_require_if_match_no_current_but_header_supplied_412() -> None:
    req = _request({"if-match": '"abc"'})
    with pytest.raises(HTTPException) as exc:
        require_if_match(req, None)
    assert exc.value.status_code == 412
