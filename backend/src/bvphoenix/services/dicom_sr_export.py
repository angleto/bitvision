"""Serialize measurements to a minimal DICOM SR JSON representation.

The full DICOM Structured Report (SR) object is binary-encoded (DICOM
Part 10) and typically produced with ``highdicom.sr`` or ``pydicom``.
Generating it here would pull in a heavy dependency for a small feature,
so this service instead produces a JSON document that mirrors the
Content Tree of a TID 1500 *Measurement Report* (IHE Radiology SR
Profile — Measurement Report, IHE RAD TF-3 Content Profiles) closely
enough that a downstream writer can consume it.

Mappings used:
  - distance  -> DCM 121206 "Distance" (UCUM ``mm``)
  - angle     -> DCM 121211 "Angle"    (UCUM ``deg``)
  - area      -> DCM 42789003 "Area"   (UCUM ``mm2``)

References:
  * DICOM PS3.16 TID 1500 — Measurement Report
  * IHE Radiology Technical Framework Vol.3 — SR Content Profiles
  * NEMA PS3.6 Data Dictionary — DCM coded concepts

The generated JSON is NOT a valid DICOM Part 10 file, but each leaf in
``content`` carries the codes and UCUM units needed by a DICOM SR
writer (e.g. ``highdicom.sr.TID1500MeasurementReport``) to emit one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Concept codes used below. Kept as plain dicts rather than enum objects
# so the output is pure JSON and trivially diffable in tests.
_CODE_DISTANCE = {"value": "121206", "scheme": "DCM", "meaning": "Distance"}
_CODE_ANGLE = {"value": "121211", "scheme": "DCM", "meaning": "Angle"}
_CODE_AREA = {"value": "42789003", "scheme": "SCT", "meaning": "Area"}
_CODE_IMAGING_MEASUREMENTS = {
    "value": "126010",
    "scheme": "DCM",
    "meaning": "Imaging Measurements",
}
_CODE_MEASUREMENT_GROUP = {
    "value": "125007",
    "scheme": "DCM",
    "meaning": "Measurement Group",
}
_CODE_TRACKING_ID = {"value": "112039", "scheme": "DCM", "meaning": "Tracking Identifier"}
_CODE_TRACKING_UID = {
    "value": "112040",
    "scheme": "DCM",
    "meaning": "Tracking Unique Identifier",
}
_CODE_FINDING = {"value": "121071", "scheme": "DCM", "meaning": "Finding"}

_TOOL_TO_CONCEPT: dict[str, dict[str, str]] = {
    "distance": _CODE_DISTANCE,
    "angle": _CODE_ANGLE,
    "area": _CODE_AREA,
}

_UNIT_TO_UCUM: dict[str, dict[str, str]] = {
    "mm": {"value": "mm", "scheme": "UCUM", "meaning": "millimeter"},
    "deg": {"value": "deg", "scheme": "UCUM", "meaning": "degree"},
    "mm2": {"value": "mm2", "scheme": "UCUM", "meaning": "square millimeter"},
}


def _measurement_group(m: dict[str, Any], index: int) -> dict[str, Any]:
    tool = str(m.get("tool", "")).lower()
    unit = str(m.get("unit", "")).lower()
    concept = _TOOL_TO_CONCEPT.get(tool, {"value": "", "scheme": "", "meaning": tool})
    ucum = _UNIT_TO_UCUM.get(unit, {"value": unit, "scheme": "UCUM", "meaning": unit})
    tracking_id = m.get("client_id") or m.get("id") or f"bvp-m-{index}"

    children: list[dict[str, Any]] = [
        {
            "relationship": "HAS_OBS_CONTEXT",
            "value_type": "TEXT",
            "concept": _CODE_TRACKING_ID,
            "value": str(tracking_id),
        },
        {
            "relationship": "HAS_OBS_CONTEXT",
            "value_type": "UIDREF",
            "concept": _CODE_TRACKING_UID,
            "value": str(m.get("id", tracking_id)),
        },
        {
            "relationship": "CONTAINS",
            "value_type": "NUM",
            "concept": concept,
            "value": float(m.get("value", 0.0)),
            "unit": ucum,
        },
    ]

    if m.get("label"):
        children.append(
            {
                "relationship": "HAS_CONCEPT_MOD",
                "value_type": "TEXT",
                "concept": _CODE_FINDING,
                "value": str(m["label"]),
            }
        )

    # Geometry — spatial coordinates. DICOM SR has SCOORD/SCOORD3D
    # content items; we emit them as a graphic_data list so a writer can
    # render them as a 2D POLYLINE / POLYGON / POINT.
    points = m.get("points") or []
    graphic_type = {"distance": "POLYLINE", "angle": "POLYLINE", "area": "POLYGON"}.get(
        tool, "POLYLINE"
    )
    if points:
        children.append(
            {
                "relationship": "INFERRED_FROM",
                "value_type": "SCOORD",
                "concept": {"value": "111030", "scheme": "DCM", "meaning": "Image Region"},
                "graphic_type": graphic_type,
                "graphic_data": [[float(p.get("x", 0)), float(p.get("y", 0))] for p in points],
                "slice_index": m.get("slice_index"),
                "viewport": m.get("viewport"),
            }
        )

    return {
        "relationship": "CONTAINS",
        "value_type": "CONTAINER",
        "concept": _CODE_MEASUREMENT_GROUP,
        "children": children,
    }


def measurements_to_sr_document(
    *,
    study_uid: str,
    series_uid: str,
    series_description: str | None,
    modality: str | None,
    measurements: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a TID 1500-shaped JSON document from measurement rows."""
    when = (generated_at or datetime.now(UTC)).isoformat()
    imaging_group: dict[str, Any] = {
        "relationship": "CONTAINS",
        "value_type": "CONTAINER",
        "concept": _CODE_IMAGING_MEASUREMENTS,
        "children": [_measurement_group(m, i) for i, m in enumerate(measurements)],
    }
    return {
        "sop_class_uid": "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
        "modality": "SR",
        "template_id": "1500",
        "template_scheme": "DCMR",
        "content_date": when,
        "referenced_study_uid": study_uid,
        "referenced_series_uid": series_uid,
        "referenced_series": {
            "series_instance_uid": series_uid,
            "series_description": series_description,
            "modality": modality,
        },
        "content": {
            "value_type": "CONTAINER",
            "concept": {
                "value": "126000",
                "scheme": "DCM",
                "meaning": "Imaging Measurement Report",
            },
            "children": [imaging_group],
        },
        "measurement_count": len(measurements),
    }
