"""An invitation follows the mailbox, and only a proven one.

A share link addressed to somebody who has no account yet issues its
grant to ``PUBLIC_SUBJECT_ID``: the link is the credential. Before
``services.invitations`` existed that was a one-way door — if the
recipient later signed up with the very address the link named, nothing
connected the two, and the only way into the record stayed the URL.
Forever, and for every future share.

The rule these tests pin down is the whole security argument:

    an invitation attaches to a subject when that subject has proven
    control of the address it was sent to, and ``email_verified_at`` is
    what proof means.

Matching on ``share_links.recipient_email`` alone would be an
account-takeover primitive — learn that a record was shared with
someone@example.com, register it, collect the grant. Half of these
tests exist to hold that line rather than to describe a feature.

Real PostgreSQL: the write-once trigger on ``grants.grantee_subject_id``
and the normalise/reset trigger on ``users.email`` (alembic 0048) are
part of what is being asserted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Grant, Patient, ShareLink, User
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.services.invitations import (
    pending_invitations_for_user,
    reconcile_invitations_for_user,
)

from .conftest import public_client, skip_if_no_db

pytestmark = skip_if_no_db


@pytest_asyncio.fixture
async def make_patient(db_session: AsyncSession):
    created: list[uuid.UUID] = []

    async def _make(owner_subject_id: uuid.UUID) -> Patient:
        p = Patient(display_name="Record subject", managed_by_subject_id=owner_subject_id)
        db_session.add(p)
        await db_session.flush()
        created.append(p.id)
        return p

    yield _make

    for pid in created:
        await db_session.execute(Grant.__table__.delete().where(Grant.resource_id == pid))
        await db_session.execute(Patient.__table__.delete().where(Patient.id == pid))
    await db_session.commit()


async def _invitation(
    db: AsyncSession,
    *,
    patient: Patient,
    grantor_subject_id: uuid.UUID,
    to: str,
    grantee: uuid.UUID = PUBLIC_SUBJECT_ID,
    revoked: bool = False,
    valid_until: datetime | None = None,
    claimed_by: uuid.UUID | None = None,
) -> tuple[Grant, ShareLink]:
    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=grantor_subject_id,
        grantee_subject_id=grantee,
        permissions=["read:metadata"],
        valid_until=valid_until,
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    db.add(grant)
    await db.flush()
    link = ShareLink(
        grant_id=grant.id,
        token=f"tok-{uuid.uuid4().hex}",
        mode="claim",
        recipient_email=to,
        claimed_by_subject_id=claimed_by,
        claimed_at=datetime.now(UTC) if claimed_by else None,
    )
    db.add(link)
    await db.flush()
    return grant, link


async def _verify(db: AsyncSession, user: User) -> None:
    user.email_verified_at = datetime.now(UTC)
    await db.flush()


# --------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verified_address_collects_the_invitation(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """The case the whole thing exists for: somebody was invited, signed
    up separately, confirmed their address, and must now find the record
    in their account without ever opening the link."""
    owner = await make_user()
    recipient = await make_user(email="invited@example.com")
    patient = await make_patient(owner.subject_id)
    grant, link = await _invitation(
        db_session, patient=patient, grantor_subject_id=owner.subject_id, to="invited@example.com"
    )
    await _verify(db_session, recipient)

    attached = await reconcile_invitations_for_user(db_session, user=recipient)

    assert [a.grant_id for a in attached] == [grant.id]
    await db_session.refresh(grant)
    await db_session.refresh(link)
    assert grant.grantee_subject_id == recipient.subject_id
    assert link.claimed_by_subject_id == recipient.subject_id
    assert link.claimed_at is not None


@pytest.mark.asyncio
async def test_unverified_address_collects_nothing(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """The account-takeover case. Anyone can create an account claiming
    an address; only the mailbox holder can confirm it. Until they do,
    the invitation stays where it is."""
    owner = await make_user()
    impostor = await make_user(email="invited@example.com")
    patient = await make_patient(owner.subject_id)
    grant, _ = await _invitation(
        db_session, patient=patient, grantor_subject_id=owner.subject_id, to="invited@example.com"
    )
    assert impostor.email_verified_at is None

    assert await pending_invitations_for_user(db_session, user=impostor) == []
    assert await reconcile_invitations_for_user(db_session, user=impostor) == []
    await db_session.refresh(grant)
    assert grant.grantee_subject_id == PUBLIC_SUBJECT_ID


@pytest.mark.asyncio
async def test_a_deactivated_account_collects_nothing(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    owner = await make_user()
    blocked = await make_user(email="blocked@example.com")
    patient = await make_patient(owner.subject_id)
    await _invitation(
        db_session, patient=patient, grantor_subject_id=owner.subject_id, to="blocked@example.com"
    )
    await _verify(db_session, blocked)
    blocked.is_active = False
    await db_session.flush()

    assert await reconcile_invitations_for_user(db_session, user=blocked) == []


@pytest.mark.asyncio
async def test_an_invitation_addressed_elsewhere_is_not_collected(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    owner = await make_user()
    someone = await make_user(email="someone@example.com")
    patient = await make_patient(owner.subject_id)
    grant, _ = await _invitation(
        db_session, patient=patient, grantor_subject_id=owner.subject_id, to="other@example.com"
    )
    await _verify(db_session, someone)

    assert await reconcile_invitations_for_user(db_session, user=someone) == []
    await db_session.refresh(grant)
    assert grant.grantee_subject_id == PUBLIC_SUBJECT_ID


@pytest.mark.asyncio
async def test_revoked_and_expired_invitations_are_not_resurrected(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """A revoked invitation is not an invitation. Neither is one whose
    grant ran out — collecting either would hand back access somebody
    deliberately took away or let lapse."""
    owner = await make_user()
    recipient = await make_user(email="late@example.com")
    patient = await make_patient(owner.subject_id)
    revoked, _ = await _invitation(
        db_session,
        patient=patient,
        grantor_subject_id=owner.subject_id,
        to="late@example.com",
        revoked=True,
    )
    expired, _ = await _invitation(
        db_session,
        patient=patient,
        grantor_subject_id=owner.subject_id,
        to="late@example.com",
        valid_until=datetime.now(UTC) - timedelta(days=1),
    )
    await _verify(db_session, recipient)

    assert await reconcile_invitations_for_user(db_session, user=recipient) == []
    for g in (revoked, expired):
        await db_session.refresh(g)
        assert g.grantee_subject_id == PUBLIC_SUBJECT_ID


@pytest.mark.asyncio
async def test_an_invitation_somebody_else_claimed_is_left_alone(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """Once a grant is off PUBLIC it belongs to an account. The datastore
    forbids moving it (``trg_grants_grantee_write_once``); the sweep must
    not even try."""
    owner = await make_user()
    first = await make_user(email="first@example.com")
    second = await make_user(email="shared-link@example.com")
    patient = await make_patient(owner.subject_id)
    grant, _ = await _invitation(
        db_session,
        patient=patient,
        grantor_subject_id=owner.subject_id,
        to="shared-link@example.com",
        grantee=first.subject_id,
        claimed_by=first.subject_id,
    )
    await _verify(db_session, second)

    assert await reconcile_invitations_for_user(db_session, user=second) == []
    await db_session.refresh(grant)
    assert grant.grantee_subject_id == first.subject_id


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """It runs on every login, so a second pass must find nothing rather
    than churn the grant."""
    owner = await make_user()
    recipient = await make_user(email="repeat@example.com")
    patient = await make_patient(owner.subject_id)
    await _invitation(
        db_session, patient=patient, grantor_subject_id=owner.subject_id, to="repeat@example.com"
    )
    await _verify(db_session, recipient)

    assert len(await reconcile_invitations_for_user(db_session, user=recipient)) == 1
    assert await reconcile_invitations_for_user(db_session, user=recipient) == []


@pytest.mark.asyncio
async def test_every_invitation_to_one_mailbox_is_collected_at_once(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """Two shares to the same person should not need two links opened."""
    owner = await make_user()
    recipient = await make_user(email="two@example.com")
    p1 = await make_patient(owner.subject_id)
    p2 = await make_patient(owner.subject_id)
    await _invitation(
        db_session, patient=p1, grantor_subject_id=owner.subject_id, to="two@example.com"
    )
    await _invitation(
        db_session, patient=p2, grantor_subject_id=owner.subject_id, to="two@example.com"
    )
    await _verify(db_session, recipient)

    attached = await reconcile_invitations_for_user(db_session, user=recipient)
    assert len(attached) == 2
    assert {a.resource_id for a in attached} == {p1.id, p2.id}


# --------------------------------------------------------------------
# The datastore invariants the rule leans on
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_an_address_drops_its_verification(
    db_session: AsyncSession, make_user
) -> None:
    """Reconciliation reads ``email_verified_at`` as proof about the
    address currently on the row. If the address could move while the
    flag stayed set, the sweep would attach invitations addressed to the
    new mailbox on the strength of a verification of the old one."""
    user = await make_user(email="before@example.com")
    await _verify(db_session, user)
    assert user.email_verified_at is not None

    user.email = "after@example.com"
    await db_session.flush()
    await db_session.refresh(user)

    assert user.email_verified_at is None, "the trigger cleared it"


@pytest.mark.asyncio
async def test_account_addresses_are_stored_canonically(
    db_session: AsyncSession, make_user
) -> None:
    """One side of the reconciliation join. ``users_email_key`` is a
    byte-wise unique index, so without a single canonical spelling two
    rows differing only in case could both satisfy a case-insensitive
    match and the sweep would have to choose between two accounts.

    Normalised rather than rejected: every lookup in the codebase
    lowercases before querying, so an account stored as ``A@x.com``
    could never be logged into at all.
    """
    user = await make_user(email="  MiXeD@Example.COM  ")
    await db_session.flush()
    await db_session.refresh(user)
    assert user.email == "mixed@example.com"


@pytest.mark.asyncio
async def test_a_grant_cannot_move_between_two_accounts(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """Write-once off PUBLIC. Moving a grant from one real subject to
    another is an ownership transfer wearing an UPDATE, and no code
    should be able to perform it by accident."""
    owner = await make_user()
    first = await make_user(email="a@example.com")
    second = await make_user(email="b@example.com")
    patient = await make_patient(owner.subject_id)
    grant, _ = await _invitation(
        db_session,
        patient=patient,
        grantor_subject_id=owner.subject_id,
        to="a@example.com",
        grantee=first.subject_id,
    )

    grant.grantee_subject_id = second.subject_id
    with pytest.raises(Exception) as exc:
        await db_session.flush()
    assert "write-once" in str(exc.value)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_share_link_addressee_is_stored_canonically(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    owner = await make_user()
    patient = await make_patient(owner.subject_id)
    _, link = await _invitation(
        db_session,
        patient=patient,
        grantor_subject_id=owner.subject_id,
        to="  Recipient@Example.COM  ",
    )
    await db_session.refresh(link)
    assert link.recipient_email == "recipient@example.com"

    # ...and therefore a verified account with that address finds it.
    recipient = await make_user(email="recipient@example.com")
    await _verify(db_session, recipient)
    assert len(await reconcile_invitations_for_user(db_session, user=recipient)) == 1


@pytest.mark.asyncio
async def test_no_account_creation_path_leaves_an_unverifiable_account(
    db_session: AsyncSession,
) -> None:
    """The lockout, stated as an invariant over the code rather than
    over one endpoint.

    ``require_email_verification`` is on in production, and
    ``api/auth.login`` refuses an account whose ``email_verified_at`` is
    NULL. An account created with a password but no verification token
    is therefore locked out permanently, with the right password. That
    is exactly what the share-link claim used to produce.

    Every path that mints a ``User`` with a password must go through
    ``services.account_provisioning.start_email_verification``.
    """
    import inspect

    from bvphoenix.api import auth as auth_api
    from bvphoenix.api import sharing as sharing_api

    for name, fn in (
        ("register", auth_api.register),
        ("resend_verification", auth_api.resend_verification),
        ("_perform_claim", sharing_api._perform_claim),
    ):
        source = inspect.getsource(fn)
        assert "start_email_verification" in source, (
            f"{name} creates or repairs a local account without issuing a "
            "verification token; with require_email_verification on that "
            "account can never log in"
        )


@pytest.mark.asyncio
async def test_login_names_the_verification_gate_with_a_stable_code(
    db_session: AsyncSession, make_user, monkeypatch
) -> None:
    """The 403 has to be actionable.

    It used to answer with the prose "email not verified" and nothing
    else, so the login screen printed an English sentence at a dead end
    and an account born from a share-link claim was indistinguishable
    from a forgotten password. The slug is what lets the screen offer
    the resend instead.
    """
    from bvphoenix.auth import hash_password
    from bvphoenix.config import get_settings

    user = await make_user(email="unverified@example.com")
    user.password_hash = hash_password("correct-horse-battery")
    await db_session.flush()
    await db_session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "require_email_verification", True, raising=False)

    async with public_client(db_session) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"email": "unverified@example.com", "password": "correct-horse-battery"},
        )

    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["type"].endswith("/email_not_verified"), body


@pytest.mark.asyncio
async def test_login_attachment_survives_the_request(
    db_session: AsyncSession, make_user, make_patient
) -> None:
    """The attachment has to be committed, not merely flushed.

    ``db.session.get_db`` hands the handler a session and never commits
    it, so a sweep that only flushes is silently discarded when the
    request ends: the user logs in, the fascicolo is not there, and
    nothing anywhere says why. The assertion therefore reads back
    through a *separate* session — through the shared test session a
    flush is indistinguishable from a commit, which is exactly how this
    slipped through the first time.
    """
    from bvphoenix.auth import hash_password
    from bvphoenix.db.session import SessionFactory

    owner = await make_user()
    recipient = await make_user(email="commits@example.com")
    recipient.password_hash = hash_password("correct-horse-battery")
    recipient.email_verified_at = datetime.now(UTC)
    patient = await make_patient(owner.subject_id)
    grant, _ = await _invitation(
        db_session, patient=patient, grantor_subject_id=owner.subject_id, to="commits@example.com"
    )
    await db_session.commit()

    async with public_client(db_session) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"email": "commits@example.com", "password": "correct-horse-battery"},
        )
    assert resp.status_code == 200, resp.text

    async with SessionFactory() as fresh:
        stored = (await fresh.execute(select(Grant).where(Grant.id == grant.id))).scalar_one()
        assert stored.grantee_subject_id == recipient.subject_id, (
            "the invitation was attached but never committed"
        )
