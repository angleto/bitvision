"""Smart DICOM series splitter.

When SeriesInstanceUID is unreliable (different manufacturers, operator
error, multi-frame acquisitions packed in one series, mixed orientations),
this module re-groups DICOM instances into coherent sub-series using
geometric and acquisition-level tags.

The strategy is intentionally modular: each criterion is a function
that returns a grouping key. When multiple instances within a
SeriesInstanceUID produce different keys, the series is split.

Used by the import CLI and the worker pipeline. The caller decides
whether to create new series rows or flag the originals for review.

**Splitting criteria (in order of priority):**

1. **ImageOrientationPatient** — slices with different orientations
   (e.g. axial + sagittal scout in the same series) are separate
   acquisitions. Rounded to 2 decimal places to tolerate minor float
   drift between manufacturers.

2. **SpacingBetweenSlices / SliceThickness consistency** — if the
   z-spacing jumps by >2× within a series, the series contains
   multiple stacks (e.g. localizer + actual acquisition).

3. **EchoNumbers / EchoTime** — multi-echo sequences (T2* mapping,
   SWI) put all echoes in the same series. Each echo number is a
   logical sub-series.

4. **TemporalPositionIdentifier / TemporalPositionIndex** — dynamic /
   4D sequences (fMRI, DCE perfusion) use this tag to distinguish
   time points.

5. **AcquisitionNumber** — some scanners (Siemens, Philips) use
   different acquisition numbers for logically separate stacks within
   one SeriesInstanceUID.

6. **FrameOfReferenceUID** — if present and different, slices live in
   different coordinate spaces (e.g. different table positions).

The compound key is a tuple of these values. Instances that share the
same compound key belong to the same logical sub-series.
"""

from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pydicom


@dataclass
class InstanceInfo:
    """Lightweight metadata extracted from a DICOM instance for grouping."""

    path_or_key: str
    sop_instance_uid: str
    series_instance_uid: str
    instance_number: int | None

    orientation_key: str
    echo_number: int | None
    temporal_position: int | None
    acquisition_number: int | None
    frame_of_reference_uid: str | None
    slice_location: float | None
    image_position_z: float | None

    # Multi-contrast / multi-echo / diffusion discriminators. These are
    # the tags that separate co-located sub-stacks packed under one
    # SeriesInstanceUID (the classic case: a Philips mDIXON series whose
    # Water / Fat / In-phase / Out-of-phase volumes all share the same
    # z-positions and ImageOrientationPatient and are told apart only by
    # ``ImageType[2]`` + ``EchoTime``). Defaulted so legacy callers that
    # build InstanceInfo without them keep working.
    image_type_key: str | None = None  # ImageType[2:] joined (W / F / IP / OP, M / P, SUB, ...)
    echo_time: float | None = None  # EchoTime rounded to 2 dp
    diffusion_b_value: int | None = None  # DiffusionBValue (DWI)
    stack_id: str | None = None  # Philips StackID

    @property
    def compound_key(self) -> tuple:
        """All-in-one grouping key. Instances with the same key belong
        to the same logical sub-series.

        Indices 0..5 are frozen (orientation/echo/temporal/acq/FoR) so
        the positional reason mapping in ``split_series`` stays valid;
        the multi-contrast discriminators are appended at 6..9.
        """
        return (
            self.series_instance_uid,
            self.orientation_key,
            self.echo_number or 0,
            self.temporal_position or 0,
            self.acquisition_number or 0,
            self.frame_of_reference_uid or "",
            self.image_type_key or "",
            self.echo_time or 0.0,
            self.diffusion_b_value or 0,
            self.stack_id or "",
        )


@dataclass
class SubSeries:
    """A coherent group of instances within a parent SeriesInstanceUID."""

    parent_series_uid: str
    compound_key: tuple
    instances: list[InstanceInfo] = field(default_factory=list)
    reason: str = ""

    @property
    def label(self) -> str:
        parts = []
        sample = self.instances[0] if self.instances else None
        if sample and sample.echo_number:
            parts.append(f"echo={sample.echo_number}")
        if sample and sample.temporal_position:
            parts.append(f"temporal={sample.temporal_position}")
        if sample and sample.acquisition_number:
            parts.append(f"acq={sample.acquisition_number}")
        return " ".join(parts) if parts else "main"


