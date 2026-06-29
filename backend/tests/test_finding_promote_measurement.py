"""Promote a live PET-VOI measurement onto a Finding (Flow 2e09b6d9).

The measurement columns that make the corpus quantitatively queryable
(SUVmax/peak/mean, MTV) are materialised SERVER-SIDE from the VOI
computer — never asserted by the caller. These tests cover the parts
that do not need an S3 volume: the metrics→columns mapping (incl. the
raw-units SUV guard), the request-schema geometry requirements, and the
write helper (columns + series anchor + etag bump + audit revision +
idempotent measurement-geometry link). The recompute glue itself reuses
the already-tested ``load_pet_volume`` + ``compute_voi_*``.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from bvphoenix.api.findings import (
    PromoteMeasurementIn,
    _apply_promoted_measurements,
    _roi_stats_to_measurements,
    _voi_metrics_to_measurements,
    _volume_to_measurements,
)
from bvphoenix.api.studies._shared import ROIStatsIn, ROIStatsOut
from bvphoenix.db.models import Finding, FindingGeometry, FindingRevision, FindingType, Marker
from bvphoenix.services.pet_voi import VoiMetrics
from tests.conftest import skip_if_no_db


def _roi_out(**over) -> ROIStatsOut:
    base: dict = {
        "voxel_count": 100,
        "mean": 42.0,
        "std": 7.5,
        "min": 10.0,
        "max": 88.0,
        "peak_1cm3": 80.0,
        "suv_mean": None,
        "suv_sd": None,
        "suv_max": None,
        "suv_peak": None,
        "suv_variant_used": None,
        "units_native": None,
    }
    base.update(over)
    return ROIStatsOut(**base)


def test_roi_stats_mapping_hu_path_when_no_suv() -> None:
    # CT (no SUV variant): native mean/std land in the HU columns.
    out = _roi_stats_to_measurements(_roi_out(mean=-45.0, std=12.0, units_native="HU"))
    assert out == {"hu_mean": -45.0, "hu_std": 12.0}
    assert "suv_max" not in out


def test_roi_stats_mapping_suv_path_excludes_hu() -> None:
    # PET ROI with a resolved SUV variant: write only suv_* (a number in
    # hu_mean is asserted to be HU, so it must not carry Bq/mL).
    out = _roi_stats_to_measurements(
        _roi_out(suv_max=6.1, suv_peak=5.5, suv_mean=4.0, units_native="BQML")
    )
    assert out == {"suv_max": 6.1, "suv_peak": 5.5, "suv_mean": 4.0}
    assert "hu_mean" not in out


def test_measure_volume_mapping_longest_short_volume() -> None:
    # extent (dx, dy, dz) in mm → longest = max, short = median, volume_ml.
    out = _volume_to_measurements({"extent_mm": [12.0, 30.0, 4.0], "volume_ml": 1.44})
    assert out == {"longest_diameter_mm": 30.0, "short_axis_mm": 12.0, "volume_ml": 1.44}


# ---------------------------------------------------------------------------
# Pure: VOI metrics → finding measurement columns
# ---------------------------------------------------------------------------


def _metrics(units: str) -> VoiMetrics:
    return VoiMetrics(
        suv_max=5.2,
        suv_peak=4.8,
        suv_mean=3.1,
        mtv_ml=12.0,
        tlg=37.2,
        voxel_count=200,
        units=units,  # type: ignore[arg-type]
        voi_kind="spherical",
        notes=[],
    )


def test_mapping_suv_units_fills_all_columns() -> None:
    out = _voi_metrics_to_measurements(_metrics("SUV"))
    assert out == {"volume_ml": 12.0, "suv_max": 5.2, "suv_peak": 4.8, "suv_mean": 3.1}


def test_mapping_raw_units_drops_suv_keeps_volume() -> None:
    # No decay-corrected dose: raw PET counts must NOT be written into the
    # SUV columns (a clinically misleading number); only MTV survives.
    out = _voi_metrics_to_measurements(_metrics("raw"))
    assert out == {"volume_ml": 12.0}
    assert "suv_max" not in out


# ---------------------------------------------------------------------------
# Pure: request schema requires the geometry its source needs
# ---------------------------------------------------------------------------


def test_schema_voi_spherical_requires_center_and_radius() -> None:
    with pytest.raises(ValidationError):
        PromoteMeasurementIn(series_id=uuid.uuid4(), source="voi_spherical")
    ok = PromoteMeasurementIn(
        series_id=uuid.uuid4(),
        source="voi_spherical",
        center_mm={"x": 1.0, "y": 2.0, "z": 3.0},  # type: ignore[arg-type]
        radius_mm=10.0,
    )
    assert ok.radius_mm == 10.0


def test_schema_voi_threshold_requires_seed_and_threshold() -> None:
    with pytest.raises(ValidationError):
        PromoteMeasurementIn(
            series_id=uuid.uuid4(),
            source="voi_threshold",
            seed_mm={"x": 1.0, "y": 2.0, "z": 3.0},  # type: ignore[arg-type]
        )


def test_schema_roi_stats_requires_roi() -> None:
    with pytest.raises(ValidationError):
        PromoteMeasurementIn(series_id=uuid.uuid4(), source="roi_stats")
    ok = PromoteMeasurementIn(
        series_id=uuid.uuid4(),
        source="roi_stats",
        roi={"kind": "rectangle", "min_ijk": [0, 0, 0], "max_ijk": [4, 4, 4]},  # type: ignore[arg-type]
    )
    assert ok.roi is not None


def test_schema_measure_volume_requires_volume() -> None:
    with pytest.raises(ValidationError):
        PromoteMeasurementIn(series_id=uuid.uuid4(), source="measure_volume")
    ok = PromoteMeasurementIn(
        series_id=uuid.uuid4(),
        source="measure_volume",
        volume={"p0": {"i": 0, "j": 0, "k": 0}, "p1": {"i": 4, "j": 4, "k": 2}},  # type: ignore[arg-type]
    )
    assert ok.volume is not None


# ---------------------------------------------------------------------------
# DB-backed: the write helper
# ---------------------------------------------------------------------------


async def _make_finding(db, owner, study, *, status: str = "candidate") -> Finding:
    ftype = (await db.execute(select(FindingType).limit(1))).scalars().first()
    assert ftype is not None, "finding_types vocab must be seeded (migration 0001)"
    f = Finding(
        patient_id=study.patient_id,
        study_id=study.id,
        finding_type_id=ftype.id,
        author_subject_id=owner.subject_id,
        author_kind="agent",
        status=status,
        etag=uuid.uuid4(),
    )
    db.add(f)
    await db.flush()
    return f


async def _cleanup(db, *, finding_id: uuid.UUID, marker_id: uuid.UUID | None = None) -> None:
    await db.execute(delete(FindingGeometry).where(FindingGeometry.finding_id == finding_id))
    await db.execute(delete(FindingRevision).where(FindingRevision.finding_id == finding_id))
    await db.execute(delete(Finding).where(Finding.id == finding_id))
    if marker_id is not None:
        await db.execute(delete(Marker).where(Marker.id == marker_id))
    await db.commit()


@skip_if_no_db
async def test_apply_materializes_columns_anchors_series_and_revisions(
    db_session, make_user, make_study
) -> None:
    owner = await make_user()
    study, series = await make_study(owner, modality="PT")
    f = await _make_finding(db_session, owner, study)
    old_etag = f.etag

    changed = await _apply_promoted_measurements(
        db_session,
        finding=f,
        source="voi_spherical",
        series_id=series.id,
        measurements={"volume_ml": 12.0, "suv_max": 5.2, "suv_peak": 4.8, "suv_mean": 3.1},
        geometry_marker_id=None,
        actor_subject_id=owner.subject_id,
        author_kind="agent",
    )

    assert set(changed) == {"volume_ml", "suv_max", "suv_peak", "suv_mean"}
    assert f.suv_max == 5.2
    assert f.volume_ml == 12.0
    assert f.series_id == series.id  # anchored to the measured series
    assert f.etag != old_etag  # optimistic-concurrency token rotated
    assert f.status == "candidate"  # left for a human to confirm

    rev = (
        (
            await db_session.execute(
                select(FindingRevision)
                .where(FindingRevision.finding_id == f.id)
                .order_by(FindingRevision.revision_no.desc())
            )
        )
        .scalars()
        .first()
    )
    assert rev is not None
    assert rev.change_kind == "update"
    assert rev.diff_summary is not None
    assert rev.diff_summary.startswith("promote_measurement:voi_spherical")
    assert rev.author_kind == "agent"

    await _cleanup(db_session, finding_id=f.id)


@skip_if_no_db
async def test_compute_roi_stats_core_reads_packed_volume(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """The extracted core computes numpy-precise stats over the packed volume
    (so the roi_stats promotion source measures the same number the route
    does). Verifies the refactor didn't change the math."""
    import numpy as np

    from bvphoenix.api.studies import roi_stats as roi_mod
    from bvphoenix.db.models import Derivative
    from bvphoenix.services.volumes import DERIVATIVE_FORMAT, DERIVATIVE_KIND, HEADER_STRUCT

    owner = await make_user()
    study, series = await make_study(owner, modality="CT")

    # Known volume: arr[z,y,x] = z*100 + y*10 + x over a 4x3x2 grid.
    nz, ny, nx = 2, 3, 4
    arr = np.fromfunction(lambda z, y, x: z * 100 + y * 10 + x, (nz, ny, nx), dtype=np.float32)
    header = HEADER_STRUCT.pack(nx, ny, nz, 1.0, 1.0, 1.0, float(arr.min()), float(arr.max()))
    packed = header + arr.tobytes()

    db_session.add(
        Derivative(
            series_id=series.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-test",
            s3_key="vol-roi",
        )
    )
    await db_session.flush()

    class _FakeStorage:
        def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
            return packed

    monkeypatch.setattr(roi_mod, "get_s3_storage", lambda: _FakeStorage())

    out = await roi_mod.compute_roi_stats_core(
        db_session,
        series,
        ROIStatsIn(kind="rectangle", min_ijk=[0, 0, 0], max_ijk=[nx - 1, ny - 1, nz - 1]),
    )
    assert out.voxel_count == arr.size
    assert out.mean == pytest.approx(float(arr.mean()))
    assert out.std == pytest.approx(float(arr.std()))
    assert out.max == pytest.approx(float(arr.max()))
    # No SUV variant requested → suv columns stay None (CT path → hu_*).
    assert out.suv_max is None
    assert _roi_stats_to_measurements(out) == {"hu_mean": out.mean, "hu_std": out.std}

    await db_session.execute(delete(Finding).where(Finding.study_id == study.id))
    await db_session.commit()


