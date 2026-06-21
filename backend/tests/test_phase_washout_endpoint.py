"""End-to-end test of the cross-phase wash-out endpoint
(``POST /studies/{id}/phase-roi-stats``) with the S3 volume fetch mocked.

Covers the full handler path that a radiologist exercises when drawing a
wash-out ROI: load the phases' packed-volume derivatives, RELEASE the DB
connection, fan out the (mocked) S3 GETs, sample one LPS sphere in every
phase, and compute APW/RPW. The connection-release refactor (mirroring
api/display_metadata) is what stops the ``QueuePool ... TimeoutError`` 500
the production viewer hit; this test pins the happy path AND the two-phase
shape (unenhanced + delayed only, no enhanced) that must NOT 500.
"""

from __future__ import annotations

import base64
import uuid

import numpy as np
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import Series, User
from bvphoenix.db.models.dicom import Derivative
from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services.volumes import DERIVATIVE_FORMAT, DERIVATIVE_KIND, HEADER_STRUCT
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db

_FOR = "1.2.840.FOR.washout"
_GEOM = {
    "origin": [0.0, 0.0, 0.0],
    "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    "frame_of_reference_uid": _FOR,
}


def _packed(hu: float, n: int = 11, sp: float = 1.0) -> bytes:
    """A packed Float32 volume_f32 blob: n³ voxels all = ``hu`` HU, unit
    spacing, identity geometry (so world == index)."""
    header = HEADER_STRUCT.pack(n, n, n, sp, sp, sp, float(hu), float(hu))
    scalars = np.full(n * n * n, hu, dtype=np.float32).tobytes()
    return header + scalars


class _FakeStorage:
    """Maps s3_key -> bytes; a key whose value is an Exception is raised when
    fetched (simulating a missing / corrupt object)."""

    def __init__(self, by_key: dict[str, bytes | Exception]) -> None:
        self._by_key = by_key

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        del bucket
        val = self._by_key[key]
        if isinstance(val, Exception):
            raise val
        return val

    def get_object_range(self, *, bucket: str, key: str, start: int, length: int) -> bytes:
        del bucket
        val = self._by_key[key]
        if isinstance(val, Exception):
            raise val
        return val[start : start + length]


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


async def _phase_series(
    db: AsyncSession, study_id: uuid.UUID, desc: str, number: int, phase: str
) -> tuple[Series, str]:
    """Create a classified CT phase series + its packed-volume derivative;
    return (series, s3_key) so the caller can register the mocked bytes."""
    s = Series(
        id=uuid.uuid4(),
        study_id=study_id,
        series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        series_number=number,
        modality="CT",
        body_part_examined="LIVER",
        series_description=desc,
        acquisition_phase=phase,
        phase_source="human",
    )
    db.add(s)
    await db.flush()
    key = f"vol/{s.id}"
    db.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=key,
            geometry=_GEOM,
        )
    )
    await db.flush()
    return s, key


async def test_washout_three_phases_apw_rpw(db_session, make_user, make_study, monkeypatch) -> None:
    owner = await make_user()
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    _, k1 = await _phase_series(db_session, study.id, "portale", 2, "portal_venous")
    _, k2 = await _phase_series(db_session, study.id, "tardiva", 3, "delayed")
    await db_session.commit()

    monkeypatch.setattr(
        "bvphoenix.api.studies.phases.get_s3_storage",
        lambda: _FakeStorage({k0: _packed(30.0), k1: _packed(100.0), k2: _packed(60.0)}),
    )

    async with _client_as(db_session, owner) as client:
        r = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "center_lps": [5.0, 5.0, 5.0],
                "radius_mm": 3.0,
                "frame_of_reference_uid": _FOR,
                # Adrenal scenario: the adenoma verdict flags are adrenal-scoped.
                "region": "adrenal",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["washout"]["region"] == "adrenal"
    by_phase = {s["acquisition_phase"]: s for s in body["samples"]}
    assert set(by_phase) == {"unenhanced", "portal_venous", "delayed"}
    assert by_phase["unenhanced"]["hu_mean"] == 30.0
    assert by_phase["portal_venous"]["hu_mean"] == 100.0
    assert by_phase["delayed"]["hu_mean"] == 60.0
    assert all(s["voxel_count"] > 0 for s in body["samples"])
    assert body["skipped"] == []

    w = body["washout"]
    # APW = 100*(E-D)/(E-U) = 100*(100-60)/(100-30) = 57.14; RPW = 100*40/100 = 40.
    assert w["apw"] == 100.0 * (100 - 60) / (100 - 30)
    assert w["rpw"] == 40.0
    assert w["apw_ge_60"] is False
    assert w["rpw_ge_40"] is True
    assert w["unenhanced_below_10hu"] is False
    assert [c["acquisition_phase"] for c in w["curve"]] == [
        "unenhanced",
        "portal_venous",
        "delayed",
    ]


