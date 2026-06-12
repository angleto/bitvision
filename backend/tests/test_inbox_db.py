"""DB-backed integration tests for the patient inbox (migration 0025).

Runs the real pipeline — address provisioning → raw intake → staging →
auto-checks → decision → promotion — against Postgres, with S3 replaced
by an in-memory fake (the storage seam is ``get_s3_storage``, patched
in every consuming module). ClamAV is intentionally absent: the check
reports ``error`` and the suite asserts the engine treats that as
"never silently clean" (item still queues, auto-accept refused).

Security assertions: cross-patient composite-FK rejection, the XOR
ingress constraint, RBAC refusal of an unrelated user, revoked-code
routing miss.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    Document,
    InboundEmail,
    InboxItem,
    InboxSenderAllowlist,
    Patient,
    ProvenanceEvent,
)
from bvphoenix.services.inbox import addresses as addr_service
from bvphoenix.services.inbox.checks import auto_accept_entry
from bvphoenix.services.inbox.emails import (
    InboundEmailError,
    persist_raw_email,
    purge_staged,
    stage_inbound_email,
)
from bvphoenix.services.inbox.profile import INBOX_PROFILE
from bvphoenix.services.review_queue import ReviewDecisionError
from bvphoenix.services.review_queue import engine as review_engine
from bvphoenix.services.review_queue.actor import ReviewActor
from tests.conftest import skip_if_no_db

pytestmark = [skip_if_no_db, pytest.mark.asyncio]


# ---- fakes -----------------------------------------------------------


class FakeStorage:
    """Dict-backed stand-in for S3Storage (only the methods the inbox
    paths call)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_bytes(self, data, *, bucket: str, key: str):
        payload = data if isinstance(data, bytes) else data.read()
        self.objects[(bucket, key)] = payload
        return None

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    def delete_object(self, *, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


@pytest.fixture()
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    storage = FakeStorage()
    for module in (
        "bvphoenix.services.inbox.emails",
        "bvphoenix.services.inbox.profile",
        "bvphoenix.services.inbox.promotion",
        "bvphoenix.services.documents.ingest_blob",
    ):
        monkeypatch.setattr(f"{module}.get_s3_storage", lambda: storage)
    return storage


@pytest.fixture()
def inbox_enabled(monkeypatch: pytest.MonkeyPatch):
    from bvphoenix.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "inbound_email_enabled", True)
    return settings


@pytest.fixture()
async def patient_with_owner(db_session: AsyncSession, make_user):
    owner = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="Inbox Test Patient",
    )
    db_session.add(patient)
    await db_session.flush()
    yield owner, patient
    # Roll back before ``make_user``'s teardown commits its subject
    # DELETE: the tests write human provenance rows in this (never
    # committed) transaction, and committing the cascade would SET NULL
    # their subject — tripping ``ck_provenance_events_human_subject_present``.
    await db_session.rollback()


