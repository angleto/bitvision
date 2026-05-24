"""Grant policy helpers.

Today's job is narrow: decide whether a new grant should default to
de-identifying DICOM hand-offs when the caller did not supply an
explicit choice. ``docs/authorization.md`` §7 locks the rule down:

    Anonymisation for external grants = ON by default.
    Any grant to a principal not in the owner's orgs ships with
    ``conditions.anonymized = true`` unless the grantor explicitly
    disables it.

The column on :class:`bvphoenix.db.models.Grant` still defaults to
``False`` at the schema level because the *storage* default has to be
policy-agnostic (internal grants shouldn't pay the deid tax). Enforcing
the external-default at the service layer keeps the DB honest while
letting the API do the right thing.

The heuristic for "internal vs external":

* The public subject (``link_public`` share, anonymous access) is
  always external.
* A grantee that is an organisation the grantor belongs to is internal.
* A grantee that is a user sharing at least one org with the grantor is
  internal.
* Anything else (other orgs, unrelated users, groups outside the
  grantor's orgs) is external and defaults to de-identify = True.

The membership query is cheap and indexed (``ix_memberships_parent``);
the helper is safe to call on every share creation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models.sharing import PUBLIC_SUBJECT_ID


async def is_external_grantee(
    db: AsyncSession,
    *,
    grantor_subject_id: uuid.UUID,
    grantee_subject_id: uuid.UUID,
) -> bool:
    """Return True when ``grantee`` is outside ``grantor``'s org circle.

    The public subject is always external. Otherwise we look for a
    shared-org edge in ``memberships`` — either the grantee IS an org
    the grantor belongs to, or the two principals co-member at least
    one org.

    Conservative on the ambiguous case: if no evidence of a shared org
    surfaces, we treat the grantee as external. A false-positive
    "external" ruling costs a PHI scrub the user could have skipped; a
    false-negative leaks identifiers, and authorization.md explicitly
    designs the system for the former failure mode.
    """
    if grantee_subject_id == PUBLIC_SUBJECT_ID:
        return True
    if grantor_subject_id == grantee_subject_id:
        # Self-grant is a rare edge case (owner recasting access for
        # their own principal). Treat it as internal.
        return False

    # One round-trip answers both "is grantee an org I'm in?" and "do
    # we co-member any org?". The UNION lets Postgres short-circuit as
    # soon as any match lands, which is what the LIMIT 1 enforces.
    row = (
        await db.execute(
            text(
                """
                SELECT 1
                FROM memberships
                WHERE subject_id = :grantor
                  AND parent_subject_id = :grantee
                UNION
                SELECT 1
                FROM memberships m1
                JOIN memberships m2
                  ON m1.parent_subject_id = m2.parent_subject_id
                WHERE m1.subject_id = :grantor
                  AND m2.subject_id = :grantee
                LIMIT 1
                """
            ),
            {"grantor": grantor_subject_id, "grantee": grantee_subject_id},
        )
    ).first()
    return row is None


async def resolve_deidentify_default(
    db: AsyncSession,
    *,
    grantor_subject_id: uuid.UUID,
    grantee_subject_id: uuid.UUID,
    explicit: bool | None,
) -> bool:
    """Return the effective ``deidentify`` flag for a new grant.

    When the caller passed an explicit boolean, honour it verbatim —
    authorization.md gives the grantor the final word. When the caller
    did not pick, external grants get de-identify = True and internal
    grants get False.
    """
    if explicit is not None:
        return explicit
    return await is_external_grantee(
        db,
        grantor_subject_id=grantor_subject_id,
        grantee_subject_id=grantee_subject_id,
    )


__all__ = ["is_external_grantee", "resolve_deidentify_default"]