async def test_washout_two_phases_no_enhanced_does_not_500(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """The real mamma study shape: only unenhanced + delayed (no enhanced
    phase). APW/RPW are not computable but the endpoint must return 200 with
    the two samples — it must NOT raise (the bug surfaced as a 500)."""
    owner = await make_user()
    study, s0 = await make_study(
        owner, modality="CT", body_part="CHEST", series_description="Basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    _, k2 = await _phase_series(db_session, study.id, "tardiva dopo portale", 9, "delayed")
    await db_session.commit()

    monkeypatch.setattr(
        "bvphoenix.api.studies.phases.get_s3_storage",
        lambda: _FakeStorage({k0: _packed(40.0), k2: _packed(70.0)}),
    )

    async with _client_as(db_session, owner) as client:
        r = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "center_lps": [5.0, 5.0, 5.0],
                "radius_mm": 3.0,
                "frame_of_reference_uid": _FOR,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert {s["acquisition_phase"] for s in body["samples"]} == {"unenhanced", "delayed"}
    w = body["washout"]
    assert w["apw"] is None and w["rpw"] is None
    assert w["unenhanced_hu"] == 40.0
    assert w["delayed_hu"] == 70.0


async def test_washout_skips_unpacked_phase(db_session, make_user, make_study, monkeypatch) -> None:
    """A classified phase with no packed derivative is reported under
    'skipped' (not packed), never 500s the whole request."""
    owner = await make_user()
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    # A delayed phase with NO derivative row (unpacked).
    unpacked = Series(
        id=uuid.uuid4(),
        study_id=study.id,
        series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        series_number=3,
        modality="CT",
        body_part_examined="LIVER",
        series_description="tardiva (non impacchettata)",
        acquisition_phase="delayed",
        phase_source="human",
    )
    db_session.add(unpacked)
    await db_session.commit()

    monkeypatch.setattr(
        "bvphoenix.api.studies.phases.get_s3_storage",
        lambda: _FakeStorage({k0: _packed(35.0)}),
    )

    async with _client_as(db_session, owner) as client:
        r = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "center_lps": [5.0, 5.0, 5.0],
                "radius_mm": 3.0,
                "frame_of_reference_uid": _FOR,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["acquisition_phase"] for s in body["samples"]] == ["unenhanced"]
    assert any(sk["reason"] == "not packed" for sk in body["skipped"])


async def test_washout_unreadable_volume_skips_not_500(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """A phase whose S3 object errors (missing / corrupt) must degrade to
    'skipped', not 500 the whole wash-out — the deterministic failure mode
    behind the radiologist's persistent Internal Server Error."""
    owner = await make_user()
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    _, k2 = await _phase_series(db_session, study.id, "tardiva", 3, "delayed")
    await db_session.commit()

    # The delayed phase's object raises on fetch; the basale is fine + corrupt
    # bytes also covered via a short blob would raise struct.error.
    monkeypatch.setattr(
        "bvphoenix.api.studies.phases.get_s3_storage",
        lambda: _FakeStorage({k0: _packed(35.0), k2: RuntimeError("S3 NoSuchKey")}),
    )

    async with _client_as(db_session, owner) as client:
        r = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "center_lps": [5.0, 5.0, 5.0],
                "radius_mm": 3.0,
                "frame_of_reference_uid": _FOR,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["acquisition_phase"] for s in body["samples"]] == ["unenhanced"]
    assert any("unreadable" in sk["reason"] for sk in body["skipped"])


async def _phase_series_geom(
    db: AsyncSession, study_id: uuid.UUID, desc: str, number: int, phase: str, origin: list[float]
) -> tuple[Series, str]:
    """Like ``_phase_series`` but with a custom packed-volume origin, to model
    phases that share a FoR string yet were acquired at different table
    positions (their world origins differ — the real DICOM non-conformance)."""
    s = Series(
        id=uuid.uuid4(),
        study_id=study_id,
        series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        series_number=number,
        modality="CT",
        body_part_examined="LIVER",
        series_description=desc,
        acquisition_phase=phase,
        phase_source="human",
    )
    db.add(s)
    await db.flush()
    key = f"vol/{s.id}"
    db.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=key,
            geometry={
                "origin": origin,
                "direction": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                "frame_of_reference_uid": _FOR,
            },
        )
    )
    await db.flush()
    return s, key


