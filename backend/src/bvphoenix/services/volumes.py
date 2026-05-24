"""Pack a DICOM series into a compact Float32 volume for the browser.

Binary format (little-endian, suitable for ``Float32Array`` after a
``DataView`` header parse):

    uint32 nx, uint32 ny, uint32 nz
    float32 sx, float32 sy, float32 sz           (voxel spacing, mm)
    float32 value_min, float32 value_max         (global range)
    float32 scalars[nx * ny * nz]                (x-fastest)

This is used by ``GET /api/series/{id}/volume.raw`` and is a deliberately
narrow contract: the front-end can build a ``vtkImageData`` from it
without an extra DICOM/NIfTI reader dependency. Computed results are
cached in the derivatives bucket so a second request is instant.

A low-resolution preview variant (``pack_low_res``) is served from
``GET /api/series/{id}/volume-preview.raw`` for progressive loading —
the viewer can render something ~1/8th the size almost immediately,
then swap in the full-res blob once it finishes downloading.

The 32-byte header is intentionally frozen — new display hints (e.g.
PhotometricInterpretation, auto-invert flag) ride on the companion
``/api/series/{id}/display-metadata`` endpoint instead, so existing
cached derivatives stay readable by older clients.

Future: once the worker pipeline packs NIfTI on ingest the cached blob
comes from there instead of being computed on first access here.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import TypedDict

import numpy as np
import pydicom

from bvphoenix.storage import S3Storage

DERIVATIVE_KIND = "volume_f32"
DERIVATIVE_KIND_PREVIEW = "volume_f32_preview"
DERIVATIVE_FORMAT = "raw"
HEADER_STRUCT = struct.Struct("<3I 3f 2f")  # 32 bytes — do not grow (compat)


# DICOM SOP class UIDs that never represent a stackable 3D volume:
# secondary captures, presentation states, structured reports,
# key object selections, single-frame X-ray / mammography / fluoroscopy.
# A series whose every instance falls in this set must be served by the
# 2D viewer; the volume endpoint returns 404 so the frontend falls
# through to ``<Series2DViewer>`` instead of trying to assemble one.
NON_VOLUMETRIC_SOP_CLASSES: frozenset[str] = frozenset(
    {
        # Secondary capture (screenshot from a workstation, "saved
        # state" PNG, etc.). Common when a clinician exports an
        # annotated image from PACS as part of the case.
        "1.2.840.10008.5.1.4.1.1.7",
        "1.2.840.10008.5.1.4.1.1.7.1",  # Multi-frame Single Bit SC
        "1.2.840.10008.5.1.4.1.1.7.2",  # Multi-frame Grayscale Byte SC
        "1.2.840.10008.5.1.4.1.1.7.3",  # Multi-frame Grayscale Word SC
        "1.2.840.10008.5.1.4.1.1.7.4",  # Multi-frame True Color SC
        # Presentation states (annotation overlays).
        "1.2.840.10008.5.1.4.1.1.11.1",  # Grayscale Softcopy Pres State
        "1.2.840.10008.5.1.4.1.1.11.2",  # Color Softcopy Pres State
        "1.2.840.10008.5.1.4.1.1.11.3",  # Pseudo-Color
        "1.2.840.10008.5.1.4.1.1.11.4",  # Blending
        "1.2.840.10008.5.1.4.1.1.11.5",  # XA/XRF Grayscale
        # Structured reports / key object / waveforms.
        "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
        "1.2.840.10008.5.1.4.1.1.88.22",  # Enhanced SR
        "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
        "1.2.840.10008.5.1.4.1.1.88.34",  # Comprehensive 3D SR
        "1.2.840.10008.5.1.4.1.1.88.40",  # Procedure Log
        "1.2.840.10008.5.1.4.1.1.88.50",  # Mammography CAD SR
        "1.2.840.10008.5.1.4.1.1.88.65",  # Chest CAD SR
        "1.2.840.10008.5.1.4.1.1.88.67",  # X-Ray Radiation Dose SR
        "1.2.840.10008.5.1.4.1.1.88.59",  # Key Object Selection
        # Encapsulated PDF / CDA — clinical reports stored as DICOM.
        "1.2.840.10008.5.1.4.1.1.104.1",  # Encapsulated PDF
        "1.2.840.10008.5.1.4.1.1.104.2",  # Encapsulated CDA
    }
)


class NonVolumetricSeriesError(ValueError):
    """Raised by ``pack_series`` when a series cannot be assembled into a
    coherent 3D volume.

    Two common causes:

    * Every instance carries a SOP class that has no z-axis (Secondary
      Capture, Presentation State, Structured Report, Encapsulated PDF).
    * The series has multiple instances but their
      ``ImageOrientationPatient`` cosines disagree by more than a small
      threshold, so they are projections at different angles (typical
      CT scout / topogram with AP + LAT) rather than slices through a
      common volume.

    The volume endpoint catches this and replies 404 so the frontend
    falls through to its 2D slice viewer fallback. The exception is a
    subclass of ``ValueError`` so older callers that catch ``ValueError``
    continue to work.
    """


class DisplayMetadataDict(TypedDict):
    photometric_interpretation: str | None
    invert: bool
    pixel_spacing: tuple[float, float]  # [sx, sy] mm — column, row
    rows: int
    columns: int
    image_orientation_patient: tuple[float, float, float, float, float, float] | None
    primary_plane: str  # "axial" | "sagittal" | "coronal" | "oblique" | "unknown"


def _classify_primary_plane(
    iop: tuple[float, float, float, float, float, float] | None,
) -> str:
    """Map the row+column direction cosines to the primary review plane.

    The slice normal (row × col) tells us which body axis the slice
    plane is perpendicular to. We pick the dominant axis of |normal|:

    * |z| > 0.85 → axial (perpendicular to head-foot)
    * |x| > 0.85 → sagittal (perpendicular to left-right)
    * |y| > 0.85 → coronal (perpendicular to AP)
    * otherwise → oblique (the viewer keeps an axial primary)

    The 0.85 cutoff (~32°) tolerates angled spine MRs that aren't quite
    on-axis without flipping into "oblique" too eagerly.
    """
    if iop is None:
        return "unknown"
    rx, ry, rz, cx, cy, cz = iop
    nx = ry * cz - rz * cy
    ny = rz * cx - rx * cz
    nz = rx * cy - ry * cx
    ax, ay, az = abs(nx), abs(ny), abs(nz)
    if az >= 0.85:
        return "axial"
    if ax >= 0.85:
        return "sagittal"
    if ay >= 0.85:
        return "coronal"
    return "oblique"


def read_display_metadata(ds: pydicom.Dataset) -> DisplayMetadataDict:
    """Extract display hints (photometric interpretation, pixel spacing,
    image orientation) from a DICOM dataset. Used by the
    ``display-metadata`` endpoint so the viewer can auto-invert
    MONOCHROME1, correct non-square pixel spacing, and pick the right
    primary plane (axial vs sagittal vs coronal) for the hanging
    protocol."""
    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "").upper().strip()
    # PixelSpacing is [row_spacing, col_spacing] = [sy, sx] in DICOM.
    ps = getattr(ds, "PixelSpacing", None)
    if ps is not None and len(ps) >= 2:
        sy, sx = float(ps[0]), float(ps[1])
    else:
        sy = sx = 1.0
    raw_iop = getattr(ds, "ImageOrientationPatient", None)
    iop: tuple[float, float, float, float, float, float] | None = None
    if raw_iop is not None and len(raw_iop) >= 6:
        try:
            iop = (
                float(raw_iop[0]),
                float(raw_iop[1]),
                float(raw_iop[2]),
                float(raw_iop[3]),
                float(raw_iop[4]),
                float(raw_iop[5]),
            )
        except (TypeError, ValueError):
            iop = None
    return {
        "photometric_interpretation": photometric or None,
        "invert": photometric == "MONOCHROME1",
        "pixel_spacing": (sx, sy),
        "rows": int(getattr(ds, "Rows", 0) or 0),
        "columns": int(getattr(ds, "Columns", 0) or 0),
        "image_orientation_patient": iop,
        "primary_plane": _classify_primary_plane(iop),
    }


@dataclass(frozen=True, slots=True)
class PackedVolume:
    bytes_: bytes
    nx: int
    ny: int
    nz: int
    spacing: tuple[float, float, float]
    value_range: tuple[float, float]

    @property
    def size(self) -> int:
        return len(self.bytes_)


def _sort_key(ds: pydicom.Dataset) -> tuple[float, int]:
    """Sort by z-coordinate (axial imaging convention); fall back to
    instance number when ImagePositionPatient is missing."""
    pos = getattr(ds, "ImagePositionPatient", None)
    z = float(pos[2]) if pos else 0.0
    inst = int(getattr(ds, "InstanceNumber", 0) or 0)
    return (z, inst)


def _orientations_consistent(datasets: list[pydicom.Dataset]) -> bool:
    """Return True iff every dataset shares a near-identical
    ``ImageOrientationPatient`` (the row + column direction cosines).

    Threshold: cosine dot product within 0.01 of unity, applied to both
    row and column vectors. CT scouts, when stored as a "series" with
    AP + LAT projections, fail this check and signal that we are looking
    at separate 2D acquisitions rather than slices through a common
    volume. Datasets that omit IOP entirely are treated as compatible
    (legacy CR/DX data without orientation tags).
    """
    if len(datasets) < 2:
        return True
    ref = None
    for ds in datasets:
        iop = getattr(ds, "ImageOrientationPatient", None)
        if iop is None:
            continue
        try:
            row = np.array([float(iop[0]), float(iop[1]), float(iop[2])])
            col = np.array([float(iop[3]), float(iop[4]), float(iop[5])])
        except (TypeError, IndexError, ValueError):
            continue
        if ref is None:
            ref = (row, col)
            continue
        # Cosine similarity, allowing the cosines to be flipped (180°).
        row_dot = abs(float(np.dot(ref[0], row)))
        col_dot = abs(float(np.dot(ref[1], col)))
        if row_dot < 0.99 or col_dot < 0.99:
            return False
    return True


def _all_non_volumetric(datasets: list[pydicom.Dataset]) -> bool:
    """Return True iff every dataset's SOP class is in
    ``NON_VOLUMETRIC_SOP_CLASSES``.

    A mixed-class series (e.g. a CT image plus a Secondary Capture) is
    *not* flagged: callers that have at least one stackable instance
    can still try to pack it. The endpoint pre-check upstream already
    handles the all-SC / all-PR / all-SR shortcut without an S3 round
    trip.
    """
    if not datasets:
        return False
    for ds in datasets:
        sop = str(getattr(ds, "SOPClassUID", "") or "")
        if sop and sop not in NON_VOLUMETRIC_SOP_CLASSES:
            return False
    return True


def pack_series(
    *,
    storage: S3Storage,
    instance_entries: list[tuple[str, str]],  # [(bucket, key), ...] in DB order
) -> PackedVolume:
    """Fetch every instance from S3, decode its pixel data, stack into
    a single Float32 volume.

    Supports both classic single-frame DICOM (one slice per instance)
    and Enhanced PET multi-frame (one instance encodes the whole
    volume; Addendum B). Detection: if any input has
    ``NumberOfFrames > 1`` we treat that file as a multi-frame
    container and pull rescale + position from
    ``PerFrameFunctionalGroupsSequence``.
    """
    if not instance_entries:
        raise ValueError("series has no instances")

    datasets: list[pydicom.Dataset] = []
    for bucket, key in instance_entries:
        data = storage.get_object_bytes(bucket=bucket, key=key)
        ds = pydicom.dcmread(io.BytesIO(data))
        datasets.append(ds)

    # Reject non-volumetric series before we try to pack them. The two
    # signals: a SOP class that's never a stack (SC, PR, SR, ...), and
    # a multi-instance series whose orientations disagree (CT scout
    # AP + LAT, "Saved State" exports of multiple unrelated views). The
    # API translates this into a 404 so the frontend falls back to the
    # slice-by-slice 2D viewer.
    if _all_non_volumetric(datasets):
        raise NonVolumetricSeriesError(
            "series consists entirely of non-volumetric SOP classes "
            "(secondary capture / presentation state / structured report)"
        )
    if not _orientations_consistent(datasets):
        raise NonVolumetricSeriesError(
            "series instances have inconsistent ImageOrientationPatient; "
            "they are separate 2D projections (e.g. CT scout AP + LAT), "
            "not slices through a single volume"
        )

    # Enhanced PET / multi-frame branch (Addendum B). When at least
    # one instance carries multi-frame data, route the whole batch
    # through the multi-frame packer:
    #   * single multi-frame file → unpack as one volume
    #   * multiple files sharing ``ConcatenationUID`` → stitch in
    #     order of ``InConcatenationNumber`` so a total-body PET
    #     spread across several files reads as one volume
    #     (Addendum B §6)
    multi_datasets = [ds for ds in datasets if int(getattr(ds, "NumberOfFrames", 0) or 0) > 1]
    if multi_datasets:
        if len(multi_datasets) == 1 and len(datasets) == 1:
            return _pack_enhanced_multiframe(multi_datasets[0])
        # Multiple multi-frame files. Cluster by ConcatenationUID:
        # if all share one UID we have a concatenated volume; if
        # they don't share, we fall back to using just the first
        # (with a console-grade warning — not bubbled to the user
        # because the call signature here is sync).
        cuids = {getattr(ds, "ConcatenationUID", None) for ds in multi_datasets}
        cuids.discard(None)
        if len(cuids) == 1:
            return _pack_concatenated_multiframe(multi_datasets)
        # Heterogeneous Enhanced PETs in the same series — pick the
        # first as a best-effort.
        return _pack_enhanced_multiframe(multi_datasets[0])

    datasets.sort(key=_sort_key)

    # Shape + spacing come from the first slice; we assume uniform.
    first = datasets[0]
    rows = int(first.Rows)
    cols = int(first.Columns)
    nz = len(datasets)
    pixel_spacing = getattr(first, "PixelSpacing", [1.0, 1.0])
    sx, sy = float(pixel_spacing[1]), float(pixel_spacing[0])
    slice_thickness = float(getattr(first, "SliceThickness", 1.0) or 1.0)
    # Prefer z-delta between the first two slices when available.
    if len(datasets) >= 2:
        p0 = getattr(datasets[0], "ImagePositionPatient", None)
        p1 = getattr(datasets[1], "ImagePositionPatient", None)
        if p0 and p1:
            slice_thickness = abs(float(p1[2]) - float(p0[2])) or slice_thickness

    volume = np.empty((nz, rows, cols), dtype=np.float32)
    for i, ds in enumerate(datasets):
        arr = ds.pixel_array.astype(np.float32)
        # Apply rescale slope/intercept when present (CT Hounsfield etc.).
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        if slope != 1.0 or intercept != 0.0:
            arr = arr * slope + intercept
        volume[i] = arr

    # Sanitize NaN / inf / negative-near-zero per Addendum A §9.3.
    # Some PET scanners emit NaN or subnormals (~10⁻³⁸) on background
    # voxels; in the shader they propagate into divisions/comparisons
    # and produce scattered black pixels or NaN ROI stats. Replacing
    # them with 0 at packing time keeps the rendering pipeline pure.
    bad_mask = ~np.isfinite(volume)
    if bad_mask.any():
        volume[bad_mask] = 0.0
    # Clamp tiny subnormal magnitudes to 0 — they never represent
    # diagnostic signal and trip up GPU float comparisons.
    volume[np.abs(volume) < 1e-30] = 0.0

    vmin = float(volume.min())
    vmax = float(volume.max())

    header = HEADER_STRUCT.pack(cols, rows, nz, sx, sy, slice_thickness, vmin, vmax)
    # Reorder to x-fastest (columns) — already the natural axis-2 order
    # of the stacked array, so a plain ravel works.
    return PackedVolume(
        bytes_=header + volume.tobytes(order="C"),
        nx=cols,
        ny=rows,
        nz=nz,
        spacing=(sx, sy, slice_thickness),
        value_range=(vmin, vmax),
    )


def _detect_4d(ds: pydicom.Dataset) -> tuple[bool, int]:
    """Detect a temporal dimension on a multi-frame DICOM via
    ``DimensionIndexSequence``. Returns ``(is_4d, n_time_frames)``.
    For 4D datasets we'll pick a single representative time frame
    in the spatial pack — full temporal navigation is exposed via a
    separate API in a follow-up. Mirrors Addendum B §5.
    """
    dis = getattr(ds, "DimensionIndexSequence", None)
    if not dis or len(dis) < 2:
        return False, 1
    # Heuristic: if at least one DimensionIndex points at
    # ``TemporalPositionIndex`` (0020,9128) or ``StackPositionNumber``
    # is paired with a second axis, it's 4D.
    has_temporal = False
    for it in dis:
        ptr = getattr(it, "DimensionIndexPointer", None)
        if ptr is None:
            continue
        # ``DimensionIndexPointer`` is a Tag (group, element).
        try:
            group = int(ptr.group)
            element = int(ptr.element)
        except AttributeError:
            continue
        if (group, element) in {(0x0020, 0x9128), (0x0054, 0x1330), (0x0054, 0x1300)}:
            has_temporal = True
            break
    if not has_temporal:
        return False, 1
    # Count distinct time-axis values from PerFrameFunctionalGroups.
    per_frame = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    if not per_frame:
        return True, 1
    times: set[int] = set()
    for fg in per_frame:
        fc = getattr(fg, "FrameContentSequence", None)
        if not fc:
            continue
        div = getattr(fc[0], "DimensionIndexValues", None)
        if div is None or len(div) == 0:
            continue
        # Convention: first index is the temporal one when present.
        try:
            times.add(int(div[0]))
        except (TypeError, ValueError):
            continue
    return True, max(1, len(times))


def _pack_enhanced_multiframe(ds: pydicom.Dataset) -> PackedVolume:
    """Build a packed volume from a single Enhanced PET / multi-frame
    DICOM (Addendum B). Reads the shared geometry from
    ``SharedFunctionalGroupsSequence``, the per-frame rescale and
    position from ``PerFrameFunctionalGroupsSequence``, and orders
    the frames by their Z position so the resulting Float32 cube is
    in the same axis-2-fastest layout the classic path produces.

    4D handling (Addendum B §5): when ``DimensionIndexSequence``
    advertises a temporal axis, we keep only the first time-frame
    so the spatial volume is unambiguous. Full temporal navigation
    is a separate concern.

    Caveats:
      * Real-World Value Mapping (preferred SUV path) is honoured
        when present, otherwise we fall back to per-frame
        PixelValueTransformation.
    """
    n_frames = int(getattr(ds, "NumberOfFrames", 0) or 0)
    if n_frames < 1:
        raise ValueError("multi-frame dataset reports NumberOfFrames < 1")
    rows = int(ds.Rows)
    cols = int(ds.Columns)

    pixels = ds.pixel_array
    if pixels.ndim != 3 or pixels.shape != (n_frames, rows, cols):
        raise ValueError(
            f"unexpected multi-frame pixel shape {pixels.shape}, "
            f"expected ({n_frames}, {rows}, {cols})"
        )
    pixels = pixels.astype(np.float32)

    shared = getattr(ds, "SharedFunctionalGroupsSequence", None)
    shared_item = shared[0] if shared else None

    pixel_spacing = (1.0, 1.0)
    spacing_between = 1.0
    shared_pms = getattr(shared_item, "PixelMeasuresSequence", None) if shared_item else None
    if shared_pms:
        ps = getattr(shared_pms[0], "PixelSpacing", None)
        if ps:
            pixel_spacing = (float(ps[0]), float(ps[1]))
        sb = getattr(shared_pms[0], "SpacingBetweenSlices", None) or getattr(
            shared_pms[0], "SliceThickness", None
        )
        if sb:
            spacing_between = float(sb)

    # Real-World Value Mapping (Addendum B §4.5): when the file
    # advertises a SUV mapping directly, prefer it. Otherwise fall
    # back to the standard rescale slope/intercept.
    rwvm_slope = None
    rwvm_intercept = None
    if shared_item is not None:
        rwvm = getattr(shared_item, "RealWorldValueMappingSequence", None)
        if rwvm:
            it = rwvm[0]
            try:
                rwvm_slope = float(it.RealWorldValueSlope)
                rwvm_intercept = float(it.RealWorldValueIntercept)
            except (AttributeError, TypeError, ValueError):
                rwvm_slope = rwvm_intercept = None

    shared_slope = 1.0
    shared_intercept = 0.0
    if shared_item is not None:
        pvt = getattr(shared_item, "PixelValueTransformationSequence", None)
        if pvt:
            try:
                shared_slope = float(pvt[0].RescaleSlope)
                shared_intercept = float(pvt[0].RescaleIntercept)
            except (AttributeError, TypeError, ValueError):
                pass

    is_4d, _n_time = _detect_4d(ds)
    per_frame = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    z_positions: list[float] = []
    per_frame_slope: list[float] = []
    per_frame_intercept: list[float] = []
    keep_indices: list[int] = []
    first_time_idx: int | None = None
    for i in range(n_frames):
        slope = shared_slope
        intercept = shared_intercept
        z = float(i)
        time_idx: int | None = None
        if per_frame is not None and i < len(per_frame):
            fg = per_frame[i]
            pps = getattr(fg, "PlanePositionSequence", None)
            if pps:
                ipp = getattr(pps[0], "ImagePositionPatient", None)
                if ipp and len(ipp) >= 3:
                    z = float(ipp[2])
            pvt_local = getattr(fg, "PixelValueTransformationSequence", None)
            if pvt_local:
                try:
                    slope = float(pvt_local[0].RescaleSlope)
                    intercept = float(pvt_local[0].RescaleIntercept)
                except (AttributeError, TypeError, ValueError):
                    pass
            if is_4d:
                fc = getattr(fg, "FrameContentSequence", None)
                if fc:
                    div = getattr(fc[0], "DimensionIndexValues", None)
                    if div is not None and len(div) > 0:
                        try:
                            time_idx = int(div[0])
                        except (TypeError, ValueError):
                            time_idx = None
        # 4D: keep only frames matching the first time index seen so
        # the resulting volume is one snapshot, not a temporal mash.
        if is_4d:
            if first_time_idx is None and time_idx is not None:
                first_time_idx = time_idx
            if time_idx is None or time_idx != first_time_idx:
                continue
        z_positions.append(z)
        per_frame_slope.append(slope)
        per_frame_intercept.append(intercept)
        keep_indices.append(i)

    if not keep_indices:
        # Defensive: every frame was filtered out (would only happen
        # on malformed 4D metadata). Fall back to all frames at the
        # cost of mixing temporal frames.
        keep_indices = list(range(n_frames))
        z_positions = [float(i) for i in keep_indices]
        per_frame_slope = [shared_slope] * len(keep_indices)
        per_frame_intercept = [shared_intercept] * len(keep_indices)

    # Slice the pixels array to only the kept frames before the Z sort.
    pixels = pixels[keep_indices]
    n_kept = len(keep_indices)
    # Sort frame indices by Z position (Addendum B §9.4 — files are
    # often stored in acquisition order, not spatial).
    order = sorted(range(n_kept), key=lambda i: z_positions[i])
    sorted_pixels = pixels[order]
    # Apply per-frame rescale (or RWVM when present).
    volume = sorted_pixels.copy()
    if rwvm_slope is not None and rwvm_intercept is not None:
        volume = volume * rwvm_slope + rwvm_intercept
    else:
        for new_i, src_i in enumerate(order):
            s = per_frame_slope[src_i]
            inter = per_frame_intercept[src_i]
            if s != 1.0 or inter != 0.0:
                volume[new_i] = volume[new_i] * s + inter
    n_frames_used = n_kept

    # Recompute spacing_between from sorted positions if available.
    sorted_z = sorted(z_positions)
    if len(sorted_z) >= 2:
        diffs = [sorted_z[i + 1] - sorted_z[i] for i in range(len(sorted_z) - 1)]
        diffs = [d for d in diffs if d > 0]
        if diffs:
            # Median is more robust than first-vs-last on irregular spacing.
            diffs.sort()
            spacing_between = float(diffs[len(diffs) // 2]) or spacing_between

    bad_mask = ~np.isfinite(volume)
    if bad_mask.any():
        volume[bad_mask] = 0.0
    volume[np.abs(volume) < 1e-30] = 0.0

    vmin = float(volume.min())
    vmax = float(volume.max())
    sx = float(pixel_spacing[1])
    sy = float(pixel_spacing[0])
    sz = float(spacing_between)
    header = HEADER_STRUCT.pack(cols, rows, n_frames_used, sx, sy, sz, vmin, vmax)
    return PackedVolume(
        bytes_=header + volume.tobytes(order="C"),
        nx=cols,
        ny=rows,
        nz=n_frames_used,
        spacing=(sx, sy, sz),
        value_range=(vmin, vmax),
    )


def _pack_concatenated_multiframe(
    datasets: list[pydicom.Dataset],
) -> PackedVolume:
    """Stitch multiple Enhanced PET multi-frame files that share a
    ``ConcatenationUID`` into a single volume (Addendum B §6).

    The DICOM concatenation convention: each piece carries
    ``InConcatenationNumber`` (1-based ordinal) and
    ``ConcatenationFrameOffsetNumber`` (the frame index this piece
    starts at in the global volume). We sort by
    ``InConcatenationNumber`` and concatenate the per-file Float32
    volumes — _pack_enhanced_multiframe is reused per piece to honour
    Real-World Value Mapping, per-frame rescale, etc.

    Geometry is taken from the first piece; piece-to-piece spacing
    consistency is trusted (the modality/PACS produced these as a
    single physical acquisition).
    """
    pieces = sorted(datasets, key=lambda d: int(getattr(d, "InConcatenationNumber", 1) or 1))
    packed_pieces = [_pack_enhanced_multiframe(d) for d in pieces]
    if not packed_pieces:
        raise ValueError("concatenation: no pieces to stitch")

    # Geometry from the first piece.
    first = packed_pieces[0]
    nx = first.nx
    ny = first.ny
    sx, sy, sz = first.spacing

    # Concatenate scalars along Z axis.
    arrays: list[np.ndarray] = []
    total_nz = 0
    for p in packed_pieces:
        if p.nx != nx or p.ny != ny:
            raise ValueError(
                f"concatenation: piece geometry mismatch ({p.nx}x{p.ny}) vs first ({nx}x{ny})"
            )
        arr = np.frombuffer(p.bytes_, dtype=np.float32, offset=HEADER_STRUCT.size)
        arr = arr.reshape(p.nz, p.ny, p.nx)
        arrays.append(arr)
        total_nz += p.nz

    volume = np.concatenate(arrays, axis=0)
    vmin = float(volume.min())
    vmax = float(volume.max())
    header = HEADER_STRUCT.pack(nx, ny, total_nz, sx, sy, sz, vmin, vmax)
    return PackedVolume(
        bytes_=header + volume.tobytes(order="C"),
        nx=nx,
        ny=ny,
        nz=total_nz,
        spacing=(sx, sy, sz),
        value_range=(vmin, vmax),
    )


def apply_earl_harmonization(
    data: PackedVolume,
    fwhm_target_mm: float,
    fwhm_current_mm: float = 0.0,
) -> PackedVolume:
    """Apply an EARL-style post-reconstruction Gaussian smoothing
    (Addendum C §7). PET cross-scanner harmonisation per the EANM/EARL
    accreditation specifies a target effective PSF FWHM (typically
    7 mm for EARL1, 5 mm for EARL2). The recipe:

        FWHM_add = √(FWHM_target² - FWHM_current²)
        σ = FWHM_add / (2 · √(2 · ln 2))

    σ is then applied as a separable 3D Gaussian kernel sized in
    voxels. Volumes already at or above the target FWHM are returned
    unchanged (smoothing only adds resolution loss; subtraction is
    not invertible).

    The function operates on a packed volume blob (32-byte header +
    Float32 scalars) and emits a fresh packed blob with the same
    dimensions / spacing but smoothed scalars + recomputed
    value_range. Callers who need to chain harmonisation with other
    transforms (low-res, derivative caching) compose by re-packing.
    """
    if fwhm_target_mm <= 0:
        return data
    if fwhm_target_mm <= fwhm_current_mm + 1e-6:
        return data
    try:
        from scipy.ndimage import gaussian_filter  # type: ignore[import-not-found]
    except ImportError:
        # SciPy is heavy; if it's not available we silently skip the
        # filter rather than 500 the volume request. The viewer keeps
        # working, just without the EARL pass.
        return data
    import math

    payload = np.frombuffer(data.bytes_, dtype=np.float32, offset=HEADER_STRUCT.size)
    volume = payload.reshape(data.nz, data.ny, data.nx).copy()

    fwhm_add = math.sqrt(fwhm_target_mm * fwhm_target_mm - fwhm_current_mm * fwhm_current_mm)
    sigma_mm = fwhm_add / (2.0 * math.sqrt(2.0 * math.log(2.0)))
    # Convert σ from mm to voxels per axis. Volume is (z, y, x) so
    # the sigma sequence matches that order.
    sx, sy, sz = data.spacing
    sigma_voxels = (sigma_mm / sz, sigma_mm / sy, sigma_mm / sx)
    smoothed = gaussian_filter(volume, sigma=sigma_voxels, mode="nearest").astype(np.float32)

    vmin = float(smoothed.min())
    vmax = float(smoothed.max())
    header = HEADER_STRUCT.pack(data.nx, data.ny, data.nz, sx, sy, sz, vmin, vmax)
    return PackedVolume(
        bytes_=header + smoothed.tobytes(order="C"),
        nx=data.nx,
        ny=data.ny,
        nz=data.nz,
        spacing=data.spacing,
        value_range=(vmin, vmax),
    )


def pack_low_res(data: PackedVolume, factor: int = 2) -> PackedVolume:
    """Downsample a packed volume by ``factor`` in each axis.

    Used for progressive-load previews — at factor=2 the blob shrinks to
    ~1/8 the size, which means the browser can start rendering something
    usable while the full-res blob is still on the wire. We slice with a
    stride rather than doing a proper box filter to keep this cheap; the
    preview is only on-screen for a second or two anyway.

    The 32-byte header format is preserved exactly (nx/ny/nz updated,
    spacing scaled by ``factor``, value range recomputed from the
    downsampled samples so contrast stretches still look reasonable).
    """
    if factor < 1:
        raise ValueError("factor must be >= 1")
    if factor == 1:
        return data

    # Rebuild the float32 array from the payload (skip the header).
    payload = np.frombuffer(data.bytes_, dtype=np.float32, offset=HEADER_STRUCT.size)
    # Original layout is (nz, ny, nx) row-major with x-fastest.
    volume = payload.reshape(data.nz, data.ny, data.nx)
    small = np.ascontiguousarray(volume[::factor, ::factor, ::factor])
    new_nz, new_ny, new_nx = small.shape

    sx, sy, sz = data.spacing
    new_spacing = (sx * factor, sy * factor, sz * factor)
    vmin = float(small.min())
    vmax = float(small.max())

    header = HEADER_STRUCT.pack(new_nx, new_ny, new_nz, *new_spacing, vmin, vmax)
    return PackedVolume(
        bytes_=header + small.tobytes(order="C"),
        nx=new_nx,
        ny=new_ny,
        nz=new_nz,
        spacing=new_spacing,
        value_range=(vmin, vmax),
    )
