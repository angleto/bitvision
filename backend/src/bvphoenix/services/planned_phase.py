"""Singleton ``planned`` CarePhase per patient.

The calendar workflow assigns every future event
(``event_status IN ('planned','confirmed')``) to a dedicated phase
so the timeline shows them grouped under "Pianificati" instead of
in the historical clinical phases. The phase is created on demand
by :func:`ensure_planned_phase`; subsequent events on the same
patient pick it up via the get-or-create lookup.

Transition rules (enforced by the API handlers, not by this module):

- create / reschedule with ``planned``/``confirmed`` -> auto-assign.
- confirm transition keeps phase_id (still in the future bucket).
- complete / cancel / mark_missed / reschedule[old] -> clear
  ``phase_id`` so the row is no longer pinned to the planned phase
  (the LLM classifier or a human can then place it in the proper
  clinical phase).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import CarePhase

PLANNED_PHASE_SLUG = "planned"
PLANNED_PHASE_KIND = "planned"
# High ordinal so the planned phase sorts to the *bottom* of a desc
# care-timeline (the conventional rendering puts the most recent / in-
# progress phase last; planned events live in the future and belong
# right at the end of the time axis).
PLANNED_PHASE_ORDINAL = 9999


async def ensure_planned_phase(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    author_kind: str = "system",
) -> CarePhase:
    """Return the singleton planned CarePhase for ``patient_id``,
    creating it if missing. Safe to call concurrently: a future
    UNIQUE(patient_id, slug) constraint on ``care_phase`` (already
    enforced) makes a second concurrent insert fail with
    IntegrityError, which we treat as "another writer won the race"
    and retry the lookup."""
    row = (
        await db.execute(
            select(CarePhase).where(
                CarePhase.patient_id == patient_id,
                CarePhase.slug == PLANNED_PHASE_SLUG,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    phase = CarePhase(
        patient_id=patient_id,
        slug=PLANNED_PHASE_SLUG,
        name="Pianificati",
        name_i18n={"it": "Pianificati", "en": "Planned"},
        kind=PLANNED_PHASE_KIND,
        color_hex="#5b8def",
        ordinal=PLANNED_PHASE_ORDINAL,
        author_kind=author_kind,
        narrative_md=(
            "Bucket automatico per appuntamenti pianificati e confermati. "
            "Gli eventi entrano qui alla creazione (status=planned/confirmed) "
            "ed escono al completamento, annullamento o no-show."
        ),
    )
    db.add(phase)
    await db.flush()
    return phase


__all__ = [
    "PLANNED_PHASE_KIND",
    "PLANNED_PHASE_ORDINAL",
    "PLANNED_PHASE_SLUG",
    "ensure_planned_phase",
]
