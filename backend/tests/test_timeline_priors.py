"""Regression for the 'Confronta con Prior' / ComparePriorButton bug.

The patient timeline mixes studies + reports + markers + documents, sorts
them all by date descending, and truncates to ``limit`` (default 50).
ComparePriorButton lists priors by fetching the timeline and keeping
``type == "study"`` items. For a data-rich patient whose imaging studies
are OLD (e.g. a years-ago bulk import) but which has many NEWER markers /
documents, the studies fall past the default 50-item window and are
dropped, so the UI shows "Nessun esame precedente." for a patient that
clearly has priors.

The fix is to request ``section="studies"``, which isolates studies from
the crowding. This test reproduces the crowd-out on the default page and
proves the section filter returns the studies regardless.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Marker, Patient
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from tests.conftest import skip_if_no_db


def _client_as(session: AsyncSession, user) -> AsyncClient:
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


@pytest.mark.asyncio
async def test_timeline_studies_survive_section_filter_when_crowded(
    db_session: AsyncSession, make_user, make_study
) -> None:
    skip_if_no_db()
    owner = await make_user()
    patient = Patient(
        id=uuid.uuid4(),
        managed_by_subject_id=owner.subject_id,
        display_name="data-rich prior patient",
    )
    db_session.add(patient)
    await db_session.flush()

    # Two OLD imaging studies (the priors the UI must surface).
    s1, _ = await make_study(
        owner, patient=patient, study_date=date(2006, 9, 20), description="baseline CT"
    )
    s2, _ = await make_study(
        owner, patient=patient, study_date=date(2006, 10, 24), description="follow-up CT"
    )

    # 51 NEWER markers (created_at defaults to now()) crowd the default
    # 50-item page so the old studies sort past the window.
    for _ in range(51):
        db_session.add(
            Marker(
                patient_id=patient.id,
                target_kind="study",
                target_id=s1.id,
                kind="measurement.distance",
                geometry={"axis": "axial", "points": [[1, 2, 3], [4, 5, 6]]},
                author_subject_id=owner.subject_id,
                author_kind="human",
            )
        )
    await db_session.flush()

    client = _client_as(db_session, owner)
    try:
        # Default page (limit 50, no section): the bug — studies crowded out.
        r = await client.get(f"/api/patients/{patient.id}/timeline")
        assert r.status_code == 200, r.text
        default_items = r.json()
        assert not any(it["type"] == "study" for it in default_items), (
            "expected the old studies to be crowded out of the default 50-item page"
        )

        # The fix: section='studies' isolates studies regardless of crowding.
        r2 = await client.get(f"/api/patients/{patient.id}/timeline", params={"section": "studies"})
        assert r2.status_code == 200, r2.text
        study_ids = {it["data"]["id"] for it in r2.json() if it["type"] == "study"}
        assert {str(s1.id), str(s2.id)} <= study_ids
    finally:
        await client.aclose()
        app.dependency_overrides.clear()
