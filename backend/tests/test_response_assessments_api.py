"""Response-assessments API end-to-end (real Postgres, migration 0029).

Two target lesions shrink 30%+ -> the assessment computes PR; growing one
and recomputing flips it to PD; dry_run persists nothing.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import (
    Finding,
    FindingType,
    LesionTrack,
    LesionTrackPoint,
    Patient,
    User,
)
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


async def _target_lesion(
    db, *, patient_id, ftype, base_study, base_series, cur_study, cur_series, base_mm, cur_mm
) -> Finding:
    track = LesionTrack(
        patient_id=patient_id,
        label="Lesione target",
        finding_type_id=ftype,
        recist_role="target",
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db.add(track)
    f_base = Finding(
        patient_id=patient_id,
        study_id=base_study.id,
        series_id=base_series.id,
        finding_type_id=ftype,
        longest_diameter_mm=base_mm,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    f_cur = Finding(
        patient_id=patient_id,
        study_id=cur_study.id,
        series_id=cur_series.id,
        finding_type_id=ftype,
        longest_diameter_mm=cur_mm,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db.add_all([f_base, f_cur])
    await db.flush()
    db.add(
        LesionTrackPoint(
            lesion_track_id=track.id,
            finding_id=f_base.id,
            patient_id=patient_id,
            is_baseline=True,
            timepoint_date=base_study.study_date,
        )
    )
    db.add(
        LesionTrackPoint(
            lesion_track_id=track.id,
            finding_id=f_cur.id,
            patient_id=patient_id,
            is_baseline=False,
            timepoint_date=cur_study.study_date,
        )
    )
    await db.flush()
    return f_cur


async def test_response_assessment_pr_then_recompute_pd(db_session, make_user, make_study) -> None:
    owner = await make_user()
    base_study, base_series = await make_study(owner, study_date=date(2026, 1, 1))
    patient_id = base_study.patient_id
    patient = (
        await db_session.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one()
    cur_study, cur_series = await make_study(owner, patient=patient, study_date=date(2026, 4, 1))
    ftype = await _ftype_id(db_session)

    # Lesion A 50->35, lesion B 30->20: sum 80 -> 55 (-31%) = PR.
    f_cur_a = await _target_lesion(
        db_session,
        patient_id=patient_id,
        ftype=ftype,
        base_study=base_study,
        base_series=base_series,
        cur_study=cur_study,
        cur_series=cur_series,
        base_mm=50.0,
        cur_mm=35.0,
    )
    await _target_lesion(
        db_session,
        patient_id=patient_id,
        ftype=ftype,
        base_study=base_study,
        base_series=base_series,
        cur_study=cur_study,
        cur_series=cur_series,
        base_mm=30.0,
        cur_mm=20.0,
    )
    await db_session.commit()

    try:
        async with _client_as(db_session, owner) as client:
            r = await client.post(
                f"/api/patients/{patient_id}/response-assessments",
                json={
                    "current_study_id": str(cur_study.id),
                    "baseline_study_id": str(base_study.id),
                    "criterion": "recist_1_1",
                },
            )
            assert r.status_code == 201, r.text
            ra = r.json()
            ra_id = ra["id"]
            assert ra["category"] == "PR", ra
            assert ra["baseline_sum_mm"] == 80.0
            assert ra["target_sum_mm"] == 55.0
            assert ra["basis"]["n_target_lesions"] == 2

            # dry_run persists nothing.
            r = await client.post(
                f"/api/patients/{patient_id}/response-assessments?dry_run=true",
                json={
                    "current_study_id": str(cur_study.id),
                    "baseline_study_id": str(base_study.id),
                },
            )
            assert r.status_code == 201 and r.json()["id"] == "dry-run"

            # Grow lesion A on the follow-up (35 -> 80): sum 100, +25% vs
            # baseline AND +20mm vs nadir -> recompute flips to PD.
            f_cur_a.longest_diameter_mm = 80.0
            await db_session.flush()
            await db_session.commit()
            r = await client.post(f"/api/response-assessments/{ra_id}/recompute")
            assert r.status_code == 200, r.text
            assert r.json()["category"] == "PD", r.json()
            assert r.json()["target_sum_mm"] == 100.0

            # Revisions: create + recompute.
            r = await client.get(f"/api/response-assessments/{ra_id}/revisions")
            kinds = {rev["change_kind"] for rev in r.json()}
            assert {"create", "recompute"} <= kinds
    finally:
        app.dependency_overrides.clear()
        await db_session.execute(
            text("DELETE FROM response_assessments WHERE patient_id = :p"), {"p": str(patient_id)}
        )
        await db_session.execute(
            text("DELETE FROM lesion_tracks WHERE patient_id = :p"), {"p": str(patient_id)}
        )
        await db_session.execute(
            text("DELETE FROM findings WHERE patient_id = :p"), {"p": str(patient_id)}
        )
        await db_session.commit()


async def _cleanup(db_session, patient_id) -> None:
    app.dependency_overrides.clear()
    for tbl in ("response_assessments", "lesion_tracks", "findings"):
        await db_session.execute(
            text(f"DELETE FROM {tbl} WHERE patient_id = :p"), {"p": str(patient_id)}
        )
    await db_session.commit()


async def test_list_scoped_to_current_study(db_session, make_user, make_study) -> None:
    """The card lists by ``current_study_id`` so an assessment from study A
    never leaks onto study B (the stale-NE bug)."""
    owner = await make_user()
    base_study, base_series = await make_study(owner, study_date=date(2026, 1, 1))
    patient_id = base_study.patient_id
    patient = (
        await db_session.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one()
    cur_study, cur_series = await make_study(owner, patient=patient, study_date=date(2026, 4, 1))
    ftype = await _ftype_id(db_session)
    await _target_lesion(
        db_session,
        patient_id=patient_id,
        ftype=ftype,
        base_study=base_study,
        base_series=base_series,
        cur_study=cur_study,
        cur_series=cur_series,
        base_mm=50.0,
        cur_mm=35.0,
    )
    await db_session.commit()
    try:
        async with _client_as(db_session, owner) as client:
            r = await client.post(
                f"/api/patients/{patient_id}/response-assessments",
                json={"current_study_id": str(cur_study.id), "criterion": "recist_1_1"},
            )
            assert r.status_code == 201, r.text

            # Scoped to the follow-up study -> returns it.
            r = await client.get(
                f"/api/patients/{patient_id}/response-assessments"
                f"?current_study_id={cur_study.id}&limit=1"
            )
            assert r.status_code == 200
            assert len(r.json()) == 1
            assert r.json()[0]["current_study_id"] == str(cur_study.id)

            # Scoped to the baseline study (which has no assessment) -> empty,
            # not the unrelated follow-up assessment.
            r = await client.get(
                f"/api/patients/{patient_id}/response-assessments"
                f"?current_study_id={base_study.id}&limit=1"
            )
            assert r.status_code == 200
            assert r.json() == []
    finally:
        await _cleanup(db_session, patient_id)


async def test_nodal_target_uses_short_axis_e2e(db_session, make_user, make_study) -> None:
    """A lymph-node target contributes its short axis to the SoD (the
    ``FindingType.key == 'lymph_node'`` join drives ``is_nodal``)."""
    owner = await make_user()
    base_study, base_series = await make_study(owner, study_date=date(2026, 1, 1))
    patient_id = base_study.patient_id
    patient = (
        await db_session.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one()
    cur_study, cur_series = await make_study(owner, patient=patient, study_date=date(2026, 4, 1))
    node_ftype = (
        await db_session.execute(select(FindingType.id).where(FindingType.key == "lymph_node"))
    ).scalar_one()

    track = LesionTrack(
        patient_id=patient_id,
        label="Linfonodo mediastinico",
        finding_type_id=node_ftype,
        recist_role="target",
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db_session.add(track)
    f_base = Finding(
        patient_id=patient_id,
        study_id=base_study.id,
        series_id=base_series.id,
        finding_type_id=node_ftype,
        longest_diameter_mm=30.0,
        short_axis_mm=18.0,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    f_cur = Finding(
        patient_id=patient_id,
        study_id=cur_study.id,
        series_id=cur_series.id,
        finding_type_id=node_ftype,
        longest_diameter_mm=30.0,
        short_axis_mm=12.0,
        author_kind="human",
        etag=uuid.uuid4(),
    )
    db_session.add_all([f_base, f_cur])
    await db_session.flush()
    db_session.add_all(
        [
            LesionTrackPoint(
                lesion_track_id=track.id,
                finding_id=f_base.id,
                patient_id=patient_id,
                is_baseline=True,
                timepoint_date=base_study.study_date,
            ),
            LesionTrackPoint(
                lesion_track_id=track.id,
                finding_id=f_cur.id,
                patient_id=patient_id,
                is_baseline=False,
                timepoint_date=cur_study.study_date,
            ),
        ]
    )
    await db_session.commit()
    try:
        async with _client_as(db_session, owner) as client:
            r = await client.post(
                f"/api/patients/{patient_id}/response-assessments",
                json={
                    "current_study_id": str(cur_study.id),
                    "baseline_study_id": str(base_study.id),
                    "criterion": "recist_1_1",
                },
            )
            assert r.status_code == 201, r.text
            ra = r.json()
            # Short axis summed: 18 -> 12 (-33%) = PR, not the static 30->30.
            assert ra["baseline_sum_mm"] == 18.0, ra
            assert ra["target_sum_mm"] == 12.0, ra
            assert ra["category"] == "PR", ra
            assert ra["basis"]["lesions"][0]["is_nodal"] is True
    finally:
        await _cleanup(db_session, patient_id)
