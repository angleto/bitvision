"""Agent-token hardening for the documents write surface.

Three write endpoints in ``backend/src/bvphoenix/api/documents.py``:
``merge_aliases``, ``split_alias``, ``ingest_document``. Each had only
``can_patient`` (human RBAC) — an agent token under a broadly-
privileged user could mutate documents on fascicoli outside its
``agent_patient_ids``. The fix adds ``enforce_agent_patient_scope``
after each ``can_patient``.

Stub-only: route functions called directly, no HTTP/DB.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import bvphoenix.api.documents as docs_module
from bvphoenix.api.documents import ingest_document, split_alias

_OWNER = uuid.uuid4()
_PATIENT_IN_SCOPE = uuid.uuid4()
_PATIENT_OUT_OF_SCOPE = uuid.uuid4()


@dataclass
class _StubPatient:
    id: uuid.UUID


@dataclass
class _StubDocument:
    id: uuid.UUID
    patient_id: uuid.UUID
    content_sha256: str = "abc"
    original_blob_hash: str = "abc"
    etag: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class _StubUser:
    subject_id: uuid.UUID = field(default_factory=lambda: _OWNER)
    is_admin: bool = False


class _StubScalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSessionForSplit:
    """Returns the document then the patient — pattern used by
    ``split_alias``."""

    def __init__(self, doc: _StubDocument | None, patient: _StubPatient | None) -> None:
        self._doc = doc
        self._patient = patient
        self._n = 0

    async def execute(self, _stmt: Any) -> Any:
        self._n += 1
        return _StubScalar(self._doc if self._n == 1 else self._patient)

    async def commit(self) -> None:  # split commits at the end of happy path
        return None


class _StubSessionForIngest:
    """``ingest_document`` first selects the patient, then later does
    other ops we monkeypatch away."""

    def __init__(self, patient: _StubPatient | None) -> None:
        self._patient = patient

    async def execute(self, _stmt: Any) -> Any:
        return _StubScalar(self._patient)


class _Audit:
    async def log(self, **_: Any) -> None:
        return None


def _request(*, is_agent: bool, allowed: set[uuid.UUID] | None) -> SimpleNamespace:
    state_kwargs: dict[str, Any] = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


@pytest.fixture(autouse=True)
def _patch_can_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(docs_module, "can_patient", _allow)


# --------------------------------------------------------------------- #
# split_alias                                                           #
# --------------------------------------------------------------------- #


async def test_split_alias_refuses_agent_outside_patient_scope() -> None:
    doc = _StubDocument(id=uuid.uuid4(), patient_id=_PATIENT_OUT_OF_SCOPE)
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionForSplit(doc, patient)

    with pytest.raises(HTTPException) as exc_info:
        await split_alias(
            document_id=doc.id,
            body=docs_module.SplitAliasIn(reason="test"),
            request=request,  # type: ignore[arg-type]
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    assert "not authorised" in exc_info.value.detail


# --------------------------------------------------------------------- #
# ingest_document                                                       #
# --------------------------------------------------------------------- #


async def test_ingest_document_refuses_agent_outside_patient_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient = _StubPatient(id=_PATIENT_OUT_OF_SCOPE)
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionForIngest(patient)

    # Block downstream side-effects so a leak past the gate is caught.
    # The ingest body lives in services/documents/ingest_blob since the
    # review-queue refactor — patch the symbols where they are used.
    from bvphoenix.services.documents import ingest_blob as ingest_blob_module

    monkeypatch.setattr(ingest_blob_module, "record_provenance_event", lambda *a, **kw: None)
    s3_stub = SimpleNamespace(upload_bytes=AsyncMock())
    monkeypatch.setattr(ingest_blob_module, "get_s3_storage", lambda: s3_stub)

    body = docs_module.IngestDocumentIn(
        patient_id=_PATIENT_OUT_OF_SCOPE,
        filename="test.txt",
        content_base64=base64.b64encode(b"hello").decode(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await ingest_document(
            body=body,
            request=request,  # type: ignore[arg-type]
            user=_StubUser(is_admin=True),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 403
    # Gate ran before the S3 upload — pin that no bytes leaked.
    s3_stub.upload_bytes.assert_not_awaited()


# --------------------------------------------------------------------- #
# Anti-enumeration: missing document returns 404 before agent gate      #
# --------------------------------------------------------------------- #


async def test_split_alias_missing_doc_returns_404_before_agent_gate() -> None:
    request = _request(is_agent=True, allowed={_PATIENT_IN_SCOPE})
    db = _StubSessionForSplit(None, None)

    with pytest.raises(HTTPException) as exc_info:
        await split_alias(
            document_id=uuid.uuid4(),
            body=docs_module.SplitAliasIn(reason="t"),
            request=request,  # type: ignore[arg-type]
            user=_StubUser(),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            audit=_Audit(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 404