def _round_orientation(ds: pydicom.Dataset) -> str:
    """Return a stable string key for ImageOrientationPatient,
    rounded to 2 decimal places to absorb float noise."""
    iop = getattr(ds, "ImageOrientationPatient", None)
    if not iop or len(iop) < 6:
        return "unknown"
    return ",".join(f"{float(v):.2f}" for v in iop[:6])


def image_type_token(ds: pydicom.Dataset) -> str | None:
    """The contrast/processing token from ``ImageType`` (0008,0008).

    ``ImageType`` value 0 (ORIGINAL/DERIVED) and value 1
    (PRIMARY/SECONDARY) are not discriminating; value 2 onward carries
    the meaningful label — ``W`` / ``F`` / ``IP`` / ``OP`` for a Philips
    mDIXON, ``M`` / ``P`` magnitude vs phase, ``SUB`` for a subtraction.
    Returns the value-2 token uppercased, or ``None`` when absent.
    """
    it = getattr(ds, "ImageType", None)
    if not it:
        return None
    try:
        vals = [str(v).upper().strip() for v in it]
    except TypeError:
        return None
    return vals[2] if len(vals) > 2 and vals[2] else None


def _image_type_key(ds: pydicom.Dataset) -> str | None:
    """Full ImageType discriminator (value 2 onward, joined) — keeps
    ``W`` distinct from ``W\\DERIVED`` so genuinely different processings
    don't collapse into one stack."""
    it = getattr(ds, "ImageType", None)
    if not it:
        return None
    try:
        vals = [str(v).upper().strip() for v in it]
    except TypeError:
        return None
    disc = [v for v in vals[2:] if v]
    return "\\".join(disc) if disc else None


def _echo_time_key(ds: pydicom.Dataset) -> float | None:
    te = getattr(ds, "EchoTime", None)
    if te is None or te == "":
        return None
    try:
        return round(float(te), 2)
    except (TypeError, ValueError):
        return None


def _diffusion_b_value(ds: pydicom.Dataset) -> int | None:
    b = getattr(ds, "DiffusionBValue", None)
    if b is None or b == "":
        return None
    try:
        return round(float(b))
    except (TypeError, ValueError):
        return None


def _stack_id(ds: pydicom.Dataset) -> str | None:
    s = getattr(ds, "StackID", None)
    if s is None or s == "":
        return None
    return str(s)


def extract_instance_info(ds: pydicom.Dataset, *, path_or_key: str = "") -> InstanceInfo:
    """Extract grouping-relevant metadata from a parsed DICOM dataset."""
    pos = getattr(ds, "ImagePositionPatient", None)
    return InstanceInfo(
        path_or_key=path_or_key,
        sop_instance_uid=str(getattr(ds, "SOPInstanceUID", "")),
        series_instance_uid=str(getattr(ds, "SeriesInstanceUID", "")),
        instance_number=_as_int(getattr(ds, "InstanceNumber", None)),
        orientation_key=_round_orientation(ds),
        echo_number=_as_int(getattr(ds, "EchoNumbers", None) or getattr(ds, "EchoNumber", None)),
        temporal_position=_as_int(
            getattr(ds, "TemporalPositionIdentifier", None)
            or getattr(ds, "TemporalPositionIndex", None)
        ),
        acquisition_number=_as_int(getattr(ds, "AcquisitionNumber", None)),
        frame_of_reference_uid=str(getattr(ds, "FrameOfReferenceUID", "") or ""),
        slice_location=_as_float(getattr(ds, "SliceLocation", None)),
        image_position_z=float(pos[2]) if pos and len(pos) >= 3 else None,
        image_type_key=_image_type_key(ds),
        echo_time=_echo_time_key(ds),
        diffusion_b_value=_diffusion_b_value(ds),
        stack_id=_stack_id(ds),
    )