@skip_if_no_db
async def test_create_findings_from_hot_spots_creates_and_is_idempotent(
    db_session, make_user, make_study, monkeypatch
) -> None:
    """The hot-spots creation flow makes one candidate finding per detected
    spot (with a bbox.lesion marker) and is idempotent on the spot signature:
    a second identical run creates nothing new."""
    import numpy as np
    from starlette.requests import Request

    from bvphoenix.api.findings import create_findings_from_hot_spots
    from bvphoenix.api.studies import bulk as bulk_mod
    from bvphoenix.api.studies._shared import HotSpotsIn
    from bvphoenix.db.models import Derivative, FindingGeometry, Marker
    from bvphoenix.services.volumes import DERIVATIVE_FORMAT, DERIVATIVE_KIND, HEADER_STRUCT

    owner = await make_user()
    study, series = await make_study(owner, modality="PT")

    # 8x8x4 background with one bright 2x2x2 cube → exactly one component
    # above the 50%-of-max threshold.
    nz, ny, nx = 4, 8, 8
    arr = np.zeros((nz, ny, nx), dtype=np.float32)
    arr[1:3, 2:4, 2:4] = 100.0
    packed = HEADER_STRUCT.pack(nx, ny, nz, 1.0, 1.0, 1.0, 0.0, 100.0) + arr.tobytes()
    db_session.add(
        Derivative(
            series_id=series.id,
            kind=DERIVATIVE_KIND,
            format=DERIVATIVE_FORMAT,
            stack_index=0,
            s3_bucket="bv-test",
            s3_key="vol-hs",
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        bulk_mod,
        "get_s3_storage",
        lambda: type("S", (), {"get_object_bytes": staticmethod(lambda **k: packed)})(),
    )

    ftype = (await db_session.execute(select(FindingType).limit(1))).scalars().first()

    class _StubIdem:
        replay = None

        def capture(self, payload, **_kw):
            return payload

    req = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""})

    body = type(
        "B",
        (),
        {
            "hot_spots": HotSpotsIn(
                threshold_mode="percent_of_max", threshold_value=0.5, min_volume_ml=0.0
            ),
            "type": ftype.key,
            "confidence": 0.5,
        },
    )()

    class _StubAuditLocal:
        async def log(self, **_kw):
            return None

    out = await create_findings_from_hot_spots(
        req, series.id, body, db_session, owner, _StubAuditLocal(), _StubIdem()
    )
    assert out["total_spots"] == 1
    assert len(out["created"]) == 1
    fid = uuid.UUID(out["created"][0]["id"])
    # measurements + a linked bbox marker
    assert out["created"][0]["volume_ml"] is not None
    assert out["created"][0]["status"] == "candidate"
    geoms = (
        (await db_session.execute(select(FindingGeometry).where(FindingGeometry.finding_id == fid)))
        .scalars()
        .all()
    )
    assert any(g.role == "bbox" for g in geoms)

    # Second identical run: the spot signature already has a bbox.lesion
    # marker → nothing new.
    out2 = await create_findings_from_hot_spots(
        req, series.id, body, db_session, owner, _StubAuditLocal(), _StubIdem()
    )
    assert out2["total_spots"] == 1
    assert out2["created"] == []
    assert out2["skipped_existing"] == 1

    # Cleanup
    for f in (
        (await db_session.execute(select(Finding).where(Finding.study_id == study.id)))
        .scalars()
        .all()
    ):
        await db_session.execute(delete(FindingGeometry).where(FindingGeometry.finding_id == f.id))
        await db_session.execute(delete(FindingRevision).where(FindingRevision.finding_id == f.id))
        await db_session.execute(delete(Finding).where(Finding.id == f.id))
    await db_session.execute(delete(Marker).where(Marker.target_id == series.id))
    await db_session.commit()


