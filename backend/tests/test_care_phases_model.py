"""Direct DB tests for the care-phase model and the cross-patient guard.

The composite FK ``(patient_id, phase_id) → care_phase (patient_id, id)``
is the structural guarantee that an event of patient A can never be
assigned to a phase of patient B. These tests exercise that guarantee
at the DDL boundary, not via the API: if Postgres ever rejects the
constraint silently or a future migration weakens it, these tests will
catch it.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import CarePhase, ClinicalEvent, Patient, Subject
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


def _new_phase(patient_id: uuid.UUID, slug: str, ordinal: int = 0) -> CarePhase:
    return CarePhase(
        patient_id=patient_id,
        slug=slug,
        name=slug,
        name_i18n={"it": slug, "en": slug},
        kind="imaging",
        color_hex="#185FA5",
        ordinal=ordinal,
        author_kind="human",
    )


def _new_event(patient_id: uuid.UUID, title: str = "test") -> ClinicalEvent:
    return ClinicalEvent(
        patient_id=patient_id,
        kind="other",
        title=title,
        event_date=date(2026, 1, 1),
    )


@pytest_asyncio.fixture
async def two_patients(db_session: AsyncSession):
    """Two patients owned by the same throwaway subject.

    Avoids the ``make_user`` fixture (which commits on teardown and
    therefore conflicts with the rollback in ``db_session``); we
    flush only and let the outer rollback drop everything.
    """
    sid = uuid.uuid4()
    db_session.add(Subject(id=sid, kind="user", display_name=f"sub-{sid}"))
    await db_session.flush()
    pa = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=sid,
        display_name="Patient A",
    )
    pb = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=sid,
        display_name="Patient B",
    )
    db_session.add_all([pa, pb])
    await db_session.flush()
    return pa, pb


async def test_care_phase_create_and_unique_slug(db_session: AsyncSession, two_patients):
    pa, _pb = two_patients

    db_session.add(_new_phase(pa.id, "imaging-pre-op"))
    await db_session.flush()

    # Same slug on the SAME patient → IntegrityError.
    db_session.add(_new_phase(pa.id, "imaging-pre-op", ordinal=1))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_assign_event_to_same_patient_phase_ok(db_session: AsyncSession, two_patients):
    pa, _pb = two_patients
    phase = _new_phase(pa.id, "imaging-pre-op")
    event = _new_event(pa.id, "OK assignment")
    db_session.add_all([phase, event])
    await db_session.flush()

    event.phase_id = phase.id
    await db_session.flush()
    # Read back to confirm the FK accepted the assignment.
    refreshed = (
        await db_session.execute(select(ClinicalEvent).where(ClinicalEvent.id == event.id))
    ).scalar_one()
    assert refreshed.phase_id == phase.id


async def test_cross_patient_assignment_rejected_by_raw_update(
    db_session: AsyncSession, two_patients
):
    """Same invariant via a raw UPDATE statement (bypasses the ORM).

    Defends against ORM-only checks: even hand-crafted SQL cannot
    smuggle a cross-patient assignment past the composite FK.
    """
    pa, pb = two_patients
    phase_b = _new_phase(pb.id, "followup-post-op")
    event_a = _new_event(pa.id, "raw smuggling attempt")
    db_session.add_all([phase_b, event_a])
    await db_session.flush()

    from sqlalchemy import text

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE clinical_events SET phase_id = :pid WHERE id = :eid"),
            {"pid": phase_b.id, "eid": event_a.id},
        )
        await db_session.flush()
    await db_session.rollback()


# NOTE: tests that hit a CHECK / FK violation via the ORM (e.g. the
# color_hex regex check, the ORM-level cross-patient phase_id
# assignment, the same-slug-different-patient happy path) are also
# valid invariants but exercising them in pytest_asyncio currently
# trips the asyncpg connection lifecycle on teardown ("Event loop is
# closed"). Tracked separately; the raw-SQL test above already proves
# the composite FK is enforced at the DB level, which is the load-
# bearing guarantee for the cross-patient invariant.
