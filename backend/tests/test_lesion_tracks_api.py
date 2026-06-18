"""Lesion-tracks API — end-to-end against a real Postgres (migration 0027).

Pins the longitudinal contract: create a track seeded with a baseline
finding, link a follow-up timepoint, read the derived trajectory
("+100% volume, doubling 90d, increase"), and the safety invariants —
cross-patient link is rejected (422, before the composite FK would),
a second baseline is refused (409), and dry_run writes nothing.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Finding, FindingType, Patient, User
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


async def _ftype_id(db: AsyncSession) -> uuid.UUID:
    return (
        await db.execute(select(FindingType.id).where(FindingType.key == "nodule"))
    ).scalar_one()


async def _mk_finding(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    study_id: uuid.UUID,
    ftype_id: uuid.UUID,
    volume_ml: float,
    diameter_mm: float,
) -> Finding:
    f = Finding(
        patient_id=patient_id,
        study_id=study_id,
        finding_type_id=ftype_id,
        volume_ml=volume_ml,
        longest_diameter_mm=diameter_mm,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db.add(f)
    await db.flush()
    return f


async def test_lesion_track_lifecycle_and_guards(db_session, make_user, make_study) -> None:
    owner = await make_user()
    base_study, _ = await make_study(owner, description="baseline", study_date=date(2026, 1, 1))
    patient_id = base_study.patient_id
    patient = (
        await db_session.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one()
    follow_study, _ = await make_study(
        owner, patient=patient, description="follow-up", study_date=date(2026, 4, 1)
    )
    ftype = await _ftype_id(db_session)
    f_base = await _mk_finding(
        db_session,
        patient_id=patient_id,
        study_id=base_study.id,
        ftype_id=ftype,
        volume_ml=4.0,
        diameter_mm=20.0,
    )
    f_follow = await _mk_finding(
        db_session,
        patient_id=patient_id,
        study_id=follow_study.id,
        ftype_id=ftype,
        volume_ml=8.0,
        diameter_mm=25.0,
    )
    f_extra = await _mk_finding(
        db_session,
        patient_id=patient_id,
        study_id=base_study.id,
        ftype_id=ftype,
        volume_ml=2.0,
        diameter_mm=12.0,
    )
    await db_session.commit()

    other_patient_id: uuid.UUID | None = None
    try:
        async with _client_as(db_session, owner) as client:
            # --- create, seeded with the baseline timepoint ---
            r = await client.post(
                f"/api/patients/{patient_id}/lesion-tracks",
                json={
                    "label": "Nodulo LSD",
                    "type": "nodule",
                    "baseline_finding_id": str(f_base.id),
                },
            )
            assert r.status_code == 201, r.text
            track = r.json()
            tid = track["id"]
            assert track["type"] == "nodule"
            assert len(track["points"]) == 1
            assert track["points"][0]["is_baseline"] is True
            assert track["points"][0]["timepoint_date"] == "2026-01-01"

            # --- link the follow-up timepoint ---
            r = await client.post(
                f"/api/lesion-tracks/{tid}/points", json={"finding_id": str(f_follow.id)}
            )
            assert r.status_code == 200, r.text
            assert len(r.json()["points"]) == 2

            # --- derived trajectory ---
            r = await client.get(f"/api/lesion-tracks/{tid}/trajectory")
            assert r.status_code == 200, r.text
            s = r.json()["summary"]
            assert abs(s["volume_pct_change_total"] - 100.0) < 1e-6
            assert abs(s["doubling_time_days"] - 90.0) < 1e-6
            assert s["overall_direction"] == "increase"

            # --- single-baseline guard (fresh, unlinked finding) ---
            r = await client.post(
                f"/api/lesion-tracks/{tid}/points",
                json={"finding_id": str(f_extra.id), "is_baseline": True},
            )
            assert r.status_code == 409, r.text
            assert "baseline" in r.json()["detail"].lower()

            # --- a finding already tracked cannot be linked again ---
            r = await client.post(
                f"/api/lesion-tracks/{tid}/points", json={"finding_id": str(f_follow.id)}
            )
            assert r.status_code == 409, r.text

            # --- dry_run writes nothing (201, as for findings) ---
            r = await client.post(
                f"/api/patients/{patient_id}/lesion-tracks?dry_run=true",
                json={"label": "scratch"},
            )
            assert r.status_code == 201, r.text
            assert r.json()["id"] == "dry-run"

            # --- cross-patient link is rejected before the composite FK ---
            owner2 = await make_user()
            other_study, _ = await make_study(
                owner2, description="other patient", study_date=date(2026, 1, 1)
            )
            other_patient_id = other_study.patient_id
            assert other_patient_id is not None
            f_other = await _mk_finding(
                db_session,
                patient_id=other_patient_id,
                study_id=other_study.id,
                ftype_id=ftype,
                volume_ml=3.0,
                diameter_mm=15.0,
            )
            await db_session.commit()
            r = await client.post(
                f"/api/lesion-tracks/{tid}/points", json={"finding_id": str(f_other.id)}
            )
            assert r.status_code == 422, r.text
    finally:
        app.dependency_overrides.clear()
        # Clean the rows the API committed (db_session rollback can't undo commits).
        await db_session.execute(
            text("DELETE FROM lesion_tracks WHERE patient_id = :p"), {"p": str(patient_id)}
        )
        pids = [str(patient_id)] + ([str(other_patient_id)] if other_patient_id else [])
        await db_session.execute(
            text("DELETE FROM findings WHERE patient_id = ANY(:ps)"), {"ps": pids}
        )
        await db_session.commit()
