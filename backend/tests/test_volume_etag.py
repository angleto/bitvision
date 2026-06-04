"""ETag revalidation for cached volume blobs.

Regression for the mDIXON backfill foot-gun: stack 0 reuses the canonical
``volume.f32`` URL, so a 1 h opaque cache kept serving the stale (pre-fix,
interleaved) blob after a re-pack. The fix makes ``volume.raw`` a
revalidated cache entry keyed on the derivative row id (recreated on every
re-pack), so a new pack invalidates the stale client copy via
``If-None-Match`` -> 304.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from bvphoenix.api.studies._shared import (
    _derivative_etag,
    _not_modified_response,
    _volume_response,
)


def _req(if_none_match: str | None) -> SimpleNamespace:
    headers = {} if if_none_match is None else {"if-none-match": if_none_match}
    return SimpleNamespace(headers=headers)


def test_etag_is_stable_for_a_row_and_changes_on_repack() -> None:
    a = uuid.uuid4()
    b = uuid.uuid4()
    assert _derivative_etag(a) == _derivative_etag(a)  # same row -> same ETag
    assert _derivative_etag(a) != _derivative_etag(b)  # re-pack (new id) -> new ETag
    assert _derivative_etag(a) == f'"{a}"'


def test_not_modified_when_if_none_match_matches() -> None:
    etag = _derivative_etag(uuid.uuid4())
    resp = _not_modified_response(_req(etag), etag)
    assert resp is not None
    assert resp.status_code == 304
    assert resp.headers["etag"] == etag
    assert "must-revalidate" in resp.headers["cache-control"]


def test_not_modified_handles_comma_list() -> None:
    etag = _derivative_etag(uuid.uuid4())
    resp = _not_modified_response(_req(f'"other", {etag}'), etag)
    assert resp is not None and resp.status_code == 304


def test_serves_body_when_etag_differs_or_absent() -> None:
    etag = _derivative_etag(uuid.uuid4())
    assert _not_modified_response(_req(None), etag) is None
    assert _not_modified_response(_req('"stale"'), etag) is None


def test_volume_response_sets_revalidate_headers_with_etag() -> None:
    etag = _derivative_etag(uuid.uuid4())
    resp = _volume_response(b"x" * 64, accept_gzip=False, etag=etag)
    assert resp.headers["etag"] == etag
    assert resp.headers["cache-control"] == "private, max-age=0, must-revalidate"


def test_volume_response_legacy_cache_without_etag() -> None:
    resp = _volume_response(b"x" * 64, accept_gzip=False)
    assert "etag" not in resp.headers
    assert resp.headers["cache-control"] == "private, max-age=3600"
