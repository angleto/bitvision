"""Tests for ``middleware/idempotency.py``.

Most of the contract lives in the request-hash function and the
context object; the DB-backed replay path is integration-tested via
the mutating endpoints once they opt in. Here we cover the pure
helpers and the conflict semantics that the dependency raises.
"""

from __future__ import annotations

from bvphoenix.middleware.idempotency import (
    _canonical_body,
    _parse_dry_run,
    compute_request_hash,
)


def test_canonical_body_orders_keys() -> None:
    a = b'{"b":1,"a":2}'
    b = b'{"a":2,"b":1}'
    assert _canonical_body(a) == _canonical_body(b)


def test_canonical_body_empty() -> None:
    assert _canonical_body(b"") == ""


def test_canonical_body_non_json_falls_back_to_raw() -> None:
    out = _canonical_body(b"not-json-at-all")
    assert out.startswith("raw:")


def test_compute_request_hash_stable() -> None:
    a = compute_request_hash("PATCH", "/x", b'{"a":1,"b":2}', dry_run=False)
    b = compute_request_hash("PATCH", "/x", b'{"b":2,"a":1}', dry_run=False)
    assert a == b


def test_compute_request_hash_dry_run_changes_hash() -> None:
    body = b'{"a":1}'
    a = compute_request_hash("PATCH", "/x", body, dry_run=False)
    b = compute_request_hash("PATCH", "/x", body, dry_run=True)
    assert a != b


def test_compute_request_hash_method_changes_hash() -> None:
    body = b'{"a":1}'
    a = compute_request_hash("PATCH", "/x", body, dry_run=False)
    b = compute_request_hash("POST", "/x", body, dry_run=False)
    assert a != b


def test_compute_request_hash_path_changes_hash() -> None:
    body = b'{"a":1}'
    a = compute_request_hash("PATCH", "/x", body, dry_run=False)
    b = compute_request_hash("PATCH", "/y", body, dry_run=False)
    assert a != b


class _Q:
    """Minimal stand-in for Starlette QueryParams.get."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self, _key: str) -> str | None:
        return self._value


class _Req:
    def __init__(self, dry_run_value: str | None) -> None:
        self.query_params = _Q(dry_run_value)


def test_parse_dry_run_truthy() -> None:
    for v in ("1", "true", "TRUE", "yes", "Y"):
        assert _parse_dry_run(_Req(v)) is True  # type: ignore[arg-type]


def test_parse_dry_run_falsy() -> None:
    for v in (None, "", "0", "false", "no"):
        assert _parse_dry_run(_Req(v)) is False  # type: ignore[arg-type]
