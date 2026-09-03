"""One contact per mailbox, and a contact you can actually delete.

Three defects met on one production fascicolo, all of them about the
same thing: a contact's email is an identity, and nothing treated it as
one.

* The patient edit form rebuilt every contact without its ``id``.
  ``replace_all_contacts`` matched on ``id`` alone, so each save inserted
  a fresh row for every contact while the rows carrying a delegation
  were held back from deletion. Five contacts became eight, and removing
  a duplicate through the same form produced two more.
* ``delete_contact`` read the ``delegation_*`` pointer columns without
  asking whether the grant behind them was still alive. A grant revoked
  by any path other than ``revoke_contact_delegation`` left the pointers
  populated, and the contact answered 409 forever with nothing left to
  revoke.
* Nothing at the datastore prevented two rows sharing a mailbox, which
  is ambiguous three times over: the address is what a delegation
  resolves an account by, what the notification dispatcher sends to, and
  what the RFC 8058 opt-out token identifies.

These run against real PostgreSQL because two of the three fixes *are*
the schema (``uq_patient_contacts_patient_email`` and the grant triggers
from alembic 0048/0049); a substitute engine would assert nothing.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Grant, Patient, PatientContact, ShareLink
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.services import patient_contacts as svc

from .conftest import client_as, skip_if_no_db

pytestmark = skip_if_no_db


@pytest_asyncio.fixture
async def make_patient(db_session: AsyncSession):
    """Factory + teardown for a patient owned by ``owner``.

    Patients and grants reference the owner subject, so they must be
    gone before ``make_user`` drops it — otherwise every test in the
    file ends on a foreign-key violation during teardown rather than on
    its own assertion.
    """
    created: list[uuid.UUID] = []

    async def _make(owner_subject_id: uuid.UUID) -> Patient:
        p = Patient(display_name="Test Subject", managed_by_subject_id=owner_subject_id)
        db_session.add(p)
        await db_session.flush()
        created.append(p.id)
        return p

    yield _make

    for pid in created:
        await db_session.execute(Grant.__table__.delete().where(Grant.resource_id == pid))
        await db_session.execute(
            PatientContact.__table__.delete().where(PatientContact.patient_id == pid)
        )
        await db_session.execute(Patient.__table__.delete().where(Patient.id == pid))
    await db_session.commit()


async def _delegate(
    db: AsyncSession,
    *,
    patient: Patient,
    contact: PatientContact,
    grantor_subject_id: uuid.UUID,
) -> tuple[Grant, ShareLink]:
    """Minimal stand-in for ``promote_contact_to_delegate``: a live grant
    plus its link, with the contact's pointers set."""
    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=grantor_subject_id,
        grantee_subject_id=PUBLIC_SUBJECT_ID,
        permissions=["read:metadata"],
    )
    db.add(grant)
    await db.flush()
    link = ShareLink(
        grant_id=grant.id,
        token=f"tok-{uuid.uuid4().hex}",
        mode="claim",
        recipient_email=contact.email,
    )
    db.add(link)
    await db.flush()
    contact.delegation_grant_id = grant.id
    contact.delegation_share_link_id = link.id
    contact.delegation_subject_id = PUBLIC_SUBJECT_ID
    contact.delegation_level = "editor"
    await db.flush()
    return grant, link


