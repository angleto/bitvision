"""Unit tests for the resumable upload session service.

The recoverability core is ``append_chunk``: it must be idempotent on the byte
offset (a re-sent chunk after a reconnect is a no-op), reject a gap with 409 so
the client resyncs, and complete the file's S3 multipart upload exactly when
the declared size is reached. We exercise it against in-memory ORM rows + a
stub DB/S3 so the test stays fast and DB-free.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from bvphoenix.db.models import UploadSession, UploadSessionFile
from bvphoenix.services import upload_sessions as svc
from bvphoenix.storage.s3 import S3Storage

CHUNK = svc.CHUNK_SIZE


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_chunk_size_is_a_legal_s3_part() -> None:
    # Every part except the last must be >= 5 MiB; the offset->PartNumber math
    # needs an exact power-of-two divisor.
    assert CHUNK >= 5 * 1024 * 1024
    assert CHUNK & (CHUNK - 1) == 0


def test_complete_multipart_projects_to_s3_keys_only() -> None:
    """Regression: append_chunk persists ``{PartNumber, ETag, size}`` per part,
    but S3's complete_multipart_upload rejects any key other than PartNumber /
    ETag (``ParamValidationError: Unknown parameter ... "size"``). The storage
    layer must project to exactly those two keys (and sort by PartNumber)
    before calling boto3 — the prod 500 that this guards against."""
    storage = object.__new__(S3Storage)
    storage._client = MagicMock()
    storage.complete_multipart(
        bucket="b",
        key="k",
        upload_id="u",
        # Out of order + carrying the bookkeeping ``size`` key.
        parts=[
            {"PartNumber": 2, "ETag": '"e2"', "size": 100},
            {"PartNumber": 1, "ETag": '"e1"', "size": CHUNK},
        ],
    )
    sent = storage._client.complete_multipart_upload.call_args.kwargs["MultipartUpload"]["Parts"]
    assert sent == [{"PartNumber": 1, "ETag": '"e1"'}, {"PartNumber": 2, "ETag": '"e2"'}]
    assert all(set(p) == {"PartNumber", "ETag"} for p in sent)


def test_staged_key_layout() -> None:
    sid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert svc._staging_prefix(sid) == f"_ingest_jobs/{sid}/"
    assert svc._file_key(sid, 7) == f"_ingest_jobs/{sid}/7.bin"


def test_canonical_input_matches_legacy_shape_and_skips_empty() -> None:
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    session = UploadSession(
        id=sid,
        owner_subject_id=uuid.uuid4(),
        patient_id=pid,
        folder_id=None,
        tier="t1",
        wrap_iso_in_folder=True,
        status="ready",
        declared_total_bytes=10,
        received_total_bytes=10,
    )
    files = [
        UploadSessionFile(
            session_id=sid,
            file_index=0,
            filename="a.dcm",
            relative_path="CD/a.dcm",
            declared_size=10,
            s3_key=svc._file_key(sid, 0),
            status="staged",
        ),
        UploadSessionFile(
            session_id=sid,
            file_index=1,
            filename="empty.txt",
            relative_path=None,
            declared_size=0,  # excluded from the manifest
            s3_key=svc._file_key(sid, 1),
            status="staged",
        ),
    ]
    ci = svc._build_canonical_input(session, files)
    assert ci["manifest"] == [
        {"filename": "a.dcm", "relative_path": "CD/a.dcm", "s3_key": svc._file_key(sid, 0)}
    ]
    assert ci["patient_id"] == str(pid)
    assert ci["tier"] == "t1"
    assert ci["staging_prefix"] == svc._staging_prefix(sid)
    # The keys the unchanged worker reads.
    for k in ("manifest", "owner_subject_id", "folder_id", "stage_skipped", "iso_archives"):
        assert k in ci


# ---------------------------------------------------------------------------
# append_chunk — the resumable core
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value: object) -> None:
        self._v = value

    def scalar_one_or_none(self) -> object:
        return self._v

    def scalar_one(self) -> object:
        return self._v


class _StubDB:
    def __init__(self, file_row: UploadSessionFile, remaining: int = 0) -> None:
        self.file_row = file_row
        self.remaining = remaining
        self.commits = 0

    async def execute(self, stmt, *a, **k):
        if "count(" in str(stmt).lower():
            return _Result(self.remaining)
        return _Result(self.file_row)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        pass


class _StubStorage:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.parts: list[tuple[int, int]] = []
        self.completed: list[str] = []
        self.aborted: list[str] = []

    def create_multipart(self, *, bucket: str, key: str) -> str:
        self.created.append(key)
        return "UPLOAD-ID"

    def upload_part(self, *, bucket: str, key: str, upload_id: str, part_number: int, body: bytes):
        self.parts.append((part_number, len(body)))
        return f'"etag-{part_number}"'

    def complete_multipart(self, *, bucket: str, key: str, upload_id: str, parts: list) -> None:
        self.completed.append(key)


def _session() -> UploadSession:
    return UploadSession(
        id=uuid.uuid4(),
        owner_subject_id=uuid.uuid4(),
        tier="t1",
        status="uploading",
        declared_total_bytes=CHUNK + 100,
        received_total_bytes=0,
    )


def _file(session: UploadSession, declared: int) -> UploadSessionFile:
    f = UploadSessionFile(
        session_id=session.id,
        file_index=0,
        filename="a.dcm",
        relative_path=None,
        declared_size=declared,
        s3_key=svc._file_key(session.id, 0),
        s3_upload_id=None,
        received_offset=0,
        status="pending",
    )
    f.parts = []
    return f


@pytest.mark.asyncio
async def test_first_chunk_opens_multipart_and_advances_offset() -> None:
    s = _session()
    f = _file(s, CHUNK + 100)
    db = _StubDB(f)
    storage = _StubStorage()
    await svc.append_chunk(
        db, storage, bucket="b", session=s, file_index=0, offset=0, body=b"x" * CHUNK
    )
    assert storage.created == [f.s3_key]
    assert storage.parts == [(1, CHUNK)]
    assert f.received_offset == CHUNK
    assert f.status == "uploading"
    assert s.received_total_bytes == CHUNK


@pytest.mark.asyncio
async def test_resent_chunk_is_idempotent_noop() -> None:
    s = _session()
    f = _file(s, CHUNK + 100)
    f.received_offset = CHUNK  # already acked the first chunk
    f.s3_upload_id = "UPLOAD-ID"
    f.status = "uploading"
    db = _StubDB(f)
    storage = _StubStorage()
    # Client re-sends the first chunk after a reconnect.
    await svc.append_chunk(
        db, storage, bucket="b", session=s, file_index=0, offset=0, body=b"x" * CHUNK
    )
    assert storage.parts == []  # nothing re-uploaded
    assert f.received_offset == CHUNK


@pytest.mark.asyncio
async def test_offset_gap_returns_409_with_expected_offset() -> None:
    s = _session()
    f = _file(s, CHUNK + 100)
    f.received_offset = CHUNK
    f.s3_upload_id = "UPLOAD-ID"
    db = _StubDB(f)
    storage = _StubStorage()
    with pytest.raises(HTTPException) as ei:
        await svc.append_chunk(
            db, storage, bucket="b", session=s, file_index=0, offset=CHUNK * 2, body=b"x" * CHUNK
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "offset_mismatch"
    assert ei.value.detail["expected_offset"] == CHUNK


@pytest.mark.asyncio
async def test_final_chunk_completes_and_stages() -> None:
    s = _session()
    f = _file(s, CHUNK + 100)
    f.received_offset = CHUNK
    f.s3_upload_id = "UPLOAD-ID"
    f.parts = [{"PartNumber": 1, "ETag": '"e1"', "size": CHUNK}]
    db = _StubDB(f, remaining=0)
    storage = _StubStorage()
    await svc.append_chunk(
        db, storage, bucket="b", session=s, file_index=0, offset=CHUNK, body=b"y" * 100
    )
    assert storage.parts == [(2, 100)]  # PartNumber from offset
    assert storage.completed == [f.s3_key]
    assert f.status == "staged"
    assert s.status == "ready"  # last file staged -> session ready to commit


@pytest.mark.asyncio
async def test_non_final_chunk_must_be_exactly_chunk_size() -> None:
    s = _session()
    f = _file(s, CHUNK * 2)  # two full chunks expected
    db = _StubDB(f)
    storage = _StubStorage()
    with pytest.raises(HTTPException) as ei:
        await svc.append_chunk(
            db, storage, bucket="b", session=s, file_index=0, offset=0, body=b"z" * 100
        )
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "bad_chunk_size"


@pytest.mark.asyncio
async def test_chunk_overflowing_declared_size_is_rejected() -> None:
    s = _session()
    f = _file(s, 100)  # tiny file
    db = _StubDB(f)
    storage = _StubStorage()
    with pytest.raises(HTTPException) as ei:
        await svc.append_chunk(
            db, storage, bucket="b", session=s, file_index=0, offset=0, body=b"z" * 200
        )
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "chunk_overflows_declared_size"
