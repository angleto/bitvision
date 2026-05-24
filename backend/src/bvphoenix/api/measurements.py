"""Measurements API — persist 2D viewer measurements (distance, angle,
area) so they survive page reloads and can be exported as DICOM SR.

Storage uses the unified ``markers`` table with a ``measurement.<tool>``
``kind`` (e.g. ``measurement.distance``). The legacy ``annotations``
table has been retired; this module is the only producer/consumer of
the measurement subset of markers, so the on-the-wire shape is kept
flat — the frontend reads ``payload`` directly and doesn't care about
Marker internals.

The ``payload`` carries the full geometry plus the computed value:

    {
        "tool": "distance" | "angle" | "area",
        "points": [{"x": 12.3, "y": 45.6}, ...],
        "value": 38.2,
        "unit": "mm" | "deg" | "mm2",
        "label": "optional human-readable label",
        "slice_index": 34,            # optional: viewport slice when drawn
        "viewport": "axial",          # optional: axial|sagittal|coronal
        "client_id": "m17"            # optional: frontend-local id (for upsert)
    }

``POST`` is an idempotent upsert keyed on ``client_id`` so debounce-sync
from the viewer doesn't duplicate rows. ``GET`` lists. ``DELETE``
removes a single row by server id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user, require_user
from bvphoenix.db.models import ImagingStudy, Marker, Series, User
from bvphoenix.db.session import get_db
from bvphoenix.services.dicom_sr_export import measurements_to_sr_document
from bvphoenix.services.permissions import (
    READ_ANNOTATIONS,
    WRITE_ANNOTATIONS,
    can,
)

router = APIRouter(tags=["measurements"])


ToolLiteral = Literal["distance", "angle", "area"]
UnitLiteral = Literal["mm", "deg", "mm2"]


# Marker.kind values produced by this module. Kept narrow on purpose:
# the unified panel may surface other measurement.<x> kinds (created
# by future tools) but the persistence here is only the legacy three.
_TOOL_TO_KIND: dict[str, str] = {
    "distance": "measurement.distance",
    "angle": "measurement.angle",
    "area": "measurement.area",
}
_MEASUREMENT_KINDS: tuple[str, ...] = tuple(_TOOL_TO_KIND.values())


class MeasurementPoint(BaseModel):
    x: float
    y: float


class MeasurementIn(BaseModel):
    tool: ToolLiteral
    points: list[MeasurementPoint] = Field(min_length=2, max_length=256)
    value: float
    unit: UnitLiteral
    label: str | None = None
    slice_index: int | None = None
    viewport: str | None = None
    client_id: str | None = None


class MeasurementsUpsertIn(BaseModel):
    measurements: list[MeasurementIn] = Field(default_factory=list)
    replace: bool = Field(
        default=False,
        description=(
            "If true, delete server-side measurements missing from the payload. "
            "If false (default), only upsert the provided rows."
        ),
    )


class MeasurementOut(BaseModel):
    """Wire shape for a single measurement row.

    The viewer reads ``payload`` directly to render. ``id`` is used to
    target a per-row delete; ``created_at`` / ``updated_at`` drive the
    "saved" / "syncing" UI affordance.
    """

    id: str
    target_kind: str
    target_id: str
    author_subject_id: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str


async def _load_series_and_study(
    db: AsyncSession, series_id: uuid.UUID
) -> tuple[Series, ImagingStudy]:
    row = (
        await db.execute(
            select(Series, ImagingStudy)
            .join(ImagingStudy, ImagingStudy.id == Series.study_id)
            .where(Series.id == series_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="series not found")
    return row[0], row[1]


def _measurement_query(series_id: uuid.UUID) -> Select:
    return (
        select(Marker)
        .where(
            Marker.target_kind == "series",
            Marker.target_id == series_id,
            Marker.kind.in_(_MEASUREMENT_KINDS),
        )
        .order_by(Marker.created_at.asc())
    )


def _payload_from(m: MeasurementIn) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": m.tool,
        "points": [{"x": p.x, "y": p.y} for p in m.points],
        "value": m.value,
        "unit": m.unit,
    }
    if m.label is not None:
        payload["label"] = m.label
    if m.slice_index is not None:
        payload["slice_index"] = m.slice_index
    if m.viewport is not None:
        payload["viewport"] = m.viewport
    if m.client_id is not None:
        payload["client_id"] = m.client_id
    return payload


def _payload_for_marker(payload: dict[str, Any]) -> tuple[dict, dict, str | None]:
    """Split the flat client ``payload`` into the Marker columns:
    ``geometry`` (points + viewport + slice), ``computed`` (value + unit),
    and ``body`` (optional label). Storage stays consistent with the
    Marker model docstring.
    """
    geometry: dict[str, Any] = {
        "points": payload.get("points", []),
    }
    if "viewport" in payload:
        geometry["viewport"] = payload["viewport"]
    if "slice_index" in payload:
        geometry["slice_index"] = payload["slice_index"]
    if "client_id" in payload:
        geometry["client_id"] = payload["client_id"]
    geometry["tool"] = payload.get("tool")

    computed = {
        "value": payload.get("value"),
        "unit": payload.get("unit"),
    }
    body = payload.get("label")
    return geometry, computed, body


def _payload_from_marker(m: Marker) -> dict[str, Any]:
    """Reconstruct the wire ``payload`` from the Marker columns. The
    inverse of ``_payload_for_marker`` so a round-trip leaves the
    client view identical."""
    out: dict[str, Any] = {}
    g = m.geometry or {}
    if isinstance(g, dict):
        if "tool" in g:
            out["tool"] = g["tool"]
        if "points" in g:
            out["points"] = g["points"]
        if "viewport" in g:
            out["viewport"] = g["viewport"]
        if "slice_index" in g:
            out["slice_index"] = g["slice_index"]
        if "client_id" in g:
            out["client_id"] = g["client_id"]
    c = m.computed or {}
    if isinstance(c, dict):
        if "value" in c:
            out["value"] = c["value"]
        if "unit" in c:
            out["unit"] = c["unit"]
    if m.body:
        out["label"] = m.body
    return out


def _to_out(m: Marker) -> MeasurementOut:
    return MeasurementOut(
        id=str(m.id),
        target_kind=m.target_kind,
        target_id=str(m.target_id),
        author_subject_id=str(m.author_subject_id) if m.author_subject_id else None,
        payload=_payload_from_marker(m),
        created_at=m.created_at.isoformat(),
        updated_at=m.updated_at.isoformat(),
    )


@router.get("/series/{series_id}/measurements", response_model=list[MeasurementOut])
async def list_measurements(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> list[MeasurementOut]:
    _series, study = await _load_series_and_study(db, series_id)
    if not await can(db, user=user, action=READ_ANNOTATIONS, study=study):
        raise HTTPException(status_code=404, detail="series not found")
    rows = (await db.execute(_measurement_query(series_id))).scalars().all()
    return [_to_out(r) for r in rows]


@router.post(
    "/series/{series_id}/measurements",
    response_model=list[MeasurementOut],
    status_code=200,
)
async def upsert_measurements(
    series_id: uuid.UUID,
    body: MeasurementsUpsertIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> list[MeasurementOut]:
    _series, study = await _load_series_and_study(db, series_id)
    if not await can(db, user=user, action=WRITE_ANNOTATIONS, study=study):
        raise HTTPException(status_code=403, detail="cannot write measurements")

    existing = (await db.execute(_measurement_query(series_id))).scalars().all()
    by_client_id: dict[str, Marker] = {}
    for row in existing:
        cid = (row.geometry or {}).get("client_id") if isinstance(row.geometry, dict) else None
        if cid:
            by_client_id[cid] = row

    now = datetime.now(UTC)
    kept: list[Marker] = []
    for m in body.measurements:
        payload = _payload_from(m)
        geometry, computed, label = _payload_for_marker(payload)
        kind = _TOOL_TO_KIND[m.tool]
        if m.client_id and m.client_id in by_client_id:
            row = by_client_id[m.client_id]
            row.kind = kind
            row.geometry = geometry
            row.computed = computed
            row.body = label
            row.updated_at = now
        else:
            row = Marker(
                patient_id=study.patient_id,
                target_kind="series",
                target_id=series_id,
                kind=kind,
                geometry=geometry,
                computed=computed,
                body=label,
                author_subject_id=user.subject_id,
                author_kind="human",
            )
            db.add(row)
        kept.append(row)

    if body.replace:
        kept_ids = {id(a) for a in kept}
        for row in existing:
            if id(row) not in kept_ids:
                await db.delete(row)

    await db.commit()
    rows = (await db.execute(_measurement_query(series_id))).scalars().all()
    return [_to_out(r) for r in rows]


@router.delete("/measurements/{measurement_id}", status_code=204)
async def delete_measurement(
    measurement_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> None:
    row = (await db.execute(select(Marker).where(Marker.id == measurement_id))).scalar_one_or_none()
    if row is None or row.kind not in _MEASUREMENT_KINDS:
        raise HTTPException(status_code=404, detail="measurement not found")

    _series, study = await _load_series_and_study(db, row.target_id)
    is_author = row.author_subject_id == user.subject_id
    if not (is_author or await can(db, user=user, action=WRITE_ANNOTATIONS, study=study)):
        raise HTTPException(status_code=403, detail="cannot delete this measurement")
    await db.delete(row)
    await db.commit()


@router.get("/series/{series_id}/measurements.sr")
async def export_measurements_sr(
    series_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(optional_user)],
) -> dict[str, Any]:
    series, study = await _load_series_and_study(db, series_id)
    if not await can(db, user=user, action=READ_ANNOTATIONS, study=study):
        raise HTTPException(status_code=404, detail="series not found")
    rows = (await db.execute(_measurement_query(series_id))).scalars().all()
    measurements = [
        {
            "id": str(r.id),
            "created_at": r.created_at.isoformat(),
            "author_subject_id": str(r.author_subject_id) if r.author_subject_id else None,
            **_payload_from_marker(r),
        }
        for r in rows
    ]
    return measurements_to_sr_document(
        study_uid=study.study_instance_uid,
        series_uid=series.series_instance_uid,
        series_description=series.series_description,
        modality=series.modality,
        measurements=measurements,
    )