def _raw_email(
    *,
    message_id: str | None = None,
    sender: str = "lab@rossi.example",
    attachments: list[tuple[str, bytes]] = (),
    auth: str | None = "mx.example; spf=pass; dkim=pass",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Lab <{sender}>"
    msg["To"] = "x@inbox.example"
    msg["Subject"] = "Referto"
    msg["Message-ID"] = message_id or f"<{uuid.uuid4()}@rossi.example>"
    if auth:
        msg["Authentication-Results"] = auth
    msg.set_content("In allegato il referto.")
    for filename, payload in attachments:
        msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    return bytes(msg)


_PDF = b"%PDF-1.4 test referto " + b"q" * 64


# ---- addresses -------------------------------------------------------


async def test_address_lifecycle_and_routing(
    db_session: AsyncSession, patient_with_owner, inbox_enabled
) -> None:
    owner, patient = patient_with_owner
    actor = ReviewActor(kind="human", subject_id=owner.subject_id)

    address = await addr_service.create_address(
        db_session, patient=patient, actor=actor, label="Laboratorio Rossi"
    )
    assert address.active and address.label == "Laboratorio Rossi"
    rendered = addr_service.render_address(address)
    assert rendered.startswith(address.code) and "+patient@" in rendered

    # RCPT routing finds the active code…
    found = await addr_service.resolve_active_code(db_session, address.code)
    assert found is not None and found.patient_id == patient.id

    # …and a revoked one goes dark.
    await addr_service.revoke_address(db_session, address=address, actor=actor, reason="done")
    assert not address.active and address.revoked_at is not None
    assert await addr_service.resolve_active_code(db_session, address.code) is None

    # Lifecycle is audited.
    activities = (
        (
            await db_session.execute(
                select(ProvenanceEvent.activity).where(
                    ProvenanceEvent.target_kind == "inbox_address",
                    ProvenanceEvent.target_id == address.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(activities) == {"create", "revoke"}


async def test_create_address_refused_when_disabled(
    db_session: AsyncSession, patient_with_owner
) -> None:
    owner, patient = patient_with_owner
    actor = ReviewActor(kind="human", subject_id=owner.subject_id)
    with pytest.raises(addr_service.InboxAddressError) as err:
        await addr_service.create_address(db_session, patient=patient, actor=actor)
    assert err.value.code == "inbox.disabled"


# ---- raw intake ------------------------------------------------------


async def test_persist_raw_email_dedup_and_caps(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage, monkeypatch
) -> None:
    owner, patient = patient_with_owner
    actor = ReviewActor(kind="human", subject_id=owner.subject_id)
    address = await addr_service.create_address(db_session, patient=patient, actor=actor)

    raw = _raw_email(message_id="<stable@rossi.example>", attachments=[("r.pdf", _PDF)])
    row = await persist_raw_email(db_session, address=address, raw=raw)
    assert row is not None
    assert row.patient_id == patient.id
    assert row.from_address == "lab@rossi.example"
    assert (inbox_enabled.s3_bucket_raw, row.raw_s3_key) in fake_storage.objects

    # Same Message-ID → dedup hit, no second row.
    assert await persist_raw_email(db_session, address=address, raw=raw) is None

    # Size cap → structured error the MTA maps to 552.
    monkeypatch.setattr(inbox_enabled, "inbound_email_max_raw_bytes", 10)
    with pytest.raises(InboundEmailError) as err:
        await persist_raw_email(db_session, address=address, raw=raw)
    assert err.value.code == "inbound.too_large"


# ---- staging + checks + decision + promotion -------------------------


async def _staged_item(db_session, owner, patient, fake_storage, **email_kw) -> InboxItem:
    actor = ReviewActor(kind="human", subject_id=owner.subject_id)
    address = await addr_service.create_address(db_session, patient=patient, actor=actor)
    raw = _raw_email(**email_kw)
    inbound = await persist_raw_email(db_session, address=address, raw=raw)
    assert inbound is not None
    return await stage_inbound_email(db_session, inbound=inbound)


async def test_stage_creates_item_with_manifest(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage
) -> None:
    owner, patient = patient_with_owner
    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )
    assert item.status == "received"
    assert item.source_channel == "email"
    comps = item.manifest["components"]
    assert [c["name"] for c in comps] == ["referto.pdf"]
    assert (inbox_enabled.s3_bucket_raw, comps[0]["s3_key"]) in fake_storage.objects
    assert item.manifest["email"]["spf"] == "pass"

    # Idempotent: a second staging pass returns the same item.
    inbound = await db_session.get(InboundEmail, item.inbound_email_id)
    again = await stage_inbound_email(db_session, inbound=inbound)
    assert again.id == item.id


async def test_full_review_cycle_promotes_document(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage
) -> None:
    owner, patient = patient_with_owner
    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )

    await review_engine.start_processing(db_session, INBOX_PROFILE, item)
    verdict = await review_engine.run_auto_checks(db_session, INBOX_PROFILE, item)
    # No clamd in the test env: the scan reports ``error`` and the item
    # still needs a human — never silently clean.
    assert verdict == "error"
    assert item.status == "needs_review"
    assert item.auto_checks["checks"]["clamav"]["verdict"] == "error"
    assert item.auto_checks["checks"]["magic_allowlist"]["verdict"] == "pass"
    assert item.auto_checks["checks"]["dicom_route"]["details"]["route"] == "document"

    actor = ReviewActor(kind="human", subject_id=owner.subject_id)
    await review_engine.decide(
        db_session, INBOX_PROFILE, item, decision="accepted", actor=actor, reason="ok"
    )
    assert item.status == "accepted"
    assert item.reviewed_by_subject_id == owner.subject_id

    outcome = await review_engine.promote(db_session, INBOX_PROFILE, item, actor=actor)
    assert item.status == "promoted"
    assert len(outcome["documents"]) == 1

    doc = await db_session.get(Document, uuid.UUID(outcome["documents"][0]["id"]))
    assert doc is not None
    assert doc.patient_id == patient.id
    assert doc.provenance_id == "email_attachment"
    # Provenance chain points back at the inbox item.
    chain = (
        (
            await db_session.execute(
                select(ProvenanceEvent).where(
                    ProvenanceEvent.target_kind == "document",
                    ProvenanceEvent.target_id == doc.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(e.source_kind == "inbox_item" and e.source_id == item.id for e in chain)


async def test_unrelated_user_cannot_decide(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage, make_user
) -> None:
    owner, patient = patient_with_owner
    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )
    await review_engine.start_processing(db_session, INBOX_PROFILE, item)
    await review_engine.run_auto_checks(db_session, INBOX_PROFILE, item)

    stranger = await make_user()
    actor = ReviewActor(kind="human", subject_id=stranger.subject_id)
    with pytest.raises(ReviewDecisionError) as err:
        await review_engine.decide(
            db_session, INBOX_PROFILE, item, decision="accepted", actor=actor
        )
    assert err.value.code == "decision.not_authorized"
    assert item.status == "needs_review"


async def test_reject_purges_staged_blobs(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage
) -> None:
    owner, patient = patient_with_owner
    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )
    await review_engine.start_processing(db_session, INBOX_PROFILE, item)
    await review_engine.run_auto_checks(db_session, INBOX_PROFILE, item)

    staged_key = item.manifest["components"][0]["s3_key"]
    assert (inbox_enabled.s3_bucket_raw, staged_key) in fake_storage.objects

    actor = ReviewActor(kind="human", subject_id=owner.subject_id)
    await review_engine.decide(
        db_session, INBOX_PROFILE, item, decision="rejected", actor=actor, reason="spam"
    )
    assert item.status == "rejected"
    assert (inbox_enabled.s3_bucket_raw, staged_key) not in fake_storage.objects


async def test_purge_staged_only_touches_inbox_prefix(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage
) -> None:
    owner, patient = patient_with_owner
    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )
    # A manifest tampered to point outside the staging prefix must not
    # delete canonical objects.
    fake_storage.objects[(inbox_enabled.s3_bucket_raw, "patient-docs/x/y.pdf")] = b"keep"
    item.manifest = {
        "components": [{"name": "evil", "s3_key": "patient-docs/x/y.pdf"}],
    }
    removed = await purge_staged(item)
    assert removed == 0
    assert (inbox_enabled.s3_bucket_raw, "patient-docs/x/y.pdf") in fake_storage.objects


# ---- auto-accept eligibility -----------------------------------------


async def test_auto_accept_requires_allowlist_alignment_and_clean_pass(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage
) -> None:
    owner, patient = patient_with_owner
    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )

    # Not allowlisted → no.
    item.auto_verdict = "pass"
    assert await auto_accept_entry(db_session, item) is None

    entry = InboxSenderAllowlist(
        id=uuid.uuid4(),
        patient_id=patient.id,
        sender_email="lab@rossi.example",
        created_by_subject_id=owner.subject_id,
    )
    db_session.add(entry)
    await db_session.flush()

    # Allowlisted + aligned + clean pass → yes, attributed to the creator.
    found = await auto_accept_entry(db_session, item)
    assert found is not None and found.id == entry.id

    # Any non-pass verdict (e.g. the clamd outage) → no.
    item.auto_verdict = "error"
    assert await auto_accept_entry(db_session, item) is None

    # Alignment failure → no.
    item.auto_verdict = "pass"
    item.manifest = {
        **item.manifest,
        "email": {**item.manifest["email"], "spf": "fail", "dkim": "fail"},
    }
    assert await auto_accept_entry(db_session, item) is None


# ---- cross-patient inexpressibility ----------------------------------


async def test_cross_patient_email_reference_is_rejected(
    db_session: AsyncSession, patient_with_owner, inbox_enabled, fake_storage, make_user
) -> None:
    from sqlalchemy.exc import IntegrityError

    owner, patient = patient_with_owner
    other_owner = await make_user()
    other_patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=other_owner.subject_id,
        display_name="Other",
    )
    db_session.add(other_patient)
    await db_session.flush()

    item = await _staged_item(
        db_session, owner, patient, fake_storage, attachments=[("referto.pdf", _PDF)]
    )

    evil = InboxItem(
        id=uuid.uuid4(),
        patient_id=other_patient.id,  # ≠ the email's patient
        inbound_email_id=item.inbound_email_id,
        source_channel="email",
        staged_prefix="_inbox/evil",
        status="received",
        etag=uuid.uuid4(),
    )
    db_session.add(evil)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_single_source_xor_constraint(
    db_session: AsyncSession, patient_with_owner, inbox_enabled
) -> None:
    from sqlalchemy.exc import IntegrityError

    _owner, patient = patient_with_owner
    neither = InboxItem(
        id=uuid.uuid4(),
        patient_id=patient.id,
        source_channel="upload_ui",
        status="received",
        etag=uuid.uuid4(),
    )
    db_session.add(neither)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
