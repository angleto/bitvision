"""De-facing seam for recognizable-visual-feature risk (M6, low-risk tier).

``classify_pixel_risk`` flags head/face CT/MR/PT as ``low``: a recognizable face
can be surface-rendered from the volume (PS3.15 "Clean Recognizable Visual
Features Option"). Removing that is "de-facing".

Real de-facing needs the 3D volume plus a registration/segmentation method
(FreeSurfer ``mri_deface`` / ``pydeface`` / a CNN); that is a future GPU/3D tier.
This module defines the seam + a fail-safe null default + a conservative 2D
heuristic, so the capability is pluggable behind one Protocol without touching
callers.

* :class:`NullDefacer`, no-op. Records that de-facing did NOT run (the result
  carries ``residual_suspect=True`` + a reason) so an egress gate consuming that
  signal can hold the instance for human review; it NEVER claims features were
  removed.
* :class:`HeuristicFaceMasker`, a CONSERVATIVE 2D placeholder. Masks an anterior
  band of each slice assuming standard axial radiological orientation. It is not
  validated de-facing and it refuses body parts where the anterior region is the
  finding (orbit/sinus/face/TMJ), to avoid destroying clinical content. A real
  3D defacer drops in behind the same Protocol.

The PS3.15 ``RecognizableVisualFeatures=NO`` + CID 7050 ``113102`` provenance is
written by ``pixel_deid.mark_visual_features_removed`` ONLY after a human accepts
the defaced result, never automatically, mirroring the Clean Pixel Data flow.

NOTE: this seam only PRODUCES the review signal (``residual_suspect`` +
``face_deid_reason``). Wiring an egress gate (PixelPhiCheck / training cohort
export) to consume it is follow-up work; with de-facing off by default the
signal is inert, so that gap is not a current-behavior regression.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x, y, w, h

# Body parts where the anterior face region IS the clinical finding, never
# heuristically mask these (it would destroy the ROI). Defacing them correctly
# requires a real 3D method that preserves the diagnostic region.
_FACE_IS_ROI: frozenset[str] = frozenset(
    {"ORBIT", "ORBITS", "SINUS", "FACE", "TMJ", "MAXILLA", "MANDIBLE", "NECK"}
)
# Body parts where a conservative anterior mask is acceptable ("" = unspecified).
_DEFACE_ELIGIBLE: frozenset[str] = frozenset({"HEAD", "SKULL", "BRAIN", ""})


@dataclass(frozen=True)
class DefaceResult:
    applied: bool
    boxes_per_frame: list[list[Box]] = field(default_factory=list)
    reason: str = ""


@runtime_checkable
class Defacer(Protocol):
    def deface(self, frames: list[np.ndarray], *, body_part: str) -> DefaceResult: ...


class NullDefacer:
    """No-op: records that de-facing did not run. Never claims removal."""

    def deface(self, frames: list[np.ndarray], *, body_part: str) -> DefaceResult:
        return DefaceResult(applied=False, reason="null_defacer_no_op")


@dataclass
class HeuristicFaceMasker:
    """Conservative 2D placeholder (see module docstring). Masks an anterior band
    assuming standard axial radiological orientation; refuses ROI-bearing regions.
    NOT a substitute for validated 3D de-facing."""

    anterior_fraction: float = 0.4

    def deface(self, frames: list[np.ndarray], *, body_part: str) -> DefaceResult:
        bp = (body_part or "").upper()
        if bp in _FACE_IS_ROI:
            return DefaceResult(applied=False, reason=f"face_is_roi:{bp}")
        if bp not in _DEFACE_ELIGIBLE:
            return DefaceResult(applied=False, reason=f"body_part_not_eligible:{bp}")
        boxes_per_frame: list[list[Box]] = []
        for frame in frames:
            shape = np.asarray(frame).shape
            h, w = shape[0], shape[1]
            band = max(1, round(h * self.anterior_fraction))
            boxes_per_frame.append([(0, 0, w, band)])
        return DefaceResult(
            applied=True, boxes_per_frame=boxes_per_frame, reason="heuristic_anterior_band"
        )


def get_defacer() -> Defacer | None:
    """Resolve the configured defacer, or None when de-facing is disabled
    (default), face-risk then ships as today, unchanged."""
    from bvphoenix.config import get_settings

    s = get_settings()
    if not getattr(s, "face_deid_enabled", False):
        return None
    mode = (getattr(s, "face_deid_mode", "null") or "null").strip().lower()
    if mode == "heuristic":
        return HeuristicFaceMasker()
    return NullDefacer()


__all__ = [
    "DefaceResult",
    "Defacer",
    "HeuristicFaceMasker",
    "NullDefacer",
    "get_defacer",
]
