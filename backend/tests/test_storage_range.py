"""Range-aware GET path on :class:`S3Storage`.

The download endpoints emit ``Accept-Ranges: bytes`` and forward the
caller's ``Range`` header to S3 so dropped multi-GB downloads can be
resumed by the browser's download manager. The tests stub the boto3
client so we can assert:

* a ``Range: bytes=0-99`` request reaches S3 verbatim;
* the helper returns the ContentRange header coming back from S3 so
  the FastAPI handler can stamp it on the ``206 Partial Content``
  response;
* malformed / multi-range headers fall back to a full-body GET (no
  ``Range`` parameter on the boto3 call), so a buggy proxy can't
  ``416`` the user.
"""

from __future__ import annotations

import io
from typing import Any

from bvphoenix.storage.s3 import S3Storage


class _StubBody:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size if size and size > 0 else None)

    def close(self) -> None:
        self._buf.close()


class _StubBoto3Client:
    """Captures every ``get_object`` call and returns canned bytes."""

    def __init__(
        self,
        body: bytes,
        *,
        content_range: str | None = None,
        content_length: int | None = None,
        content_type: str | None = "application/zip",
    ) -> None:
        self.body = body
        self.content_range = content_range
        self.content_length = content_length if content_length is not None else len(body)
        self.content_type = content_type
        self.calls: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        resp: dict[str, Any] = {
            "Body": _StubBody(self.body),
            "ContentLength": self.content_length,
            "ContentType": self.content_type,
        }
        if self.content_range is not None:
            resp["ContentRange"] = self.content_range
        return resp


def _make_storage(stub: _StubBoto3Client) -> S3Storage:
    storage = S3Storage.__new__(S3Storage)
    storage._client = stub  # type: ignore[attr-defined]
    return storage


def test_no_range_header_does_not_set_range_param() -> None:
    stub = _StubBoto3Client(b"abcdefghij")
    storage = _make_storage(stub)
    body, returned, total, ctype, content_range = storage.iter_object_with_range(
        bucket="b",
        key="k",
        range_header=None,
    )
    assert b"".join(body) == b"abcdefghij"
    assert returned == 10
    assert total == 10
    assert ctype == "application/zip"
    assert content_range is None
    assert stub.calls == [{"Bucket": "b", "Key": "k"}]


def test_range_header_forwarded_and_returns_total_from_content_range() -> None:
    stub = _StubBoto3Client(
        b"01234",
        content_range="bytes 0-4/100",
        content_length=5,
    )
    storage = _make_storage(stub)
    body, returned, total, _ctype, content_range = storage.iter_object_with_range(
        bucket="b",
        key="k",
        range_header="bytes=0-4",
    )
    assert b"".join(body) == b"01234"
    assert returned == 5
    assert total == 100
    assert content_range == "bytes 0-4/100"
    assert stub.calls == [{"Bucket": "b", "Key": "k", "Range": "bytes=0-4"}]


def test_multi_range_header_falls_back_to_full_body() -> None:
    stub = _StubBoto3Client(b"abcdefghij")
    storage = _make_storage(stub)
    _body, _returned, _total, _ctype, content_range = storage.iter_object_with_range(
        bucket="b",
        key="k",
        range_header="bytes=0-3,8-9",
    )
    assert content_range is None
    assert "Range" not in stub.calls[0]


def test_malformed_range_header_falls_back_to_full_body() -> None:
    stub = _StubBoto3Client(b"abcdefghij")
    storage = _make_storage(stub)
    _body, _returned, _total, _ctype, content_range = storage.iter_object_with_range(
        bucket="b",
        key="k",
        range_header="not-a-range",
    )
    assert content_range is None
    assert "Range" not in stub.calls[0]
