"""Study acquisition-phase manifest API end-to-end (real Postgres).

GET returns the persisted manifest (initially unclassified); POST /detect
runs the classifier and persists auto labels; a subsequent GET reflects
them. Ownership + 404-on-no-access are exercised too.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Series, User
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


async def _add_series(db: AsyncSession, study_id: uuid.UUID, desc: str, number: int) -> Series:
    s = Series(
        id=uuid.uuid4(),
        study_id=study_id,
        series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        series_number=number,
        modality="CT",
        body_part_examined="LIVER",
        series_description=desc,
    )
    db.add(s)
    await db.flush()
    return s


async def test_phases_manifest_and_detect(db_session, make_user, make_study) -> None:
    owner = await make_user()
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="Pre-contrast"
    )
    s0.series_number = 1
    await _add_series(db_session, study.id, "Late arterial phase", 2)
    await _add_series(db_session, study.id, "Portal venous phase", 3)
    await db_session.commit()

    try:
        async with _client_as(db_session, owner) as client:
            # 1) Manifest before detection: 3 series, all unclassified.
            r = await client.get(f"/api/studies/{study.id}/phases")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["study_id"] == str(study.id)
            assert len(body["phases"]) == 3
            assert all(p["acquisition_phase"] is None for p in body["phases"])

            # 2) Detect: classifier runs + persists.
            r = await client.post(f"/api/studies/{study.id}/phases/detect")
            assert r.status_code == 200, r.text
            labels = {p["series_description"]: p["acquisition_phase"] for p in r.json()["phases"]}
            assert labels["Pre-contrast"] == "unenhanced"
            assert labels["Late arterial phase"] == "arterial"
            assert labels["Portal venous phase"] == "portal_venous"
            assert all(
                p["phase_source"] == "auto"
                for p in r.json()["phases"]
                if p["acquisition_phase"] is not None
            )

            # 3) A subsequent GET reflects the persisted labels.
            r = await client.get(f"/api/studies/{study.id}/phases")
            assert r.status_code == 200
            labels2 = {p["series_description"]: p["acquisition_phase"] for p in r.json()["phases"]}
            assert labels2 == labels
    finally:
        app.dependency_overrides.clear()


async def test_phase_human_override(db_session, make_user, make_study) -> None:
    owner = await make_user()
    study, _s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="Late arterial phase"
    )
    await db_session.commit()
    try:
        async with _client_as(db_session, owner) as client:
            # Auto-classify -> arterial.
            r = await client.post(f"/api/studies/{study.id}/phases/detect")
            assert r.status_code == 200, r.text
            sid = r.json()["phases"][0]["series_id"]
            assert r.json()["phases"][0]["acquisition_phase"] == "arterial"

            # Human pins it to 'delayed'.
            r = await client.patch(
                f"/api/series/{sid}/acquisition-phase",
                json={"acquisition_phase": "delayed"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["acquisition_phase"] == "delayed"
            assert r.json()["phase_source"] == "human"
            assert r.json()["needs_confirmation"] is False

            # Re-detect WITHOUT force must NOT clobber the human label.
            r = await client.post(f"/api/studies/{study.id}/phases/detect")
            assert r.status_code == 200
            assert r.json()["phases"][0]["acquisition_phase"] == "delayed"
            assert r.json()["phases"][0]["phase_source"] == "human"

            # dry_run validates but does not persist.
            r = await client.patch(
                f"/api/series/{sid}/acquisition-phase",
                json={"acquisition_phase": "portal_venous", "dry_run": True},
            )
            assert r.status_code == 200
            assert r.json()["acquisition_phase"] == "portal_venous"
            r = await client.get(f"/api/studies/{study.id}/phases")
            assert r.json()["phases"][0]["acquisition_phase"] == "delayed"  # unchanged

            # Invalid value -> 422.
            r = await client.patch(
                f"/api/series/{sid}/acquisition-phase",
                json={"acquisition_phase": "venous"},
            )
            assert r.status_code == 422, r.text

            # Clear (null) reverts to unclassified + auto-eligible.
            r = await client.patch(
                f"/api/series/{sid}/acquisition-phase",
                json={"acquisition_phase": None},
            )
            assert r.status_code == 200
            assert r.json()["acquisition_phase"] is None
            assert r.json()["phase_source"] is None
    finally:
        app.dependency_overrides.clear()


async def test_phase_roi_stats_validation_and_empty(db_session, make_user, make_study) -> None:
    owner = await make_user()
    study, _s0 = await make_study(owner, modality="CT", body_part="LIVER")
    await db_session.commit()
    try:
        async with _client_as(db_session, owner) as client:
            # Malformed ROI -> 422 (before any S3 access).
            r = await client.post(
                f"/api/studies/{study.id}/phase-roi-stats", json={"kind": "sphere"}
            )
            assert r.status_code == 422, r.text
            r = await client.post(
                f"/api/studies/{study.id}/phase-roi-stats", json={"kind": "bogus"}
            )
            assert r.status_code == 422, r.text

            # No classified CT phase yet -> 200 with empty samples + a washout
            # whose indices are all None (nothing to sample; no S3 touched).
            r = await client.post(
                f"/api/studies/{study.id}/phase-roi-stats",
                json={"kind": "sphere", "center_lps": [0, 0, 0], "radius_mm": 10},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["samples"] == []
            assert body["washout"]["apw"] is None
            assert body["washout"]["rpw"] is None
            assert body["washout"]["curve"] == []
    finally:
        app.dependency_overrides.clear()


async def test_phase_roi_stats_404_for_non_owner(db_session, make_user, make_study) -> None:
    owner = await make_user()
    other = await make_user()
    study, _s0 = await make_study(owner, modality="CT", body_part="LIVER")
    await db_session.commit()
    try:
        async with _client_as(db_session, other) as client:
            r = await client.post(
                f"/api/studies/{study.id}/phase-roi-stats",
                json={"kind": "sphere", "center_lps": [0, 0, 0], "radius_mm": 10},
            )
            assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.clear()


async def test_phases_manifest_404_for_non_owner(db_session, make_user, make_study) -> None:
    owner = await make_user()
    other = await make_user()
    study, _s0 = await make_study(owner, modality="CT", body_part="LIVER")
    await db_session.commit()
    try:
        async with _client_as(db_session, other) as client:
            r = await client.get(f"/api/studies/{study.id}/phases")
            # Storage isolation: no-access is a 404, not a 403 (no info leak).
            assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.clear()
