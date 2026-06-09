"""Regression: the embedded Q&A assistant can read + complete a
document's metadata (get_document / update_document).

The assistant was read-only, so "complete the metadata of this document"
failed with "I can't access / modify documents". These executors reuse
the same ``apply_bulk_update`` service as the HTTP PATCH (catalog
validation + versioning + audit + etag) and attribute the write with
``author_kind='agent'`` so AI edits stay visible in the revision history.

Needs a migrated Postgres; point BVP_DATABASE_URL at bvphoenix_test.
The owner subject is created inline (not via make_user) because the
write commits and we don't want make_user's teardown to delete subjects
still referenced by the committed document/commit rows.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Document, Folder, FolderItem, Patient, User
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.qna_tools import build_executors
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


class _State:
    is_agent = False
    share_link_id = None
    agent_token_id = None
    agent_assistant_id = None
    agent_token = None


class _Req:
    """Minimal stand-in for FastAPI Request: only ``.state`` is read."""

    state = _State()


async def test_assistant_reads_and_completes_document_metadata(
    db_session: AsyncSession,
) -> None:
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name="qna-writer"))
    await db_session.flush()
    db_session.add(
        User(subject_id=sid, email=f"qnaw-{uuid.uuid4().hex[:8]}@test.local", password_hash="x")
    )
    await db_session.flush()
    owner = (await db_session.execute(select(User).where(User.subject_id == sid))).scalar_one()

    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=sid,
        display_name="QnA write patient",
    )
    db_session.add(patient)
    await db_session.flush()

    doc = Document(
        id=uuid.uuid4(),
        patient_id=patient.id,
        uploaded_by_subject_id=sid,
        kind_id="unclassified",
        provenance_id="digital_native_pdf",
        authority_id="original",
        title="referto.pdf",
    )
    db_session.add(doc)
    await db_session.flush()
    # Documents must live in >=1 folder (deferred no-orphan constraint),
    # so anchor it before the commit.
    folder = Folder(
        id=uuid.uuid4(),
        name="Documenti",
        owner_subject_id=sid,
        patient_id=patient.id,
        parent_folder_id=None,
    )
    db_session.add(folder)
    await db_session.flush()
    db_session.add(FolderItem(folder_id=folder.id, resource_kind="document", resource_id=doc.id))
    await db_session.flush()
    await db_session.commit()  # real server-generated etag

    execs = build_executors(db=db_session, patient_id=patient.id, user=owner, request=_Req())

    # get_document: full metadata by id, scoped to the patient.
    meta = json.loads(await execs["get_document"]({"document_id": str(doc.id)}))
    assert meta["kind_id"] == "unclassified"
    assert meta["title"] == "referto.pdf"
    assert meta["etag"]

    # dry_run preview: no mutation, returns a diff.
    dry = json.loads(
        await execs["update_document"](
            {
                "document_id": str(doc.id),
                "kind_id": "lab_result",
                "title": "Esami del sangue 05/06/2026",
                "dry_run": True,
            }
        )
    )
    assert dry["dry_run"] is True
    assert dry["status"] == "dry_run"
    fresh = (await db_session.execute(select(Document).where(Document.id == doc.id))).scalar_one()
    await db_session.refresh(fresh)
    assert fresh.kind_id == "unclassified"  # dry run did NOT persist

    # apply for real.
    res = json.loads(
        await execs["update_document"](
            {
                "document_id": str(doc.id),
                "kind_id": "lab_result",
                "title": "Esami del sangue 05/06/2026",
                "document_date": "2026-06-05",
            }
        )
    )
    assert res["status"] == "ok", res

    updated = (await db_session.execute(select(Document).where(Document.id == doc.id))).scalar_one()
    await db_session.refresh(updated)
    assert updated.kind_id == "lab_result"
    assert updated.title == "Esami del sangue 05/06/2026"
    assert updated.document_date is not None and updated.document_date.isoformat() == "2026-06-05"

    # The write must be recorded with AI provenance (author_kind='agent').
    from sqlalchemy import text as _text

    author_kind = (
        await db_session.execute(
            _text(
                "SELECT author_kind FROM commits WHERE author_subject_id = :sid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"sid": sid},
        )
    ).scalar()
    assert author_kind == "agent"


async def test_assistant_update_refuses_without_user(db_session: AsyncSession) -> None:
    # Read-only context (no user/request) must NOT allow writes.
    patient_id = uuid.uuid4()
    execs = build_executors(db=db_session, patient_id=patient_id)
    out = json.loads(
        await execs["update_document"]({"document_id": str(uuid.uuid4()), "title": "x"})
    )
    assert "error" in out
    await db_session.rollback()
