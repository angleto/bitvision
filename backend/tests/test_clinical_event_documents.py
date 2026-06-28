"""Service-level tests for event ↔ drive Document reconciliation.

Drives the helpers in ``services/clinical_event_documents`` against a
real DB (no HTTP stack, no S3): reconciliation keys off the stored
``content_sha256`` and the link helpers operate on existing Document
rows, so the whole flow the user cares about — "is this attachment
already in the Drive? then point the event at it" — is exercisable
without object storage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    ClinicalEvent,
    ClinicalEventAttachment,
    ClinicalEventDocument,
    Document,
    Patient,
)
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.clinical_event_documents import (
    EventDocumentLinkError,
    find_patient_document_by_hash,
    get_or_create_event_document_link,
    promote_attachment,
    reconcile_attachment,
    soft_delete_event_document_link,
)
from bvphoenix.services.review_queue.actor import ReviewActor

from .conftest import skip_if_no_db

pytestmark = skip_if_no_db


async def _patient_and_event(db: AsyncSession) -> tuple[Patient, ClinicalEvent]:
    sid = uuid.uuid4()
    db.add(Subject(id=sid, kind="user", display_name=f"sub-{sid}"))
    await db.flush()
    patient = Patient(id=uuid.uuid4(), managed_by_subject_id=sid, display_name="P")
    db.add(patient)
    await db.flush()
    ev = ClinicalEvent(
        patient_id=patient.id,
        kind="outpatient_visit",
        title="visit",
        event_status="planned",
        planned_start_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        timezone="Europe/Rome",
    )
    db.add(ev)
    await db.flush()
    await db.refresh(ev)
    return patient, ev


async def _document(
    db: AsyncSession,
    patient: Patient,
    *,
    sha: str | None = None,
    original: str | None = None,
    title: str = "referto.pdf",
    created_at: datetime | None = None,
) -> Document:
    doc = Document(
        id=uuid.uuid4(),
        patient_id=patient.id,
        kind_id="unclassified",
        provenance_id="manual_entry",
        authority_id="original",
        title=title,
        content_sha256=sha,
        original_blob_hash=original if original is not None else sha,
        file_s3_key=f"raw/{uuid.uuid4()}.pdf",
        file_content_type="application/pdf",
    )
    if created_at is not None:
        doc.created_at = created_at
    db.add(doc)
    await db.flush()
    return doc


async def _attachment(
    db: AsyncSession, ev: ClinicalEvent, *, sha: str | None, filename: str = "up.pdf"
) -> ClinicalEventAttachment:
    att = ClinicalEventAttachment(
        id=uuid.uuid4(),
        event_id=ev.id,
        patient_id=ev.patient_id,
        filename=filename,
        mime="application/pdf",
        size_bytes=10,
        storage_key=f"clinical_event_attachments/{ev.patient_id}/{ev.id}/{uuid.uuid4()}/{filename}",
        content_sha256=sha,
        uploaded_by_kind="human",
    )
    db.add(att)
    await db.flush()
    return att


def _actor(patient: Patient) -> ReviewActor:
    return ReviewActor(kind="human", subject_id=patient.managed_by_subject_id)


async def _live_link_count(db: AsyncSession, event_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(ClinicalEventDocument)
            .where(
                ClinicalEventDocument.event_id == event_id,
                ClinicalEventDocument.deleted_at.is_(None),
            )
        )
    ).scalar_one()


@skip_if_no_db
async def test_reconcile_links_to_matching_document(db_session: AsyncSession) -> None:
    patient, ev = await _patient_and_event(db_session)
    sha = "a" * 64
    doc = await _document(db_session, patient, sha=sha)
    att = await _attachment(db_session, ev, sha=sha)

    link = await reconcile_attachment(db_session, event=ev, attachment=att)

    assert link is not None
    assert str(link.document_id) == str(doc.id)
    assert str(link.source_attachment_id) == str(att.id)
    assert link.link_role == "attachment"
    assert link.created_by_kind == "system"  # auto-detected, not a human act


@skip_if_no_db
async def test_reconcile_no_match_returns_none(db_session: AsyncSession) -> None:
    patient, ev = await _patient_and_event(db_session)
    await _document(db_session, patient, sha="a" * 64)
    att = await _attachment(db_session, ev, sha="b" * 64)

    assert await reconcile_attachment(db_session, event=ev, attachment=att) is None
    assert await _live_link_count(db_session, ev.id) == 0


@skip_if_no_db
async def test_reconcile_is_idempotent(db_session: AsyncSession) -> None:
    patient, ev = await _patient_and_event(db_session)
    sha = "c" * 64
    await _document(db_session, patient, sha=sha)
    att = await _attachment(db_session, ev, sha=sha)

    first = await reconcile_attachment(db_session, event=ev, attachment=att)
    second = await reconcile_attachment(db_session, event=ev, attachment=att)

    assert first is not None and second is not None
    assert str(first.id) == str(second.id)
    assert await _live_link_count(db_session, ev.id) == 1


@skip_if_no_db
async def test_link_cross_patient_raises(db_session: AsyncSession) -> None:
    patient_a, event_a = await _patient_and_event(db_session)
    patient_b, _ = await _patient_and_event(db_session)
    doc_b = await _document(db_session, patient_b, sha="d" * 64)

    with pytest.raises(EventDocumentLinkError):
        await get_or_create_event_document_link(
            db_session, event=event_a, document=doc_b, actor=_actor(patient_a)
        )


@skip_if_no_db
async def test_pure_reference_then_reconcile_enriches_source(db_session: AsyncSession) -> None:
    patient, ev = await _patient_and_event(db_session)
    sha = "e" * 64
    doc = await _document(db_session, patient, sha=sha)

    link1, created1 = await get_or_create_event_document_link(
        db_session, event=ev, document=doc, actor=_actor(patient), link_role="reference"
    )
    assert created1 is True
    assert link1.source_attachment_id is None

    att = await _attachment(db_session, ev, sha=sha)
    link2 = await reconcile_attachment(db_session, event=ev, attachment=att)

    assert link2 is not None
    assert str(link2.id) == str(link1.id)  # same row, enriched in place
    assert str(link2.source_attachment_id) == str(att.id)
    assert await _live_link_count(db_session, ev.id) == 1  # no duplicate


@skip_if_no_db
async def test_soft_delete_link_idempotent_and_relinkable(db_session: AsyncSession) -> None:
    patient, ev = await _patient_and_event(db_session)
    doc = await _document(db_session, patient, sha="f" * 64)
    link, _ = await get_or_create_event_document_link(
        db_session, event=ev, document=doc, actor=_actor(patient)
    )

    assert await soft_delete_event_document_link(
        db_session, event=ev, link_id=link.id, actor=_actor(patient)
    )
    # Second delete: nothing live to remove.
    assert not await soft_delete_event_document_link(
        db_session, event=ev, link_id=link.id, actor=_actor(patient)
    )
    # Partial-unique is over live rows only, so the pair can be re-linked.
    link2, created2 = await get_or_create_event_document_link(
        db_session, event=ev, document=doc, actor=_actor(patient)
    )
    assert created2 is True
    assert str(link2.id) != str(link.id)
    assert await _live_link_count(db_session, ev.id) == 1


@skip_if_no_db
async def test_promote_reconcile_path_makes_no_new_document(db_session: AsyncSession) -> None:
    """When the bytes are already curated, promote links instead of
    ingesting — and never touches S3 (the attachment already has its
    hash, so no blob fetch happens)."""
    patient, ev = await _patient_and_event(db_session)
    sha = "1" * 64
    doc = await _document(db_session, patient, sha=sha)
    att = await _attachment(db_session, ev, sha=sha)

    before = (
        await db_session.execute(
            select(func.count()).select_from(Document).where(Document.patient_id == patient.id)
        )
    ).scalar_one()

    link, reconciled = await promote_attachment(
        db_session,
        event=ev,
        attachment=att,
        actor=_actor(patient),
        uploaded_by_subject_id=patient.managed_by_subject_id,
    )

    after = (
        await db_session.execute(
            select(func.count()).select_from(Document).where(Document.patient_id == patient.id)
        )
    ).scalar_one()

    assert reconciled is True
    assert str(link.document_id) == str(doc.id)
    assert after == before  # no second copy materialised


@skip_if_no_db
async def test_find_by_hash_matches_content_or_original_oldest_first(
    db_session: AsyncSession,
) -> None:
    patient, _ = await _patient_and_event(db_session)
    sha = "2" * 64
    older = await _document(
        db_session, patient, sha=sha, title="older", created_at=datetime(2024, 1, 1, tzinfo=UTC)
    )
    # Newer row matches only on original_blob_hash (different stored bytes).
    await _document(
        db_session,
        patient,
        sha="3" * 64,
        original=sha,
        title="newer",
        created_at=datetime(2024, 6, 1, tzinfo=UTC),
    )

    found = await find_patient_document_by_hash(db_session, patient_id=patient.id, sha256=sha)
    assert found is not None
    assert str(found.id) == str(older.id)  # oldest-first wins

    # A hash present only as the originating-artefact anchor still resolves.
    only_original = "9" * 64
    derived = await _document(db_session, patient, sha="4" * 64, original=only_original)
    hit = await find_patient_document_by_hash(
        db_session, patient_id=patient.id, sha256=only_original
    )
    assert hit is not None
    assert str(hit.id) == str(derived.id)
