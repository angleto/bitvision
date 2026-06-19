"""Unit tests for ``services.series_kind`` — the policy that decides which
series are reviewable axial contrast-phase volumes versus clutter (scouts,
screenshots, dose reports, bolus-prep loops, MPR reformats).

Pure: no DB, no S3. Grounded in the real multiphase study that exposed the
bug (every reformat / scout / dose report opening as a phase pane).
"""

from __future__ import annotations

import numpy as np

from bvphoenix.services.series_kind import (
    is_non_reviewable_desc,
    is_reformat,
    is_reviewable_phase,
    plane_from_direction,
)


def _slice_dir(iop: list[float]) -> list[float]:
    """Build a full direction (row, col, slice) from a 6-tuple IOP, the way
    ``volumes.compute_volume_geometry`` does (slice = row × col)."""
    row = np.array(iop[:3], dtype=float)
    col = np.array(iop[3:], dtype=float)
    s = np.cross(row, col)
    return [*iop, *s.tolist()]


def test_plane_from_axial() -> None:
    assert plane_from_direction(_slice_dir([1, 0, 0, 0, 1, 0])) == "axial"


def test_plane_from_sagittal() -> None:
    # Real "COR"-labelled series whose IOP is actually a sagittal plane:
    # geometry is ground truth, not the (wrong) description.
    assert plane_from_direction(_slice_dir([0, 1, 0, 0, 0, -1])) == "sagittal"


def test_plane_from_coronal() -> None:
    assert plane_from_direction(_slice_dir([1, 0, 0, 0, 0, -1])) == "coronal"


def test_plane_oblique_when_no_dominant_axis() -> None:
    # A 45° tilt between two axes has no dominant cardinal slice normal.
    d = _slice_dir([1, 0, 0, 0, 0.707, 0.707])
    assert plane_from_direction(d) == "oblique"


def test_plane_none_without_direction() -> None:
    assert plane_from_direction(None) is None
    assert plane_from_direction([1, 0, 0]) is None


def test_geometry_overrides_reformat_token() -> None:
    # Description says "SAG" but geometry says axial -> trust geometry.
    assert is_reformat("SAG recon", "axial") is False
    # No geometry -> the description carries it.
    assert is_reformat("SAG recon", None) is True
    assert is_reformat("Basale", None) is False


def test_non_reviewable_descriptions() -> None:
    for d in ("Scout", "Screen Save", "Rapporto dose", "Serie Prep Smart", "Smart Prep"):
        assert is_non_reviewable_desc(d) is True
    assert is_non_reviewable_desc("Basale") is False


def test_real_study_reviewable_set() -> None:
    """The actual 70ce04b1 study: only Basale, Polmone (axial volumes) are
    reviewable; tardiva too — but Scout / SAG / COR / prep / dose are not."""
    cases = [
        ("Scout", 2, None, False),
        ("Basale", 258, "axial", True),
        ("Polmone 1.25", 277, "axial", True),
        ("tardiva dopo portale", 195, "axial", True),
        ("Screen Save", 1, None, False),
        ("Serie Prep Smart", 6, None, False),
        ("SAG", 152, "sagittal", False),
        ("COR", 148, "sagittal", False),
        ("Rapporto dose", 1, None, False),
    ]
    for desc, n, plane, expected in cases:
        assert (
            is_reviewable_phase(modality="CT", instance_count=n, plane=plane, description=desc)
            is expected
        ), desc


def test_non_ct_never_reviewable() -> None:
    assert (
        is_reviewable_phase(modality="SR", instance_count=999, plane="axial", description="x")
        is False
    )


def test_reformat_excluded_even_when_large_axial_unknown() -> None:
    # Unpacked coronal reformat (no geometry) excluded by description.
    assert (
        is_reviewable_phase(modality="CT", instance_count=300, plane=None, description="COR MPR")
        is False
    )
