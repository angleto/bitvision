"""Cross-patient security for the longitudinal-comparison write paths.

A propagation or a response assessment must never reach across patients —
the API rejects (422) a follow-up series or a study that belongs to a
different patient, before any worker runs or any record is written.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import FindingType, LesionTrack, User
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


def _client_as(session: AsyncSession, user: User | None) -> AsyncClient:
    async def _db():
        yield session

    async def _usr():
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _usr
    app.dependency_overrides[optional_user] = _usr
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_propagate_cross_patient_followup_rejected(db_session, make_user, make_study) -> None:
    owner = await make_user()
    study_a, _ = await make_study(owner, description="patient A", study_date=date(2026, 1, 1))
    patient_a = study_a.patient_id
    _study_b, series_b = await make_study(
        owner, description="patient B", study_date=date(2026, 1, 1)
    )
    ftype = (
        await db_session.execute(select(FindingType.id).where(FindingType.key == "nodule"))
    ).scalar_one()
    track = LesionTrack(
        patient_id=patient_a,
        label="A lesion",
        finding_type_id=ftype,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db_session.add(track)
    await db_session.commit()
    try:
        async with _client_as(db_session, owner) as client:
            r = await client.post(
                f"/api/lesion-tracks/{track.id}/propagate",
                json={"followup_series_id": str(series_b.id)},
            )
        assert r.status_code == 422, r.text
        assert "patient" in r.text.lower()
    finally:
        app.dependency_overrides.clear()
        await db_session.execute(
            text("DELETE FROM lesion_tracks WHERE patient_id = :p"), {"p": str(patient_a)}
        )
        await db_session.commit()


async def test_response_assessment_cross_patient_study_rejected(
    db_session, make_user, make_study
) -> None:
    owner = await make_user()
    study_a, _ = await make_study(owner, description="patient A", study_date=date(2026, 1, 1))
    patient_a = study_a.patient_id
    study_b, _ = await make_study(owner, description="patient B", study_date=date(2026, 4, 1))
    try:
        async with _client_as(db_session, owner) as client:
            r = await client.post(
                f"/api/patients/{patient_a}/response-assessments",
                json={"current_study_id": str(study_b.id)},
            )
        assert r.status_code == 422, r.text
        assert "patient" in r.text.lower()
    finally:
        app.dependency_overrides.clear()
        await db_session.execute(
            text("DELETE FROM response_assessments WHERE patient_id = :p"), {"p": str(patient_a)}
        )
        await db_session.commit()
