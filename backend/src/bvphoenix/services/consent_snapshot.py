"""Consent-snapshot helpers for consultations.

A consultation is created against the *current* consent state of the
patient/user, but downstream audits (Art. 7(1) GDPR: proof of consent at
the moment of processing) need to reconstruct what was granted when the
consultation was opened — not what is granted now. Storing the snapshot
as a JSONB blob on the consultation row makes this invariant historical:
later revocations do not rewrite the past.

The snapshot shape is intentionally narrow — just what an auditor or a
downstream LLM pipeline needs to decide whether a processing operation
was lawful at creation time:

.. code-block:: json

    {
      "taken_at": "2026-04-17T12:34:56+00:00",
      "consents": [
        {"kind": "research_use", "granted_at": "...", "revoked_at": null},
        ...
      ],
      "has_research_consent": true,
      "has_llm_consult_consent": true
    }

``has_*`` booleans are derived from the ``consents`` array — they exist
so hot-path checks do not have to re-scan the list. The pair is always
self-consistent: ``has_X`` is true iff there is at least one consent
entry with matching ``kind`` whose ``revoked_at`` is ``null``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Consent

# ``research_use`` is in ``CONSENT_KINDS`` (docs/security-gdpr.md §3).
# ``llm_consult`` is the consultation-specific kind added in the
# fascicolo workstream; if the column has not yet been widened to
# include it, the snapshot records the absence of a grant.
_RESEARCH_KIND = "research_use"
_LLM_CONSULT_KIND = "llm_consult"


async def build_consent_snapshot(db: AsyncSession, user_subject_id: uuid.UUID) -> dict[str, Any]:
    """Build a JSONB-serialisable snapshot of consent state for a user.

    Returns a freshly constructed dict (no ORM objects) so the caller
    can assign it directly to a ``JSONB`` column. ``datetime`` values
    are rendered as ISO-8601 strings — the same shape the GDPR export
    uses.
    """
    rows = (
        (
            await db.execute(
                select(Consent)
                .where(Consent.user_subject_id == user_subject_id)
                .order_by(Consent.kind, Consent.granted_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # Collapse history → latest row per kind. Ordering above guarantees
    # the first row per kind is the most recent.
    latest: dict[str, Consent] = {}
    for row in rows:
        latest.setdefault(row.kind, row)

    consents: list[dict[str, Any]] = []
    has_research = False
    has_llm_consult = False
    for kind, row in latest.items():
        active = row.revoked_at is None
        consents.append(
            {
                "kind": kind,
                "granted_at": row.granted_at.isoformat(),
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            }
        )
        if active and kind == _RESEARCH_KIND:
            has_research = True
        elif active and kind == _LLM_CONSULT_KIND:
            has_llm_consult = True

    return {
        "taken_at": datetime.now(UTC).isoformat(),
        "consents": consents,
        "has_research_consent": has_research,
        "has_llm_consult_consent": has_llm_consult,
    }