def substack_tag_key(ds: pydicom.Dataset) -> tuple:
    """Tag-based sub-stack discriminator for instances *within one
    series* (the SeriesInstanceUID is dropped — all instances share it
    at pack time). Two instances with the same key are candidates for
    the same coherent volume; a different key means a distinct co-located
    sub-stack (different contrast / echo / b-value / orientation / FoR).

    Reuses ``extract_instance_info`` so the keying logic lives in exactly
    one place (this module), shared by the import-time splitter and the
    volume-build de-interleaver.
    """
    return extract_instance_info(ds).compound_key[1:]


def extract_from_bytes(data: bytes, *, path_or_key: str = "") -> InstanceInfo | None:
    """Parse DICOM bytes (header only) and extract instance info."""
    try:
        ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True, force=True)
    except Exception:
        return None
    uid = getattr(ds, "SOPInstanceUID", None)
    if not uid:
        return None
    return extract_instance_info(ds, path_or_key=path_or_key)


def extract_from_file(path: Path) -> InstanceInfo | None:
    """Parse a DICOM file (header only) and extract instance info."""
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception:
        return None
    uid = getattr(ds, "SOPInstanceUID", None)
    if not uid:
        return None
    return extract_instance_info(ds, path_or_key=str(path))


def split_series(instances: list[InstanceInfo]) -> list[SubSeries]:
    """Group instances into coherent sub-series by compound key.

    If all instances share the same compound key, returns a single
    SubSeries (no split needed). Otherwise returns multiple, each
    with a ``reason`` explaining the split.
    """
    groups: dict[tuple, list[InstanceInfo]] = defaultdict(list)
    for inst in instances:
        groups[inst.compound_key].append(inst)

    def sort_key(i):
        return (i.image_position_z or 0, i.instance_number or 0)

    if len(groups) <= 1:
        key = next(iter(groups)) if groups else ()
        return [
            SubSeries(
                parent_series_uid=instances[0].series_instance_uid if instances else "",
                compound_key=key,
                instances=sorted(groups.get(key, []), key=sort_key),
                reason="no split needed",
            )
        ]

    result: list[SubSeries] = []
    reference_key = max(groups, key=lambda k: len(groups[k]))

    for key, insts in groups.items():
        reasons = []
        if key[1] != reference_key[1]:
            reasons.append("different orientation")
        if key[2] != reference_key[2]:
            reasons.append(f"echo {key[2]}")
        if key[3] != reference_key[3]:
            reasons.append(f"temporal position {key[3]}")
        if key[4] != reference_key[4]:
            reasons.append(f"acquisition {key[4]}")
        if key[5] != reference_key[5]:
            reasons.append("different frame of reference")
        if key[6] != reference_key[6]:
            reasons.append(f"image type {key[6]}")
        if key[7] != reference_key[7]:
            reasons.append(f"echo time {key[7]}")
        if key[8] != reference_key[8]:
            reasons.append(f"b-value {key[8]}")
        if key[9] != reference_key[9]:
            reasons.append(f"stack {key[9]}")

        result.append(
            SubSeries(
                parent_series_uid=insts[0].series_instance_uid,
                compound_key=key,
                instances=sorted(insts, key=sort_key),
                reason=", ".join(reasons) if reasons else "primary group",
            )
        )

    result.sort(key=lambda s: -len(s.instances))
    return result


def analyze_series_from_files(paths: list[Path]) -> list[SubSeries]:
    """Convenience: scan a list of DICOM files and return sub-series."""
    infos = []
    for p in paths:
        info = extract_from_file(p)
        if info:
            infos.append(info)
    return split_series(infos)


def _as_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        if hasattr(v, "__iter__") and not isinstance(v, str):
            return int(next(iter(v)))
        return int(v)
    except (TypeError, ValueError, StopIteration):
        return None


def _as_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
