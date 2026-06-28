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
    _voi_metrics_to_measurements,
)
from bvphoenix.db.models import Finding, FindingGeometry, FindingRevision, FindingType, Marker
from bvphoenix.services.pet_voi import VoiMetrics
from tests.conftest import skip_if_no_db

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
