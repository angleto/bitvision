"""Promote / revoke a ``Patient.contacts`` entry into a delegated user.

The fascicolo header surfaces every contact (family member, caregiver,
GP, etc.) as a tag. By default these tags are purely informational —
they exist so the clinician can see who to call, but the contact has
no access to the platform.

When the manager promotes a contact to a *delegate* (e.g. a son who
manages his father's medical records), this service:

1. Creates a :class:`Grant` with ``resource_kind="patient"`` so the
   delegate can act on the whole fascicolo (studies, folders, reports,
   consultations).
2. Creates a :class:`ShareLink` in ``mode="claim"`` so the delegate
   receives a magic link and can claim a real account on first visit.
   After the claim the link's permissions stay attached to the new
   user — future writes flow through the normal user path.
3. Persists the resulting ``subject_id`` + ``share_link_id`` +
   ``delegation_level`` back into the contact's JSONB row so the UI
   can render the right state without an extra Grant lookup per
   contact.

Revocation deletes the Grant (which cascades to the ShareLink), then
clears the three delegation fields on the contact so the row falls
back to its informational-only state.

A contact's stable ID is auto-assigned the first time we touch it so
the UI can reference contacts by identity rather than list position.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import hash_password
from bvphoenix.db.models import Grant, Patient, ShareLink, User
from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID
from bvphoenix.services.access_levels import level_to_permissions
from bvphoenix.services.grants import resolve_deidentify_default

# Whitelist of access levels the delegation flow may use. Mirrors
# ``services.access_levels.LEVELS`` but rejects "custom" so callers
# can't smuggle arbitrary permission sets through this entry point.
ALLOWED_LEVELS: frozenset[str] = frozenset({"viewer", "editor", "manager"})


class DelegationError(Exception):
    """Raised when a delegation request can't be satisfied (contact
    not found, level invalid, already delegated, etc.). The endpoint
    translates each subclass to the right HTTP status."""


class ContactNotFoundError(DelegationError):
    pass


class InvalidLevelError(DelegationError):
    pass


class AlreadyDelegatedError(DelegationError):
    pass


@dataclass
class DelegationResult:
    """Returned to the API so the response can show the operator the
    one-time password + the magic-link URL to deliver out of band.
    """

    contact_id: str
    subject_id: str
    share_link_id: str
    share_link_token: str
    generated_password: str | None
    delegation_level: str
    expires_at: datetime | None
    # Whether the contact's address already belonged to an account. When
    # it did, the grant went straight onto that subject and the
    # recipient signs in as usual; when it did not, the grant sits on
    # PUBLIC and the link is the only way in until they claim it (or
    # register and verify the same address, at which point
    # ``services.invitations`` attaches it).
    recipient_has_account: bool = False
    recipient_email: str | None = None


def _autogen_password(length: int = 24) -> str:
    """Mirror of ``api/sharing._autogen_password``: ~118 bits of entropy
    in a URL-safe alphabet, returned to the caller exactly once."""
    return secrets.token_urlsafe(length)[:length]


async def _resolve_or_create_grantee(
    db: AsyncSession, *, email: str | None
) -> tuple[uuid.UUID, bool]:
    """Find an existing User by email or fall back to PUBLIC_SUBJECT_ID.

    Returns ``(subject_id, attached_to_existing_user)``. We don't
    pre-provision an account here — the magic-link claim flow will
    mint a real user when the recipient first visits the link, and
    the ``share_links.claimed_by_subject_id`` field captures the
    binding at that point. Until then ``grantee_subject_id`` points at
    the public subject so the link itself is the credential.
    """
    if not email:
        return PUBLIC_SUBJECT_ID, False
    target = (
        await db.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if target:
        return target.subject_id, True
    return PUBLIC_SUBJECT_ID, False


async def promote_contact_to_delegate(
    db: AsyncSession,
    *,
    patient: Patient,
    contact_id: str,
    user: User,
    access_level: str,
    expires_in_hours: int | None,
    autogen_password: bool,
    explicit_password: str | None,
) -> DelegationResult:
    """Wrap an existing contact in a Grant + ShareLink and persist
    the delegation pointers on the contact row.

    Post-alembic 0071 the contact is a row in ``patient_contacts``
    (1:N), not a JSONB entry. The delegation pointers are FK columns
    on that row (``delegation_grant_id``, ``delegation_share_link_id``,
    ``delegation_subject_id``, ``delegation_level``) so dropping the
    grant cascades cleanly via ON DELETE SET NULL.

    Idempotency: if the contact already carries an active grant we
    raise ``AlreadyDelegatedError`` rather than silently leak a second
    one. Revoke first to change the level.
    """
    from bvphoenix.db.models import PatientContact

    if access_level not in ALLOWED_LEVELS:
        raise InvalidLevelError(f"access_level must be one of {sorted(ALLOWED_LEVELS)}")

    try:
        cid_uuid = uuid.UUID(contact_id)
    except (TypeError, ValueError) as exc:
        raise ContactNotFoundError(f"contact {contact_id} not found") from exc

    row = (
        await db.execute(
            select(PatientContact).where(
                PatientContact.id == cid_uuid,
                PatientContact.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ContactNotFoundError(f"contact {contact_id} not found on patient")

    if row.delegation_grant_id is not None or row.delegation_share_link_id is not None:
        raise AlreadyDelegatedError(
            "contact already has an active delegation; revoke it first "
            "if you need to change the level"
        )

    valid_until = None
    if expires_in_hours is not None:
        valid_until = datetime.now(UTC) + timedelta(hours=expires_in_hours)

    perms = level_to_permissions(access_level, download=True)

    grantee_subject_id, recipient_has_account = await _resolve_or_create_grantee(
        db, email=row.email
    )

    deidentify = await resolve_deidentify_default(
        db,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_subject_id,
        explicit=False,
    )

    label = row.label or "contact"
    relationship = row.relationship_ or ""
    purpose = f"family delegation: {label}" + (f" ({relationship})" if relationship else "")

    grant = Grant(
        resource_kind="patient",
        resource_id=patient.id,
        grantor_subject_id=user.subject_id,
        grantee_subject_id=grantee_subject_id,
        permissions=perms,
        conditions={"scope": "delegation"},
        valid_until=valid_until,
        deidentify=deidentify,
        purpose=purpose,
    )
    db.add(grant)
    await db.flush()

    plaintext_password: str | None = explicit_password
    generated_password: str | None = None
    if autogen_password and not explicit_password:
        generated_password = _autogen_password()
        plaintext_password = generated_password

    link = ShareLink(
        grant_id=grant.id,
        token=secrets.token_urlsafe(32),
        password_hash=(hash_password(plaintext_password) if plaintext_password else None),
        label=f"Delega — {label}",
        max_uses=None,
        mode="claim",
        recipient_name=label,
        recipient_email=(row.email or "").strip().lower() or None,
        recipient_phone=(row.phone or "").strip() or None,
    )
    db.add(link)
    await db.flush()

    row.delegation_subject_id = grantee_subject_id
    row.delegation_share_link_id = link.id
    row.delegation_grant_id = grant.id
    row.delegation_level = access_level
    await db.flush()
    await db.commit()

    return DelegationResult(
        contact_id=str(row.id),
        subject_id=str(grantee_subject_id),
        share_link_id=str(link.id),
        share_link_token=link.token,
        generated_password=generated_password,
        delegation_level=access_level,
        expires_at=valid_until,
        recipient_has_account=recipient_has_account,
        recipient_email=link.recipient_email,
    )


async def revoke_contact_delegation(
    db: AsyncSession,
    *,
    patient: Patient,
    contact_id: str,
) -> None:
    """Drop the Grant attached to this contact and clear the
    delegation pointers on the row.

    Idempotent: if the contact has no active delegation we no-op
    instead of raising. Post-0071 the pointers are FK columns on
    ``patient_contacts``; we drop the Grant explicitly (the FKs use
    ``ON DELETE SET NULL`` so the row survives, just goes back to
    informational state).
    """
    from bvphoenix.db.models import PatientContact

    try:
        cid_uuid = uuid.UUID(contact_id)
    except (TypeError, ValueError) as exc:
        raise ContactNotFoundError(f"contact {contact_id} not found") from exc

    row = (
        await db.execute(
            select(PatientContact).where(
                PatientContact.id == cid_uuid,
                PatientContact.patient_id == patient.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ContactNotFoundError(f"contact {contact_id} not found on patient")

    grant_id = row.delegation_grant_id
    if grant_id is None:
        return  # already informational

    await db.execute(delete(Grant).where(Grant.id == grant_id))

    row.delegation_subject_id = None
    row.delegation_share_link_id = None
    row.delegation_grant_id = None
    row.delegation_level = None
    await db.flush()
    await db.commit()


def assign_missing_contact_ids(contacts: list[dict[str, Any]]) -> bool:
    """Walk the contact list and assign UUIDs to any entry without one.

    Returns ``True`` if at least one entry was mutated, so callers can
    decide whether to issue an UPDATE. Used by the patient
    create/update endpoints to guarantee every persisted contact has
    a stable id from day one.
    """
    mutated = False
    for entry in contacts:
        if not entry.get("id"):
            entry["id"] = str(uuid.uuid4())
            mutated = True
    return mutated
