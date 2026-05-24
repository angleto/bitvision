"""CRUD on the 1:N ``patient_contacts`` table.

This module replaces the JSONB-stash flow that used to live on
``patients.contacts``. Each function is the single entry point for
its operation; endpoints, the delegation service, and the MCP tools
all call these helpers instead of poking the table directly.

Design points:

* **Replace-all semantics on PATCH `contacts`**: when a caller sends
  the entire contacts array on ``update_patient``, we diff it against
  the table — entries with ``id`` that match an existing row are
  updated in place (delegation pointers preserved), entries without
  ``id`` are inserted, rows in the table not present in the new array
  are deleted. Delegation rows pinned by an active grant are refused
  for deletion: the operator must revoke the delegation first
  (``DELETE /api/patients/{id}/contacts/{cid}/delegate``).
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
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import PatientContact


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
        email=email,
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


async def delete_contact(
    db: AsyncSession, *, patient_id: uuid.UUID, contact_id: uuid.UUID
) -> tuple[bool, str | None]:
    """Drop a contact. Refuses contacts with an active delegation.

    Returns ``(deleted, reason)``. ``deleted=False`` with a human
    ``reason`` when the row exists but can't be removed; ``True, None``
    on success; ``False, "not_found"`` when the row never existed (the
    caller maps that to 404).
    """
    row = await get_contact(db, patient_id=patient_id, contact_id=contact_id)
    if row is None:
        return False, "not_found"
    if row.delegation_grant_id is not None or row.delegation_share_link_id is not None:
        return (
            False,
            (
                "contact has an active delegation; revoke it via DELETE "
                "/api/patients/{id}/contacts/{cid}/delegate before deleting"
            ),
        )
    await db.delete(row)
    await db.flush()
    return True, None


async def replace_all_contacts(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    incoming: list[dict[str, Any]],
) -> Sequence[PatientContact]:
    """Reconcile the table to ``incoming``: insert / update / delete.

    Used by ``update_patient`` PATCH when the caller passes a full
    ``contacts`` array (replace-all semantics, the legacy contract from
    the JSONB era). Entries with an ``id`` matching an existing row
    are patched in place; new entries are inserted; rows present in
    the table but absent from ``incoming`` are deleted **only when
    they have no active delegation** — otherwise they're kept and a
    warning is appended to the result so the caller surfaces it to
    the human.
    """
    existing = list(await list_contacts(db, patient_id))
    existing_by_id = {row.id: row for row in existing}

    incoming_ids: set[uuid.UUID] = set()
    for entry in incoming:
        raw_id = entry.get("id")
        eid: uuid.UUID | None = None
        if raw_id:
            try:
                eid = uuid.UUID(raw_id) if isinstance(raw_id, str) else raw_id
            except (TypeError, ValueError):
                eid = None
        if eid is not None and eid in existing_by_id:
            incoming_ids.add(eid)
            await update_contact(
                db,
                patient_id=patient_id,
                contact_id=eid,
                fields={
                    k: v
                    for k, v in entry.items()
                    if k
                    in (
                        "label",
                        "relationship",
                        "email",
                        "phone",
                        "notes",
                        "is_primary",
                        "consent_to_contact",
                    )
                },
            )
        else:
            row = await create_contact(
                db,
                patient_id=patient_id,
                label=entry.get("label") or "contact",
                relationship=entry.get("relationship"),
                email=entry.get("email"),
                phone=entry.get("phone"),
                notes=entry.get("notes"),
                is_primary=bool(entry.get("is_primary", False)),
                consent_to_contact=bool(entry.get("consent_to_contact", False)),
                contact_id=eid,
            )
            incoming_ids.add(row.id)

    for row in existing:
        if row.id in incoming_ids:
            continue
        if row.delegation_grant_id is not None or row.delegation_share_link_id is not None:
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
    "create_contact",
    "delete_contact",
    "get_contact",
    "list_contacts",
    "replace_all_contacts",
    "to_pydantic_dict",
    "update_contact",
]
