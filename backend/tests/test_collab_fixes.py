"""Regression tests for the collaborative-mode fixes (2026-06-08).

Covers the four defects found while debugging why a delegate saw
"patient not found" and could not upload:

* BUG#3 — bulk ingest of a non-DICOM document WITHOUT a chosen folder
  must anchor it in the patient's root folder, or the deferred
  ``document_orphan_forbidden`` trigger fails the commit (the original
  "session is in 'prepared' state" crash).
* BUG#1 — sharing a patient with an email that already has an account
  must bind the grant to that real subject (not PUBLIC_SUBJECT_ID);
  ``mode='claim'`` links must be claimable/bindable; ``/bind`` must
  repoint a PUBLIC-held grant to the logged-in recipient.
* BUG#4 — folder-access for upload must honour a patient-level grant
  (``can_access_folder`` cascade), not just folder ownership.

These are integration tests: they need a migrated Postgres (the
``enforce_document_in_folder`` constraint trigger is the whole point of
BUG#3). Point BVP_DATABASE_URL at a migrated DB (e.g. bvphoenix_test).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Folder,
    FolderItem,
    Grant,
    Patient,
    PatientContact,
    ShareLink,
    User,
)
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.services import bulk_ingest as bi
from bvphoenix.services.permissions import can_access_folder
from tests.conftest import client_as, public_client, skip_if_no_db

pytestmark = skip_if_no_db


class _StubStorage:
    """In-memory S3 stand-in for the bulk-ingest path."""

    def __init__(self, blobs: dict[tuple[str, str], bytes] | None = None) -> None:
        self.blobs = blobs or {}

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        return self.blobs[(bucket, key)]

    def upload_bytes(self, data, *, bucket: str, key: str):
        return None

    def copy_object(self, **kwargs):
        return None

    def iter_object(self, *, bucket: str, key: str, chunk_size: int = 8192):
        data = self.blobs[(bucket, key)]
        return (iter([data]), len(data), None)

    def delete_object(self, *, bucket: str, key: str) -> None:
        return None


async def _cleanup_patient(db: AsyncSession, patient_id: uuid.UUID) -> None:
    """Hard-delete everything filed under a patient, FK- and trigger-safe.

    documents + folder_items go in one statement-group committed together
    so the deferred no-orphan trigger sees the document rows already gone
    (no live document with zero containment) at commit time.
    """
    await db.rollback()
    for stmt in (
        "DELETE FROM folder_items WHERE folder_id IN "
        "(SELECT id FROM folders WHERE patient_id = :p) "
        "OR resource_id IN (SELECT id FROM documents WHERE patient_id = :p) "
        "OR resource_id = :p",
        "DELETE FROM documents WHERE patient_id = :p",
        "DELETE FROM folders WHERE patient_id = :p",
        "DELETE FROM clinical_events WHERE patient_id = :p",
        "DELETE FROM share_links WHERE grant_id IN (SELECT id FROM grants WHERE resource_id = :p)",
        "DELETE FROM grants WHERE resource_id = :p",
        "DELETE FROM patient_contacts WHERE patient_id = :p",
        "DELETE FROM patients WHERE id = :p",
    ):
        await db.execute(text(stmt), {"p": patient_id})
    await db.commit()


# --------------------------------------------------------------------------
# BUG#3 — bulk ingest anchors documents even without a chosen folder
# --------------------------------------------------------------------------


async def test_bulk_ingest_document_without_folder_anchored_to_root(
    db_session: AsyncSession, make_user, monkeypatch
) -> None:
    owner = await make_user(email=f"bulk-{uuid.uuid4().hex[:8]}@test.local")
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Bulk ingest patient",
    )
    db_session.add(patient)
    await db_session.flush()
    await db_session.commit()

    settings = get_settings()
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    key = f"_ingest_jobs/{uuid.uuid4()}/report.pdf"
    stub = _StubStorage({(settings.s3_bucket_raw, key): pdf})
    monkeypatch.setattr("bvphoenix.services.bulk_ingest.get_s3_storage", lambda: stub)

    staged = [bi.StagedFile(relative_path="report.pdf", filename="report.pdf", s3_key=key)]
    try:
        # Pre-fix this commit raised CheckViolationError(document_orphan_forbidden).
        summary = await bi.process_bulk_ingest(
            db_session,
            staged_files=staged,
            owner_subject_id=owner.subject_id,
            patient_id=patient.id,
            folder_id=None,
            tier="t1",
        )
        assert len(summary.documents_created) == 1
        doc_id = uuid.UUID(summary.documents_created[0].id)
        items = (
            (
                await db_session.execute(
                    select(FolderItem).where(
                        FolderItem.resource_kind == "document",
                        FolderItem.resource_id == doc_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        # Anchored in exactly one folder (the patient root) → no-orphan satisfied.
        assert len(items) == 1
    finally:
        await _cleanup_patient(db_session, patient.id)


# --------------------------------------------------------------------------
# BUG#1 — sharing binds to a real account; claim/bind for mode='claim'
# --------------------------------------------------------------------------


async def test_patient_share_to_existing_account_binds_real_subject(
    db_session: AsyncSession, make_user
) -> None:
    owner = await make_user(email=f"shareowner-{uuid.uuid4().hex[:8]}@test.local")
    recip_email = f"sharerecip-{uuid.uuid4().hex[:8]}@test.local"
    recipient = await make_user(email=recip_email)
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Share patient",
    )
    db_session.add(patient)
    await db_session.flush()
    await db_session.commit()

    try:
        async with client_as(db_session, owner) as client:
            r = await client.post(
                f"/api/patients/{patient.id}/share",
                json={
                    "access_level": "editor",
                    "download": True,
                    "target": {"kind": "link_public"},
                    "recipient_email": recip_email,
                    "mode": "claim",
                },
            )
            assert r.status_code == 201, r.text
            grants = (
                (
                    await db_session.execute(
                        select(Grant).where(
                            Grant.resource_kind == "patient", Grant.resource_id == patient.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(grants) == 1
            # Bound to the real account, not the public sentinel.
            assert grants[0].grantee_subject_id == recipient.subject_id
            # editor level carries write:report → the recipient can upload.
            assert "write:report" in grants[0].permissions
    finally:
        await _cleanup_patient(db_session, patient.id)


async def test_share_info_claimable_then_bindable_for_claim_mode(
    db_session: AsyncSession, make_user
) -> None:
    owner = await make_user(email=f"delegowner-{uuid.uuid4().hex[:8]}@test.local")
    recip_email = f"delegrecip-{uuid.uuid4().hex[:8]}@test.local"
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Delegation patient",
    )
    db_session.add(patient)
    await db_session.flush()
    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=owner.subject_id,
        grantee_subject_id=PUBLIC_SUBJECT_ID,
        permissions=["read:metadata", "read:pixels"],
        valid_from=datetime.now(UTC),
        conditions={"scope": "delegation"},
    )
    db_session.add(grant)
    await db_session.flush()
    token = f"tok-{uuid.uuid4().hex}"
    link = ShareLink(
        grant_id=grant.id,
        token=token,
        mode="claim",
        recipient_email=recip_email,
        recipient_name="Delegate",
    )
    db_session.add(link)
    await db_session.flush()
    await db_session.commit()

    try:
        # No account for recipient yet → claimable (create account), not bindable.
        async with public_client(db_session) as pub:
            r = await pub.get(f"/api/shared/{token}/info")
            assert r.status_code == 200, r.text
            info = r.json()
            assert info["claimable"] is True
            assert info["bindable"] is False

        # Recipient registers separately → now bindable (log in + /bind), not claimable.
        await make_user(email=recip_email)
        async with public_client(db_session) as pub:
            r = await pub.get(f"/api/shared/{token}/info")
            assert r.status_code == 200, r.text
            info = r.json()
            assert info["claimable"] is False
            assert info["bindable"] is True
    finally:
        await _cleanup_patient(db_session, patient.id)


async def test_bind_repoints_public_grant_to_logged_in_recipient(
    db_session: AsyncSession, make_user
) -> None:
    owner = await make_user(email=f"bindowner-{uuid.uuid4().hex[:8]}@test.local")
    recip_email = f"bindrecip-{uuid.uuid4().hex[:8]}@test.local"
    recipient = await make_user(email=recip_email)
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Bind patient",
    )
    db_session.add(patient)
    await db_session.flush()
    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=owner.subject_id,
        grantee_subject_id=PUBLIC_SUBJECT_ID,
        permissions=["read:metadata", "write:report"],
        valid_from=datetime.now(UTC),
        conditions={"scope": "delegation"},
    )
    db_session.add(grant)
    await db_session.flush()
    token = f"tok-{uuid.uuid4().hex}"
    link = ShareLink(grant_id=grant.id, token=token, mode="claim", recipient_email=recip_email)
    db_session.add(link)
    await db_session.flush()
    await db_session.commit()

    try:
        async with client_as(db_session, recipient) as client:
            r = await client.post(f"/api/share-links/{token}/bind")
            assert r.status_code == 200, r.text
            refreshed = (
                await db_session.execute(select(Grant).where(Grant.id == grant.id))
            ).scalar_one()
            assert refreshed.grantee_subject_id == recipient.subject_id
    finally:
        await _cleanup_patient(db_session, patient.id)


# --------------------------------------------------------------------------
# BUG#4 — folder access honours patient-level grants, not just ownership
# --------------------------------------------------------------------------


async def test_can_access_folder_via_patient_grant(db_session: AsyncSession, make_user) -> None:
    owner = await make_user(email=f"folderowner-{uuid.uuid4().hex[:8]}@test.local")
    collab = await make_user(email=f"foldercollab-{uuid.uuid4().hex[:8]}@test.local")
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Folder patient",
    )
    db_session.add(patient)
    await db_session.flush()
    folder = Folder(
        id=uuid.uuid4(),
        name="Imaging",
        owner_subject_id=owner.subject_id,
        patient_id=patient.id,
        parent_folder_id=None,
    )
    db_session.add(folder)
    await db_session.flush()

    # No grant yet: a non-owner collaborator cannot reach the folder.
    assert await can_access_folder(db_session, user=collab, folder_id=folder.id) is False

    db_session.add(
        Grant(
            resource_kind="patient",
            resource_id=patient.id,
            grantor_subject_id=owner.subject_id,
            grantee_subject_id=collab.subject_id,
            permissions=["read:metadata", "write:report"],
            valid_from=datetime.now(UTC),
        )
    )
    await db_session.flush()

    # Patient-level grant cascades into the patient's folder.
    assert await can_access_folder(db_session, user=collab, folder_id=folder.id) is True

    # Drop the flushed-but-uncommitted rows before make_user's teardown
    # deletes the subjects — otherwise the still-pending patient/folder/
    # grant FK-reference those subjects and the DELETE errors.
    await db_session.rollback()


# --------------------------------------------------------------------------
# BUG#5 — contacts surface delegation only when the backing grant is live
# --------------------------------------------------------------------------


async def test_contacts_hide_delegation_when_grant_not_active(
    db_session: AsyncSession, make_user
) -> None:
    from bvphoenix.api.patients._shared import _load_patient_contacts

    owner = await make_user(email=f"cowner-{uuid.uuid4().hex[:8]}@test.local")
    delegate = await make_user(email=f"cdelegate-{uuid.uuid4().hex[:8]}@test.local")
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Contacts patient",
    )
    db_session.add(patient)
    await db_session.flush()

    g_revoked = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=owner.subject_id,
        grantee_subject_id=delegate.subject_id,
        permissions=["read:metadata", "write:report"],
        valid_from=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
    )
    g_active = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=owner.subject_id,
        grantee_subject_id=delegate.subject_id,
        permissions=["read:metadata", "write:report"],
        valid_from=datetime.now(UTC),
    )
    db_session.add_all([g_revoked, g_active])
    await db_session.flush()

    dead = PatientContact(
        patient_id=patient.id,
        label="Dead delegate",
        email="dead@test.local",
        delegation_subject_id=delegate.subject_id,
        delegation_grant_id=g_revoked.id,
        delegation_level="editor",
    )
    live = PatientContact(
        patient_id=patient.id,
        label="Live delegate",
        email="live@test.local",
        delegation_subject_id=delegate.subject_id,
        delegation_grant_id=g_active.id,
        delegation_level="editor",
    )
    db_session.add_all([dead, live])
    await db_session.flush()

    contacts = await _load_patient_contacts(db_session, patient.id)
    by_label = {c.label: c for c in contacts}
    # Revoked grant → delegation hidden (contact reads as informational).
    assert by_label["Dead delegate"].delegation_level is None
    assert by_label["Dead delegate"].delegation_subject_id is None
    # Active grant → delegation surfaced.
    assert by_label["Live delegate"].delegation_level == "editor"

    await db_session.rollback()
