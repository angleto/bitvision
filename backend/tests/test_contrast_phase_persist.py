"""DB orchestration of the contrast-phase classifier
(``services.contrast_phase_classify.classify_and_persist_study``).

Pins the two persistence invariants:
* auto labels land on the series rows (phase_source='auto');
* a human override is never clobbered by re-detection unless force=True.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
import pytest_asyncio
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ImagingStudy, Patient, Series, Subject, User
from bvphoenix.services.contrast_phase_classify import classify_and_persist_study
from tests.conftest import skip_if_no_db


@pytest_asyncio.fixture
async def owner(
    db_session: AsyncSession, make_user: Callable[..., Awaitable[User]]
) -> tuple[User, Subject]:
    user = await make_user(email=f"owner-{uuid.uuid4()}@example.com")
    subject = (
        await db_session.execute(select(Subject).where(Subject.id == user.subject_id))
    ).scalar_one()
    return user, subject


async def _seed_study(
    db_session: AsyncSession, user: User, descriptions: list[str]
) -> tuple[ImagingStudy, list[Series]]:
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=user.subject_id,
        display_name="Phase Patient",
    )
    db_session.add(patient)
    await db_session.flush()
    study = ImagingStudy(
        id=uuid.uuid4(),
        patient_id=patient.id,
        study_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        owner_subject_id=user.subject_id,
        modalities=["CT"],
        study_description="CT abdomen multiphase",
    )
    db_session.add(study)
    await db_session.flush()
    series: list[Series] = []
    for i, desc in enumerate(descriptions, start=1):
        s = Series(
            id=uuid.uuid4(),
            study_id=study.id,
            series_instance_uid=generate_uid(),
            series_number=i,
            modality="CT",
            body_part_examined="LIVER",
            series_description=desc,
        )
        db_session.add(s)
        series.append(s)
    await db_session.flush()
    return study, series


@skip_if_no_db
@pytest.mark.asyncio
async def test_classify_and_persist_sets_auto_labels(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    user, _ = owner
    study, series = await _seed_study(
        db_session, user, ["Pre-contrast", "Late arterial phase", "Portal venous phase"]
    )
    await classify_and_persist_study(db_session, study.id)

    for s in series:
        await db_session.refresh(s)
    assert series[0].acquisition_phase == "unenhanced"
    assert series[1].acquisition_phase == "arterial"
    assert series[2].acquisition_phase == "portal_venous"
    for s in series:
        assert s.phase_source == "auto"
        assert s.phase_confidence is not None

    await db_session.rollback()


@skip_if_no_db
@pytest.mark.asyncio
async def test_human_override_is_not_clobbered(
    db_session: AsyncSession, owner: tuple[User, Subject]
) -> None:
    user, _ = owner
    study, series = await _seed_study(
        db_session, user, ["Late arterial phase", "Portal venous phase"]
    )
    # A clinician pins series[0] to 'delayed' (disagreeing with the text).
    series[0].acquisition_phase = "delayed"
    series[0].phase_source = "human"
    series[0].phase_confidence = None
    await db_session.flush()

    # Re-detect WITHOUT force: the human label must survive.
    await classify_and_persist_study(db_session, study.id)
    await db_session.refresh(series[0])
    await db_session.refresh(series[1])
    assert series[0].acquisition_phase == "delayed"
    assert series[0].phase_source == "human"
    assert series[1].acquisition_phase == "portal_venous"
    assert series[1].phase_source == "auto"

    # Re-detect WITH force: the auto-classifier overwrites even the human label.
    await classify_and_persist_study(db_session, study.id, force=True)
    await db_session.refresh(series[0])
    assert series[0].acquisition_phase == "arterial"
    assert series[0].phase_source == "auto"

    await db_session.rollback()