# --------------------------------------------------------------------
# The duplication mechanism
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_id_less_replace_all_reconciles_on_email_instead_of_duplicating(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """The exact production sequence: a full contacts array with no ids.

    Before the fix this inserted one new row per entry and kept the
    delegated originals, so the list grew on every save. It must now
    land on the rows that are already there.
    """
    owner = await make_user()
    patient = await make_patient(owner.subject_id)

    kept = await svc.create_contact(
        db_session,
        patient_id=patient.id,
        label="Angelo Leto",
        relationship="figlio",
        email="angelo@leto.blue",
        phone="+393288829997",
        consent_to_contact=True,
    )
    delegated = await svc.create_contact(
        db_session,
        patient_id=patient.id,
        label="Alfonso Leto",
        relationship="marito",
        email="alfonso@example.com",
    )
    await _delegate(
        db_session, patient=patient, contact=delegated, grantor_subject_id=owner.subject_id
    )
    original_ids = {kept.id, delegated.id}

    # What the form used to send: no ids at all.
    await svc.replace_all_contacts(
        db_session,
        patient_id=patient.id,
        incoming=[
            {"label": "Angelo Leto", "relationship": "figlio", "email": "angelo@leto.blue"},
            {"label": "Alfonso Leto", "relationship": "marito", "email": "alfonso@example.com"},
        ],
    )

    rows = list(await svc.list_contacts(db_session, patient.id))
    assert len(rows) == 2, "an id-less save must reconcile, not duplicate"
    assert {r.id for r in rows} == original_ids, "the rows kept their identity"

    # And the state that only lives on the row survived the round-trip:
    # re-inserting would have reset consent and minted a new opt-out
    # token, silently breaking an unsubscribe link already in the wild.
    refreshed = {r.id: r for r in rows}
    assert refreshed[kept.id].consent_to_contact is True
    assert refreshed[delegated.id].delegation_grant_id is not None


@pytest.mark.asyncio
async def test_replace_all_matches_email_case_insensitively(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """A client that echoes back a differently-cased address must still
    land on the same row — the database stores one canonical spelling."""
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    existing = await svc.create_contact(
        db_session, patient_id=patient.id, label="X", email="Mixed.Case@Example.COM"
    )
    assert existing.email == "mixed.case@example.com", "normalised on write"

    await svc.replace_all_contacts(
        db_session,
        patient_id=patient.id,
        incoming=[{"label": "X renamed", "email": "  MIXED.case@example.com "}],
    )
    rows = list(await svc.list_contacts(db_session, patient.id))
    assert len(rows) == 1
    assert rows[0].id == existing.id
    assert rows[0].label == "X renamed"


@pytest.mark.asyncio
async def test_two_entries_with_one_address_are_refused(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """Folding them into one row would discard whichever the caller
    meant to keep, so the write is refused and the address is named."""
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    with pytest.raises(svc.DuplicateContactEmailError) as exc:
        await svc.replace_all_contacts(
            db_session,
            patient_id=patient.id,
            incoming=[
                {"label": "One", "email": "shared@example.com"},
                {"label": "Two", "email": "SHARED@example.com"},
            ],
        )
    assert exc.value.email == "shared@example.com"


@pytest.mark.asyncio
async def test_contacts_without_an_address_are_not_constrained(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """The uniqueness is on the mailbox, so any number of phone-only
    contacts coexist. This is the escape hatch for two people who really
    do share an inbox."""
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    await svc.create_contact(db_session, patient_id=patient.id, label="Nonna", phone="+3900")
    await svc.create_contact(db_session, patient_id=patient.id, label="Vicino", phone="+3901")
    await svc.create_contact(db_session, patient_id=patient.id, label="Blank", email="   ")
    await db_session.flush()
    rows = list(await svc.list_contacts(db_session, patient.id))
    assert len(rows) == 3
    assert all(r.email is None for r in rows), "empty string is stored as NULL"


# --------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_contact_is_deletable(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    row = await svc.create_contact(
        db_session, patient_id=patient.id, label="Plain", email="plain@example.com"
    )
    outcome = await svc.delete_contact(db_session, patient_id=patient.id, contact_id=row.id)
    assert outcome.deleted is True
    assert outcome.revoked_delegation is False
    assert list(await svc.list_contacts(db_session, patient.id)) == []


@pytest.mark.asyncio
async def test_live_delegation_blocks_deletion_until_asked_for(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """Refused by default: taking somebody off the contact list while
    they keep a working grant would hide their access from the only
    screen that shows it."""
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    row = await svc.create_contact(
        db_session, patient_id=patient.id, label="Delegate", email="d@example.com"
    )
    grant, link = await _delegate(
        db_session, patient=patient, contact=row, grantor_subject_id=owner.subject_id
    )

    refused = await svc.delete_contact(db_session, patient_id=patient.id, contact_id=row.id)
    assert refused.deleted is False
    assert refused.code == "delegation_active"
    assert refused.delegation_grant_id == grant.id

    accepted = await svc.delete_contact(
        db_session, patient_id=patient.id, contact_id=row.id, revoke_delegation=True
    )
    assert accepted.deleted is True
    assert accepted.revoked_delegation is True
    assert (
        await db_session.execute(select(Grant).where(Grant.id == grant.id))
    ).scalar_one_or_none() is None, "the grant goes with the contact"
    assert (
        await db_session.execute(select(ShareLink).where(ShareLink.id == link.id))
    ).scalar_one_or_none() is None, "and the link cascades with the grant"


@pytest.mark.asyncio
async def test_revoked_grant_does_not_wedge_the_contact(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """The production dead end, and the reason the invariant moved into
    the datastore.

    Revoking a grant by any path other than ``revoke_contact_delegation``
    used to leave all four pointer columns populated. ``delete_contact``
    read them as "still delegated" and refused, permanently, with
    nothing left to revoke. The trigger from alembic 0048 clears them at
    the moment the grant dies, so the state cannot recur.
    """
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    row = await svc.create_contact(
        db_session, patient_id=patient.id, label="Stale", email="stale@example.com"
    )
    grant, _ = await _delegate(
        db_session, patient=patient, contact=row, grantor_subject_id=owner.subject_id
    )

    # Revoke the grant the way every other surface does: set revoked_at.
    from datetime import UTC, datetime

    grant.revoked_at = datetime.now(UTC)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.delegation_grant_id is None, "the trigger cleared the pointers"
    assert row.delegation_level is None

    outcome = await svc.delete_contact(db_session, patient_id=patient.id, contact_id=row.id)
    assert outcome.deleted is True, "a dead delegation must not block deletion"


@pytest.mark.asyncio
async def test_deleting_a_missing_contact_is_a_404_not_a_crash(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    outcome = await svc.delete_contact(db_session, patient_id=patient.id, contact_id=uuid.uuid4())
    assert outcome.deleted is False
    assert outcome.code == "not_found"


# --------------------------------------------------------------------
# The constraint itself
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_refuses_a_second_contact_on_one_address(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """End to end through the API, so the IntegrityError is proved to
    surface as the stable code the UI branches on rather than a 500."""
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    await db_session.commit()

    async with client_as(db_session, owner) as client:
        first = await client.post(
            f"/api/patients/{patient.id}/contacts",
            json={"label": "One", "email": "dup@example.com"},
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            f"/api/patients/{patient.id}/contacts",
            json={"label": "Two", "email": "DUP@example.com"},
        )
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["type"].endswith("/contact_email_duplicate")
        assert body["email"] == "dup@example.com"