@skip_if_no_db
async def test_measurement_geometry_link_is_idempotent(db_session, make_user, make_study) -> None:
    owner = await make_user()
    study, series = await make_study(owner, modality="PT")
    f = await _make_finding(db_session, owner, study)
    marker = Marker(
        patient_id=study.patient_id,
        target_kind="series",
        target_id=series.id,
        kind="measurement.sphere",
        author_kind="agent",
        author_subject_id=owner.subject_id,
        geometry={"center_mm": [1.0, 2.0, 3.0], "radius_mm": 10.0},
        etag=uuid.uuid4(),
    )
    db_session.add(marker)
    await db_session.flush()

    # Promote twice with the same VOI marker — the measurement-geometry link
    # must not duplicate.
    for _ in range(2):
        await _apply_promoted_measurements(
            db_session,
            finding=f,
            source="voi_spherical",
            series_id=series.id,
            measurements={"volume_ml": 10.0},
            geometry_marker_id=marker.id,
            actor_subject_id=owner.subject_id,
            author_kind="agent",
        )

    links = (
        (
            await db_session.execute(
                select(FindingGeometry).where(
                    FindingGeometry.finding_id == f.id,
                    FindingGeometry.role == "measurement",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].marker_id == marker.id

    await _cleanup(db_session, finding_id=f.id, marker_id=marker.id)
