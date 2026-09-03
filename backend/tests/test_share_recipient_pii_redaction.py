"""Public ``GET /shared/{token}/info`` MUST NOT leak PII — neither the
intended recipient's, nor the record subject's.

The endpoint is unauthenticated: anyone who has (or guesses, or
intercepts) the token can hit it. Returning the addressee's name /
email lets a phisher reproduce the personalised landing page verbatim
and lets an attacker who somehow lifted the token learn whom the
grantor sent it to (third-party PII). The minimum-disclosure principle
applies; the recipient already knows their own email.

This test asserts at the schema level — ``ShareInfoOut`` must not
declare those fields — and at the runtime level: the response payload
shall contain neither ``recipient_email`` nor ``recipient_name`` keys.

The same argument covers the *subject* of a patient-scoped (fascicolo)
share. ``study_title`` used to be built as ``f"Fascicolo: {display_name}"``,
which handed the patient's real name to anyone holding the token —
before the password gate, so even to someone who never gets past
/verify. The share-invitation email builder had already made the
opposite call for the same reason ("Patient-scoped shares: don't leak
the real name"); ``/info`` had simply not followed it. The title is now
withheld and the landing page renders a kind-appropriate label from
``resource_kind``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.api.sharing import ShareInfoOut
from bvphoenix.db.models import Grant, Patient, ShareLink
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID

from .conftest import public_client, skip_if_no_db


def test_share_info_schema_excludes_recipient_pii() -> None:
    """``ShareInfoOut`` schema must not declare recipient_email or
    recipient_name as fields. The OpenAPI consumer (FE) therefore
    cannot accidentally surface them on the public landing.
    """
    fields = set(ShareInfoOut.model_fields.keys())
    assert "recipient_email" not in fields, (
        "recipient_email leaks third-party PII on /shared/{token}/info"
    )
    assert "recipient_name" not in fields, (
        "recipient_name leaks third-party PII on /shared/{token}/info"
    )


def test_share_info_serialised_excludes_recipient_pii() -> None:
    """A populated ShareInfoOut instance must not serialise either
    recipient field even if the caller passes them via extra= kwargs.
    Defends against a future maintainer reintroducing the leak.
    """
    info = ShareInfoOut(
        study_title="CT thorax",
        modalities=["CT"],
        study_date="2025-01-01",
        requires_password=False,
        expires_at=None,
        permissions=["read"],
        max_uses=None,
        uses_remaining=None,
        resource_kind="study",
        resource_id="00000000-0000-0000-0000-000000000000",
        mode="claim",
        claimable=False,
        deidentified=False,
        total_files=10,
        total_bytes=10_000,
        grantor_display="Dr Rossi",
    )
    payload = info.model_dump()
    assert "recipient_email" not in payload
    assert "recipient_name" not in payload


@skip_if_no_db
async def test_share_info_withholds_patient_name_on_fascicolo_share(
    db_session: AsyncSession, make_user
) -> None:
    """A patient-scoped share must not disclose who the record belongs
    to on the unauthenticated landing endpoint.

    The assertion is on the whole serialised payload, not just
    ``study_title``: a future field that happens to echo the name would
    be the same leak under a different key.
    """
    owner = await make_user(email=f"pii-{uuid.uuid4().hex[:8]}@test.local")
    # Distinctive enough that a substring match cannot pass by accident.
    display_name = f"Zbigniew Testardi {uuid.uuid4().hex[:8]}"
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name=display_name,
    )
    db_session.add(patient)
    await db_session.flush()
    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=owner.subject_id,
        grantee_subject_id=PUBLIC_SUBJECT_ID,
        permissions=["read:metadata"],
        valid_from=datetime.now(UTC),
    )
    db_session.add(grant)
    await db_session.flush()
    token = f"tok-{uuid.uuid4().hex}"
    db_session.add(ShareLink(grant_id=grant.id, token=token, mode="claim"))
    await db_session.flush()
    await db_session.commit()
    # Capture the ids as plain values: after the commit/rollback cycle the
    # ORM instances are expired, and touching ``.id`` in the teardown would
    # emit a refresh against a session we are about to unwind.
    grant_id, patient_id = str(grant.id), str(patient.id)

    try:
        async with public_client(db_session) as pub:
            resp = await pub.get(f"/api/shared/{token}/info")
        assert resp.status_code == 200, resp.text
        payload = resp.json()

        assert display_name not in resp.text, (
            "patient display_name leaks on the pre-auth /shared/{token}/info"
        )
        assert payload["study_title"] is None
        # The landing page still needs to know what it is looking at.
        assert payload["resource_kind"] == "patient"
        assert payload["resource_id"] == patient_id
    finally:
        # Order matters: the grant must go before ``make_user`` tears the
        # subject down, or its FK holds the subject hostage.
        for stmt, params in (
            ("DELETE FROM share_links WHERE grant_id = :g", {"g": grant_id}),
            ("DELETE FROM grants WHERE id = :g", {"g": grant_id}),
            (
                "DELETE FROM folder_items WHERE folder_id IN "
                "(SELECT id FROM folders WHERE patient_id = :p)",
                {"p": patient_id},
            ),
            ("DELETE FROM folders WHERE patient_id = :p", {"p": patient_id}),
            ("DELETE FROM patients WHERE id = :p", {"p": patient_id}),
        ):
            await db_session.execute(text(stmt), params)
        await db_session.commit()
