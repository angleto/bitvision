"""CRUD on the 1:N ``patient_contacts`` table.

This module replaces the JSONB-stash flow that used to live on
``patients.contacts``. Each function is the single entry point for
its operation; endpoints, the delegation service, and the MCP tools
all call these helpers instead of poking the table directly.

Design points:

* **Replace-all semantics on PATCH `contacts`**: when a caller sends
  the entire contacts array on ``update_patient``, we diff it against
  the table — entries are matched to an existing row by ``id`` first
  and by email second, matched rows are updated in place (delegation
  pointers preserved), unmatched entries are inserted, and rows in the
  table not present in the new array are deleted. Delegation rows
  pinned by a live grant are refused for deletion: the operator must
  revoke the delegation first
  (``DELETE /api/patients/{id}/contacts/{cid}/delegate``).

  The email fallback is what makes this safe against a client that
  loses the ids. It is not hypothetical: the patient edit form rebuilt
  each row as ``{label, relationship, email, phone}`` and dropped the
  ``id``, so every save inserted a fresh row for every contact while
  the delegated originals were kept back from deletion. Five contacts
  on one production fascicolo became eight that way, and removing a
  duplicate through the same form produced two more. The client is
  fixed, ``uq_patient_contacts_patient_email`` (alembic 0049) makes
  the outcome impossible to store, and this fallback means a caller
  that still sends id-less entries reconciles instead of failing.
* **Single-primary invariant**: marking a contact ``is_primary=True``
  demotes the previous primary in the same transaction. The DB also
  enforces this with a partial unique index — the SET in this module
  exists so the writes don't fail with a constraint violation.
* **Pydantic round-trip**: ``to_pydantic_contact`` takes a row and
  returns the public ``PatientContact`` schema; the API layer never
  reaches into the row's columns directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Grant, PatientContact


class DuplicateContactEmailError(ValueError):
    """Two entries in one replace-all payload name the same mailbox.

    Folding them into one row would silently discard whichever the
    caller meant to keep, so the write is refused and the caller is
    told which address collided.
    """

    def __init__(self, email: str) -> None:
        super().__init__(f"two contacts in this payload share the address {email}")
        self.email = email


@dataclass(frozen=True)
class ContactDeletion:
    """Outcome of :func:`delete_contact`.

    ``code`` is the stable identifier the API surfaces; the prose that
    goes with it is resolved by the caller, never by this module.
    """

    deleted: bool
    code: str | None = None
    revoked_delegation: bool = False
    delegation_grant_id: uuid.UUID | None = None


def normalise_email(value: str | None) -> str | None:
    """Canonical form of a contact address: trimmed, lowercased, and
    empty collapsed to ``None``.

    Mirrors ``fn_normalise_email`` / ``trg_patient_contacts_normalise_email``
    (alembic 0048), which is what actually enforces this on the way into
    the table. The Python twin exists so that in-memory matching (the
    replace-all diff below) reaches the same answer as the database
    would, without a round-trip per entry.
    """
    if value is None:
        return None
    return value.strip().lower() or None


async def list_contacts(db: AsyncSession, patient_id: uuid.UUID) -> Sequence[PatientContact]:
    """Return every contact attached to a patient, primary first."""
    stmt = (
        select(PatientContact)
        .where(PatientContact.patient_id == patient_id)
        .order_by(PatientContact.is_primary.desc(), PatientContact.created_at.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def get_contact(
    db: AsyncSession, *, patient_id: uuid.UUID, contact_id: uuid.UUID
) -> PatientContact | None:
    return (
        await db.execute(
            select(PatientContact).where(
                PatientContact.patient_id == patient_id,
                PatientContact.id == contact_id,
            )
        )
    ).scalar_one_or_none()


async def _ensure_single_primary(
    db: AsyncSession, *, patient_id: uuid.UUID, except_id: uuid.UUID | None = None
) -> None:
    """Demote any other ``is_primary=True`` row for this patient.

    Called before INSERT / UPDATE that flips ``is_primary`` on. Doing
    the demote in the same transaction keeps the partial unique index
    happy (the index would reject the second TRUE otherwise).
    """
    stmt = update(PatientContact).where(
        PatientContact.patient_id == patient_id,
        PatientContact.is_primary.is_(True),
    )
    if except_id is not None:
        stmt = stmt.where(PatientContact.id != except_id)
    await db.execute(stmt.values(is_primary=False))


async def create_contact(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    label: str,
    relationship: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    is_primary: bool = False,
    consent_to_contact: bool = False,
    contact_id: uuid.UUID | None = None,
) -> PatientContact:
    """Insert one contact. Returns the row with its server-assigned id."""
    if is_primary:
        await _ensure_single_primary(db, patient_id=patient_id)
    row = PatientContact(
        id=contact_id or uuid.uuid4(),
        patient_id=patient_id,
        label=label,
        relationship_=relationship,
        # Canonicalised here as well as by ``trg_patient_contacts_normalise_email``
        # so the in-session object matches the stored row without a refresh.
        email=normalise_email(email),
        phone=phone,
        notes=notes,
        is_primary=is_primary,
        consent_to_contact=consent_to_contact,
    )
    db.add(row)
    await db.flush()
    return row


async def update_contact(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    fields: dict[str, Any],
) -> PatientContact | None:
    """Patch a contact in place. ``fields`` carries only the keys the
    caller wants to change; missing keys keep their stored value.

    Returns ``None`` when the row doesn't exist for that patient.
    """
    row = await get_contact(db, patient_id=patient_id, contact_id=contact_id)
    if row is None:
        return None

    if fields.get("is_primary") is True and not row.is_primary:
        await _ensure_single_primary(db, patient_id=patient_id, except_id=contact_id)

    # Map the public field name ``relationship`` to the column attr
    # ``relationship_`` (Python keyword clash).
    if "relationship" in fields:
        row.relationship_ = fields["relationship"]
        fields.pop("relationship")

    if "email" in fields:
        fields["email"] = normalise_email(fields["email"])

    for k in (
        "label",
        "email",
        "phone",
        "notes",
        "is_primary",
        "consent_to_contact",
    ):
        if k in fields:
            setattr(row, k, fields[k])
    await db.flush()
    return row


async def live_delegation_grant(db: AsyncSession, row: PatientContact) -> Grant | None:
    """The grant behind this contact's delegation, if it is still alive.

    "Alive" means present, not revoked, and not past ``valid_until``.
    Reading the pointer columns alone is not enough and used to be the
    bug: a grant revoked through any path other than
    ``revoke_contact_delegation`` left all four ``delegation_*`` columns
    populated, and :func:`delete_contact` read them as "still delegated"
    and refused forever — with nothing left to revoke. Alembic 0048 now
    clears the pointers from the datastore when a grant is revoked or
    deleted, so that state is no longer reachable; expiry is not a
    trigger-able event, which is why ``valid_until`` is still checked
    here rather than trusted to the schema.
    """
    if row.delegation_grant_id is None:
        return None
    grant = (
        await db.execute(select(Grant).where(Grant.id == row.delegation_grant_id))
    ).scalar_one_or_none()
    if grant is None or grant.revoked_at is not None:
        return None
    if grant.valid_until is not None and grant.valid_until <= datetime.now(UTC):
        return None
    return grant


async def delete_contact(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    contact_id: uuid.UUID,
    revoke_delegation: bool = False,
) -> ContactDeletion:
    """Drop a contact.

    A contact holding a *live* delegation is refused unless the caller
    asks for the delegation to go with it: removing the person from the
    fascicolo's contact list while they keep a working grant on it would
    make the access invisible from the only screen that shows it.
    ``revoke_delegation=True`` is the one flow that does both, and the
    caller is responsible for having asked a human first.

    Revocation drops the ``Grant``; the datastore trigger installed by
    alembic 0048 clears the contact's pointers as a consequence, so this
    function does not have to (and must not — the row is about to go).

    Flushes; the caller commits.
    """
    row = await get_contact(db, patient_id=patient_id, contact_id=contact_id)
    if row is None:
        return ContactDeletion(deleted=False, code="not_found")

    grant = await live_delegation_grant(db, row)
    revoked = False
    if grant is not None:
        if not revoke_delegation:
            return ContactDeletion(
                deleted=False,
                code="delegation_active",
                delegation_grant_id=grant.id,
            )
        await db.delete(grant)
        await db.flush()
        revoked = True

    await db.delete(row)
    await db.flush()
    return ContactDeletion(deleted=True, revoked_delegation=revoked)


async def replace_all_contacts(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    incoming: list[dict[str, Any]],
) -> Sequence[PatientContact]:
    """Reconcile the table to ``incoming``: insert / update / delete.

    Used by ``update_patient`` PATCH when the caller passes a full
    ``contacts`` array (replace-all semantics, the legacy contract from
    the JSONB era).

    Each entry is matched to an existing row **by ``id`` first, then by
    canonical email**. Matched rows are patched in place (delegation
    pointers, notification channels, consent flags and the RFC 8058
    opt-out token all survive); unmatched entries are inserted; rows
    absent from ``incoming`` are deleted unless they hold a live
    delegation, in which case they are kept and the caller is expected
    to revoke explicitly.

    The email fallback is load-bearing rather than defensive. Matching
    on ``id`` alone means a client that drops the ids — which the
    patient edit form did — inserts a second row for every contact it
    meant to keep, and the delegated originals are held back from
    deletion, so the list grows on every save. Matching on the address
    reconciles instead. ``uq_patient_contacts_patient_email`` (alembic
    0049) is the backstop that makes the duplicate unstorable at all.

    Raises :class:`DuplicateContactEmailError` when two entries in one
    payload name the same mailbox: silently folding them would discard
    whichever the caller meant to keep.
    """
    existing = list(await list_contacts(db, patient_id))
    existing_by_id = {row.id: row for row in existing}
    existing_by_email = {
        normalise_email(row.email): row for row in existing if normalise_email(row.email)
    }

    _MUTABLE = (
        "label",
        "relationship",
        "email",
        "phone",
        "notes",
        "is_primary",
        "consent_to_contact",
    )

    matched_ids: set[uuid.UUID] = set()
    seen_emails: set[str] = set()
    for entry in incoming:
        raw_id = entry.get("id")
        eid: uuid.UUID | None = None
        if raw_id:
            try:
                eid = uuid.UUID(raw_id) if isinstance(raw_id, str) else raw_id
            except (TypeError, ValueError):
                eid = None

        email = normalise_email(entry.get("email"))
        if email is not None:
            if email in seen_emails:
                raise DuplicateContactEmailError(email)
            seen_emails.add(email)

        target = existing_by_id.get(eid) if eid is not None else None
        if target is None and email is not None:
            candidate = existing_by_email.get(email)
            # Only fall back to the address when it has not already been
            # claimed by an earlier entry in this same payload.
            if candidate is not None and candidate.id not in matched_ids:
                target = candidate

        if target is not None:
            matched_ids.add(target.id)
            await update_contact(
                db,
                patient_id=patient_id,
                contact_id=target.id,
                fields={k: v for k, v in entry.items() if k in _MUTABLE},
            )
        else:
            row = await create_contact(
                db,
                patient_id=patient_id,
                label=entry.get("label") or "contact",
                relationship=entry.get("relationship"),
                email=email,
                phone=entry.get("phone"),
                notes=entry.get("notes"),
                is_primary=bool(entry.get("is_primary", False)),
                consent_to_contact=bool(entry.get("consent_to_contact", False)),
                contact_id=eid,
            )
            matched_ids.add(row.id)

    for row in existing:
        if row.id in matched_ids:
            continue
        if await live_delegation_grant(db, row) is not None:
            # Don't drop delegated rows silently. The caller should
            # revoke the delegation explicitly.
            continue
        await db.delete(row)

    await db.flush()
    return await list_contacts(db, patient_id)


def to_pydantic_dict(row: PatientContact) -> dict[str, Any]:
    """Project a row to the dict shape ``PatientContact`` (Pydantic)
    expects. Keeps the API surface stable across the JSONB → table
    migration: the public schema looks identical to before, only the
    storage layout changed.
    """
    return {
        "id": str(row.id),
        "label": row.label,
        "relationship": row.relationship_,
        "email": row.email,
        "phone": row.phone,
        "notes": row.notes,
        "is_primary": row.is_primary,
        "consent_to_contact": row.consent_to_contact,
        "delegation_subject_id": (
            str(row.delegation_subject_id) if row.delegation_subject_id else None
        ),
        "delegation_share_link_id": (
            str(row.delegation_share_link_id) if row.delegation_share_link_id else None
        ),
        "delegation_level": row.delegation_level,
    }


__all__ = [
    "ContactDeletion",
    "DuplicateContactEmailError",
    "create_contact",
    "delete_contact",
    "get_contact",
    "list_contacts",
    "live_delegation_grant",
    "normalise_email",
    "replace_all_contacts",
    "to_pydantic_dict",
    "update_contact",
]
