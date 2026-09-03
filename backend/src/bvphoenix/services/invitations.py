"""Attach invitations addressed to a mailbox onto the account that owns it.

The problem this solves
-----------------------

A share link created for a named recipient is an *invitation*: it names
an email address in ``share_links.recipient_email`` and, when no account
exists for that address yet, its grant is issued to
``PUBLIC_SUBJECT_ID`` so that the link itself is the credential
(:func:`bvphoenix.services.patient_delegation._resolve_or_create_grantee`).

Before this module existed, that was a one-way door. If the recipient
later created an account through ``POST /api/auth/register`` instead of
following the link, nothing connected the two: the grant stayed on
PUBLIC, the fascicolo was invisible from their account, and the only way
in remained the original URL — forever, and for every future share.
Delegating access to a family member therefore produced a person who had
an account, had a password, and still had to dig a link out of a chat
thread every single time.

The rule
--------

An invitation attaches to a subject when that subject has **proven
control of the address the invitation was sent to**. Proof is
``users.email_verified_at IS NOT NULL`` and nothing else.

Matching on ``recipient_email`` alone would be an account-takeover
primitive: anyone who learns that a fascicolo was shared with
``someone@example.com`` could register that address and collect the
grant. Verification is what closes it, because the verification token is
delivered only to the mailbox. Two datastore invariants installed by
alembic ``0048`` hold the rule up:

* ``trg_users_email_change_resets_verification`` — an address change
  clears ``email_verified_at`` and burns outstanding tokens, so a
  verified flag always describes the address currently on the row;
* ``ck_users_email_canonical`` / ``ck_share_links_recipient_email_canonical``
  — both sides of the join are stored lowercased and trimmed, so the
  match is an exact equality and cannot resolve to two accounts.

Deliberately *not* done here:

* No canonicalisation beyond the stored form. Gmail treats ``a.b@`` and
  ``ab@`` as one mailbox; most providers do not, and folding them here
  would attach an invitation addressed to one person onto a different
  person's account at a provider that keeps them distinct.
* No attachment of a grant already held by a real subject. A grant moves
  off PUBLIC exactly once — ``trg_grants_grantee_write_once`` enforces
  it — so an invitation that somebody else has already claimed is never
  re-pointed.
* No attachment when the grant is revoked or expired. A revoked
  invitation is not an invitation.

``max_uses`` is deliberately ignored: it bounds how often the *link* may
be opened, which is a property of the bearer credential. Once the
invitation is attached to an account the link stops being the credential
at all, so an exhausted counter is not a reason to leave somebody
locked out of a fascicolo that was granted to them.

Where it is called from
-----------------------

* ``POST /api/auth/verify-email`` — the moment proof is established, and
  the reason this is not merely a convenience: it is where a newly
  verified account collects everything already waiting for it.
* ``POST /api/auth/login`` — after authentication and after the
  verification gate. Covers accounts verified before this module
  shipped, and invitations issued while the recipient was logged out.
* ``bvphoenix.api.sharing._perform_claim`` / ``_perform_bind`` — those
  attach the one link being acted on; this sweep picks up any *other*
  invitation addressed to the same mailbox in the same movement.

The caller owns the transaction: this module flushes, never commits, so
the attachment lands or rolls back with whatever the caller was doing.
The caller also owns the audit record — the return value carries exactly
what it needs to write one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Grant, PatientContact, ShareLink, User
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID

logger = logging.getLogger(__name__)

# Link modes whose grant is meant to end up on a real account. A
# ``view``-only mode (if one is ever added) would be a bearer credential
# by design and must not be swept up by this.
ATTACHABLE_MODES: frozenset[str] = frozenset({"anonymous", "claim"})


@dataclass(frozen=True)
class AttachedInvitation:
    """One invitation moved from PUBLIC onto a real account."""

    grant_id: uuid.UUID
    share_link_id: uuid.UUID
    resource_kind: str
    resource_id: uuid.UUID

    def as_audit_metadata(self) -> dict[str, str]:
        return {
            "grant_id": str(self.grant_id),
            "share_link_id": str(self.share_link_id),
            "resource_kind": self.resource_kind,
            "resource_id": str(self.resource_id),
        }


async def pending_invitations_for_user(
    db: AsyncSession, *, user: User
) -> list[tuple[ShareLink, Grant]]:
    """Return the invitations this user is entitled to but has not got.

    Read-only twin of :func:`reconcile_invitations_for_user`, used by the
    surfaces that want to *show* the pending state without changing it.
    Returns an empty list when the address is unproven, so a caller
    cannot accidentally advertise a capability that reconciliation would
    refuse to grant.
    """
    if user.email_verified_at is None or not user.is_active:
        return []
    now = datetime.now(UTC)
    stmt = (
        select(ShareLink, Grant)
        .join(Grant, Grant.id == ShareLink.grant_id)
        .where(
            ShareLink.recipient_email == user.email,
            ShareLink.claimed_at.is_(None),
            ShareLink.mode.in_(tuple(ATTACHABLE_MODES)),
            Grant.grantee_subject_id == PUBLIC_SUBJECT_ID,
            Grant.revoked_at.is_(None),
            (Grant.valid_until.is_(None)) | (Grant.valid_until > now),
        )
        .order_by(ShareLink.created_at.asc())
    )
    return [(link, grant) for link, grant in (await db.execute(stmt)).all()]


async def reconcile_invitations_for_user(
    db: AsyncSession, *, user: User
) -> list[AttachedInvitation]:
    """Attach every pending invitation addressed to ``user.email``.

    Idempotent: a second call finds nothing, because the first one moved
    the grant off PUBLIC and stamped ``claimed_at``. Safe to call on
    every login.

    Returns what was attached, so the caller can audit it. Flushes but
    does not commit.
    """
    pending = await pending_invitations_for_user(db, user=user)
    if not pending:
        return []

    now = datetime.now(UTC)
    attached: list[AttachedInvitation] = []
    for link, grant in pending:
        grant.grantee_subject_id = user.subject_id
        link.claimed_by_subject_id = user.subject_id
        link.claimed_at = now

        # A delegated contact carries a pointer to the subject the
        # delegation resolves to. It was PUBLIC while the invitation was
        # open; it has to follow the grant, or the fascicolo UI keeps
        # showing the contact as un-attached.
        contact = (
            await db.execute(
                select(PatientContact).where(PatientContact.delegation_share_link_id == link.id)
            )
        ).scalar_one_or_none()
        if contact is not None:
            contact.delegation_subject_id = user.subject_id

        attached.append(
            AttachedInvitation(
                grant_id=grant.id,
                share_link_id=link.id,
                resource_kind=grant.resource_kind,
                resource_id=grant.resource_id,
            )
        )

    await db.flush()
    logger.info(
        "attached %d pending invitation(s) to subject %s",
        len(attached),
        user.subject_id,
    )
    return attached


__all__ = [
    "ATTACHABLE_MODES",
    "AttachedInvitation",
    "pending_invitations_for_user",
    "reconcile_invitations_for_user",
]