async def test_washout_per_phase_rois_recover_shifted_enhanced_phases(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """The radiologist's real bug: 4 phases share a FoR string but were acquired
    at different table positions (different world origins). A SINGLE world ROI,
    re-mapped across phases, falls outside the shifted enhanced phases' z-range
    -> they are skipped -> wash-out reports 'no enhanced phase'. Sending each
    phase its OWN ROI (the index-synced anatomy, in that phase's own world
    frame) samples all of them correctly."""
    owner = await make_user()
    # Unenhanced at origin 0; portal + delayed shifted by -100 in z (n=11, unit
    # spacing) so world z=5 maps to k=105 in the shifted phases -> out of range.
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    sp, k1 = await _phase_series_geom(
        db_session, study.id, "portale", 2, "portal_venous", [0.0, 0.0, -100.0]
    )
    sd, k2 = await _phase_series_geom(
        db_session, study.id, "tardiva", 3, "delayed", [0.0, 0.0, -100.0]
    )
    await db_session.commit()

    monkeypatch.setattr(
        "bvphoenix.api.studies.phases.get_s3_storage",
        lambda: _FakeStorage({k0: _packed(30.0), k1: _packed(100.0), k2: _packed(60.0)}),
    )

    # 1) The BUG: a single world ROI skips the shifted enhanced + delayed phases.
    async with _client_as(db_session, owner) as client:
        r = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "center_lps": [5.0, 5.0, 5.0],
                "radius_mm": 3.0,
                "frame_of_reference_uid": _FOR,
                "region": "liver",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["acquisition_phase"] for s in body["samples"]] == ["unenhanced"]
    assert {sk["acquisition_phase"] for sk in body["skipped"]} == {"portal_venous", "delayed"}
    assert body["washout"]["enhanced_phase"] is None  # the "no enhanced phase" symptom

    # 2) The FIX: each phase sampled at its OWN world point (index-synced
    # anatomy). Same anatomy index k=5 in every phase: world z = origin_z + 5.
    async with _client_as(db_session, owner) as client:
        r2 = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "region": "liver",
                "frame_of_reference_uid": _FOR,
                "phase_rois": [
                    {"series_id": str(s0.id), "center_lps": [5.0, 5.0, 5.0], "radius_mm": 3.0},
                    {"series_id": str(sp.id), "center_lps": [5.0, 5.0, -95.0], "radius_mm": 3.0},
                    {"series_id": str(sd.id), "center_lps": [5.0, 5.0, -95.0], "radius_mm": 3.0},
                ],
            },
        )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    by_phase = {s["acquisition_phase"]: s for s in body2["samples"]}
    assert set(by_phase) == {"unenhanced", "portal_venous", "delayed"}
    assert by_phase["unenhanced"]["hu_mean"] == 30.0
    assert by_phase["portal_venous"]["hu_mean"] == 100.0
    assert by_phase["delayed"]["hu_mean"] == 60.0
    assert body2["skipped"] == []
    w2 = body2["washout"]
    assert w2["enhanced_phase"] == "portal_venous"
    assert w2["delayed_phase"] == "delayed"


