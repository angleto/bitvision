"""Geometric measurements over DICOM series (Sprint 6, ADR — agents API).

Two operations are supported in v0:

* :func:`compute_distance` — euclidean distance between two 3D points
  expressed in pixel-space ``(i, j, k)`` (column, row, slice index).
  The points are converted to millimetres via ``PixelSpacing`` and
  ``SliceThickness`` / ``SpacingBetweenSlices`` from the first
  instance's allowlisted DICOM metadata.
* :func:`compute_volume` — bounding-box volume of a 3D ROI given as
  two opposite corners ``(i0, j0, k0)`` and ``(i1, j1, k1)`` in pixel
  space. The ellipsoid / mask volume case is left to a future
  iteration when SimpleITK reformat is available.

Why pixel-space input?
* Most agent calls have already located a structure on a slice
  (``i, j``) plus a slice index ``k``. Converting to patient-space
  (mm relative to ImagePositionPatient) requires DICOM ImageOrientation
  matrix multiplication, which is straightforward but adds shape
  dependencies; we accept pixel input today and produce mm output.

Failure modes:
* ``MissingSpacingError`` — ``PixelSpacing`` or slice-thickness
  derivable spacing is absent. The agent gets 422 ``measurement_unavailable``
  with the missing tag list in the body.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


class MissingSpacingError(ValueError):
    """Raised when DICOM metadata is too sparse to compute mm output."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"missing DICOM tags: {missing}")
        self.missing = missing


@dataclass(slots=True)
class Spacing3D:
    column_mm: float  # x — across columns
    row_mm: float  # y — across rows
    slice_mm: float  # z — between slices


def spacing_from_meta(meta: dict[str, Any]) -> Spacing3D:
    """Extract a 3D spacing tuple from the allowlisted DICOM meta.

    ``PixelSpacing`` is ``[row_mm, column_mm]`` per DICOM PS3.3 (note
    the order — DICOM specifies row first). The slice spacing is
    pulled from ``SpacingBetweenSlices`` when present, falling back
    to ``SliceThickness`` (less accurate but the most common case).
    """
    missing: list[str] = []
    pixel_spacing = meta.get("PixelSpacing")
    if not pixel_spacing or not isinstance(pixel_spacing, list) or len(pixel_spacing) < 2:
        missing.append("PixelSpacing")
    slice_mm: float | None = None
    sbs = meta.get("SpacingBetweenSlices")
    if isinstance(sbs, (int, float)) and sbs > 0:
        slice_mm = float(sbs)
    else:
        st = meta.get("SliceThickness")
        if isinstance(st, (int, float)) and st > 0:
            slice_mm = float(st)
        else:
            missing.append("SliceThickness")
    if missing:
        raise MissingSpacingError(missing)
    assert pixel_spacing is not None and slice_mm is not None
    row_mm = float(pixel_spacing[0])
    col_mm = float(pixel_spacing[1])
    return Spacing3D(column_mm=col_mm, row_mm=row_mm, slice_mm=slice_mm)


def compute_distance(
    a_ijk: tuple[float, float, float],
    b_ijk: tuple[float, float, float],
    spacing: Spacing3D,
) -> dict[str, Any]:
    """Euclidean distance between ``a`` and ``b`` in millimetres.

    Returns a dict with the input points (echoed for the agent),
    the per-axis delta in mm, and the magnitude.
    """
    dx = (b_ijk[0] - a_ijk[0]) * spacing.column_mm
    dy = (b_ijk[1] - a_ijk[1]) * spacing.row_mm
    dz = (b_ijk[2] - a_ijk[2]) * spacing.slice_mm
    distance_mm = math.sqrt(dx * dx + dy * dy + dz * dz)
    return {
        "a_ijk": list(a_ijk),
        "b_ijk": list(b_ijk),
        "delta_mm": [dx, dy, dz],
        "distance_mm": distance_mm,
        "spacing_mm": [spacing.column_mm, spacing.row_mm, spacing.slice_mm],
    }


def compute_volume(
    p0_ijk: tuple[float, float, float],
    p1_ijk: tuple[float, float, float],
    spacing: Spacing3D,
) -> dict[str, Any]:
    """Axis-aligned bounding-box volume of an ROI defined by two
    opposite corners in pixel space.

    The bbox is normalised so the larger coordinate per axis is
    the upper bound. Output volume is in mm^3 plus the per-axis
    extent in mm.
    """
    xs = sorted([p0_ijk[0], p1_ijk[0]])
    ys = sorted([p0_ijk[1], p1_ijk[1]])
    zs = sorted([p0_ijk[2], p1_ijk[2]])
    extent_mm = (
        (xs[1] - xs[0]) * spacing.column_mm,
        (ys[1] - ys[0]) * spacing.row_mm,
        (zs[1] - zs[0]) * spacing.slice_mm,
    )
    volume_mm3 = extent_mm[0] * extent_mm[1] * extent_mm[2]
    return {
        "p0_ijk": list(p0_ijk),
        "p1_ijk": list(p1_ijk),
        "extent_mm": list(extent_mm),
        "volume_mm3": volume_mm3,
        "volume_ml": volume_mm3 / 1000.0,
        "spacing_mm": [spacing.column_mm, spacing.row_mm, spacing.slice_mm],
    }


__all__ = [
    "MissingSpacingError",
    "Spacing3D",
    "compute_distance",
    "compute_volume",
    "spacing_from_meta",
]
