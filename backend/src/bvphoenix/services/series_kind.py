"""Classify what *kind* of thing a DICOM series is, for the multiphase
contrast viewer.

A contrast-CT study is a pile of series, but only a few of them are
**reviewable phase volumes** — the axial source acquisitions a radiologist
reads side by side (non-contrast / arterial / portal / delayed / ...). The
rest is clutter that must never be auto-opened as a "phase":

* localizers / scout / topogram / scanogram (2-3 slice planning images);
* secondary captures / screen saves (a single screenshot frame);
* dose reports ("Rapporto dose", "Dose Record");
* bolus-tracking / Smart-Prep / test-bolus monitoring series;
* MPR **reformats** (sagittal / coronal / oblique / MIP / VR / 3D) — these
  ARE the same contrast phase, but they are not the axial source the
  washout/HU machinery samples, so they are not offered as a phase pane.

This module is the single source of truth for that policy so the
classifier (which must not label a Scout as a phase) and the phases
manifest (which exposes ``is_reviewable_phase`` to the viewer and to MCP
agents) agree byte-for-byte. It is pure and side-effect-free.

The geometry signal (``ImageOrientationPatient`` slice axis) is authoritative
for axial-vs-reformat; the description regexes are the fallback for series
that are not packed yet (no derived geometry) and the only signal for
localizer/dose/prep junk (which carry no special geometry).
"""

from __future__ import annotations

import re

# Minimum received instances for a CT "series" to be a reviewable volume.
# Below this it is a scout (2-3), a screenshot/dose report (1), or a
# bolus-prep monitoring loop (a handful) — never a phase to open.
MIN_VOLUME_INSTANCES = 16

# Series whose description marks them as non-reviewable regardless of size:
# planning localizers, single-frame captures, dose reports, and the
# bolus-tracking / Smart-Prep monitoring series that precede the real
# acquisition. Word-boundaried so "scout" does not catch "scoutless" etc.
_NON_REVIEWABLE_DESC = re.compile(
    r"\b(scout|topogram|topogramma|scanogram|localiz\w*|localizz\w*|"
    r"surview|pilot|screen\s*save|screensave|secondary\s*capture|"
    r"dose\s*record|dose\s*report|rapporto\s*dose|"
    r"smart\s*prep|bolus\s*track\w*|test\s*bolus|monitoring|prep\s*smart|"
    r"serie\s*prep)\b",
    re.IGNORECASE,
)

# Multiplanar reformats / renderings: same phase, not the axial source.
_REFORMAT_DESC = re.compile(
    r"\b(sag|sagittal|sagittale|cor|coronal|coronale|"
    r"mpr|mip|minip|reformat\w*|reform\w*|ricostr\w*|rimformat\w*|"
    r"curved|cpr|vr|vrt|3d|ssd)\b",
    re.IGNORECASE,
)


def plane_from_direction(direction: list[float] | None) -> str | None:
    """Derive the acquisition plane from a packed volume's direction
    cosines (``[Rx,Ry,Rz, Cx,Cy,Cz, Sx,Sy,Sz]`` — see
    ``volumes.compute_volume_geometry``).

    The slice axis (3rd triplet) is the through-plane normal:
    dominant Z → axial, dominant X → sagittal, dominant Y → coronal.
    A normal that is not within ~45° of a cardinal axis is ``oblique``.
    Returns ``None`` when no direction is available.
    """
    if not direction or len(direction) < 9:
        return None
    sx, sy, sz = abs(direction[6]), abs(direction[7]), abs(direction[8])
    biggest = max(sx, sy, sz)
    if biggest < 0.71:  # cos(45°) ≈ 0.707 — no dominant cardinal axis
        return "oblique"
    if biggest == sz:
        return "axial"
    if biggest == sx:
        return "sagittal"
    return "coronal"


def is_reformat(description: str | None, plane: str | None) -> bool:
    """True when the series is an MPR reformat / rendering rather than an
    axial source. Geometry wins; description is the unpacked fallback."""
    if plane in ("sagittal", "coronal", "oblique"):
        return True
    if plane == "axial":
        return False  # geometry says axial — trust it over a stray token
    return bool(description and _REFORMAT_DESC.search(description))


def is_non_reviewable_desc(description: str | None) -> bool:
    """True for localizer / capture / dose / bolus-prep junk."""
    return bool(description and _NON_REVIEWABLE_DESC.search(description))


def is_reviewable_phase(
    *,
    modality: str | None,
    instance_count: int | None,
    plane: str | None = None,
    description: str | None = None,
) -> bool:
    """Is this series a reviewable contrast-phase volume — an axial CT
    source acquisition with enough slices to read, not a localizer /
    capture / dose report / bolus-prep / MPR reformat?

    ``plane`` (from packed geometry) is authoritative for axial-vs-reformat;
    when it is ``None`` (series not packed) the description carries the
    decision. Conservative: a series we cannot place is judged on what we
    do know (size + description), never auto-excluded for missing geometry.
    """
    if (modality or "").upper() != "CT":
        return False
    if (instance_count or 0) < MIN_VOLUME_INSTANCES:
        return False
    if is_non_reviewable_desc(description):
        return False
    return not is_reformat(description, plane)