async def test_washout_per_phase_parenchyma_rois(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """Liver workflow with per-phase lesion AND parenchyma ROIs across phases
    whose origins differ: the relative (lesion-minus-parenchyma) curve must be
    computed in every phase, not just the unshifted one."""
    owner = await make_user()
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    sp, k1 = await _phase_series_geom(
        db_session, study.id, "portale", 2, "portal_venous", [0.0, 0.0, -100.0]
    )
    await db_session.commit()
    # Uniform blobs: lesion ROI and parenchyma ROI read the same HU per phase
    # (the test pins plumbing, not contrast values): basale 45, portal 95.
    monkeypatch.setattr(
        "bvphoenix.api.studies.phases.get_s3_storage",
        lambda: _FakeStorage({k0: _packed(45.0), k1: _packed(95.0)}),
    )
    async with _client_as(db_session, owner) as client:
        r = await client.post(
            f"/api/studies/{study.id}/phase-roi-stats",
            json={
                "kind": "sphere",
                "region": "liver",
                "frame_of_reference_uid": _FOR,
                "phase_rois": [
                    {"series_id": str(s0.id), "center_lps": [5.0, 5.0, 5.0], "radius_mm": 2.0},
                    {"series_id": str(sp.id), "center_lps": [5.0, 5.0, -95.0], "radius_mm": 2.0},
                ],
                "phase_parenchyma_rois": [
                    {"series_id": str(s0.id), "center_lps": [3.0, 3.0, 5.0], "radius_mm": 2.0},
                    {"series_id": str(sp.id), "center_lps": [3.0, 3.0, -95.0], "radius_mm": 2.0},
                ],
            },
        )
    assert r.status_code == 200, r.text
    w = r.json()["washout"]
    rel = {p["acquisition_phase"]: p for p in w["relative_curve"]}
    assert set(rel) == {"unenhanced", "portal_venous"}
    # Uniform volume => lesion == parenchyma => delta 0 in each phase, but BOTH
    # phases must be present (the shifted one was previously dropped).
    assert rel["unenhanced"]["parenchyma_hu"] == 45.0
    assert rel["portal_venous"]["parenchyma_hu"] == 95.0


async def _setup_three_phases(db_session, make_study, owner, with_delayed: bool):
    study, s0 = await make_study(
        owner, modality="CT", body_part="LIVER", series_description="basale"
    )
    s0.series_number = 1
    s0.acquisition_phase = "unenhanced"
    s0.phase_source = "human"
    await db_session.flush()
    k0 = f"vol/{s0.id}"
    db_session.add(
        Derivative(
            id=uuid.uuid4(),
            series_id=s0.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-derivatives",
            s3_key=k0,
            geometry=_GEOM,
        )
    )
    _, k1 = await _phase_series(db_session, study.id, "portale", 2, "portal_venous")
    blobs: dict[str, bytes | Exception] = {k0: _packed(40.0), k1: _packed(120.0)}
    if with_delayed:
        _, k2 = await _phase_series(db_session, study.id, "tardiva", 3, "delayed")
        blobs[k2] = _packed(70.0)
    await db_session.commit()
    return study, blobs


async def test_washout_map_png(db_session, make_user, make_study, monkeypatch) -> None:
    owner = await make_user()
    study, blobs = await _setup_three_phases(db_session, make_study, owner, with_delayed=True)
    monkeypatch.setattr("bvphoenix.api.studies.phases.get_s3_storage", lambda: _FakeStorage(blobs))
    async with _client_as(db_session, owner) as client:
        # wash-out map = enhanced(portal_venous,120) - delayed(70) -> vabs 50, green.
        r = await client.post(
            f"/api/studies/{study.id}/washout-map",
            json={"center_lps": [5.0, 5.0, 5.0], "radius_mm": 3.0, "metric": "washout"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metric"] == "washout"
    assert body["phase_a"] == "portal_venous"
    assert body["phase_b"] == "delayed"
    assert body["vabs"] == 50.0
    assert body["width"] > 0 and body["height"] > 0
    png = base64.b64decode(body["png_base64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


async def test_washout_map_subtraction_and_422_without_delayed(
    db_session, make_user, make_study, monkeypatch
) -> None:
    owner = await make_user()
    study, blobs = await _setup_three_phases(db_session, make_study, owner, with_delayed=False)
    monkeypatch.setattr("bvphoenix.api.studies.phases.get_s3_storage", lambda: _FakeStorage(blobs))
    async with _client_as(db_session, owner) as client:
        # subtraction = enhanced(120) - unenhanced(40) -> vabs 80.
        r = await client.post(
            f"/api/studies/{study.id}/washout-map",
            json={"center_lps": [5.0, 5.0, 5.0], "radius_mm": 3.0, "metric": "subtraction"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["vabs"] == 80.0
        # wash-out needs a delayed phase; none present -> clean 422, not 500.
        r2 = await client.post(
            f"/api/studies/{study.id}/washout-map",
            json={"center_lps": [5.0, 5.0, 5.0], "radius_mm": 3.0, "metric": "washout"},
        )
        assert r2.status_code == 422, r2.text
