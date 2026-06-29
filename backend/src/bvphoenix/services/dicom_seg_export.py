"""Export a stored ``Segmentation`` mask as a conformant DICOM SEG object.

Background / why this is non-trivial
------------------------------------
Segmentation masks are persisted as a headerless raw ``uint8`` ``.bin`` in the
derivatives bucket (one byte per voxel, x-fastest, shape ``(nz, ny, nx)``, the
voxel value being the segment number per ``Segmentation.label_map``). That blob
carries **no geometry**, so it cannot be shipped to any DICOM-aware tool — only
``.bin`` raw masks today.

A DICOM SEG (Segmentation IOD, SOP class ``1.2.840.10008.5.1.4.1.1.66.4``) must
be geo-referenced: ``FrameOfReferenceUID``, a ``ReferencedSeriesSequence`` to the
source instances, and a per-frame ``ImagePositionPatient`` / ``ImageOrientation``
/ ``PixelSpacing``. The volume-build kept only a *summary* geometry
(``Derivative.geometry`` = origin + direction + FoR), not the per-slice positions
or the source SOP references. Both, however, still live on the **source DICOM
instances** in S3. So we reconstruct a fully-referenced SEG by handing the sorted
source instances to ``highdicom``, which copies the exact per-frame geometry and
builds the source references from them — the most conformant, lossless form.

The mask's z-order is the **packed-volume order** (``volumes._sort_key``: sort by
``ImagePositionPatient[2]``), so the source instances are sorted the same way and
``mask[k]`` is guaranteed to line up with ``source_datasets[k]``. A slice-count
mismatch (multi-stack / resampled series, e.g. mDIXON substacks) is refused
rather than emitting a silently-misaligned SEG.

Segment coding here is deliberately generic (category/type = "Anatomical
Structure"); the human-readable organ/lesion name goes in ``SegmentLabel``.
Mapping the finding vocabulary to RadLex/SNOMED/ICD-O codes is a separate task
(do not invent codes). AI provenance is preserved: an agent/automatic producer
is declared ``AUTOMATIC`` with an ``AlgorithmIdentificationSequence`` (family =
DCM "Artificial Intelligence"); a human producer is ``MANUAL``.
"""

from __future__ import annotations

import io
import logging

import highdicom as hd
import numpy as np
import pydicom
from highdicom.sr.coding import CodedConcept
from pydicom.sr.codedict import codes
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Instance, Segmentation
from bvphoenix.services.volumes import _sort_key

logger = logging.getLogger(__name__)

SEGMENTATION_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.66.4"

# Generic, conformant Segmented Property coding. The specific organ/lesion name
# is carried in SegmentLabel; precise RadLex/SNOMED/ICD-O coding is task
# 89536d5b ("map Finding vocabulary to a code-system"), which must use a real
# terminology source — never a guessed code.
_GENERIC_CATEGORY = CodedConcept("123037004", "SCT", "Anatomical Structure")
_GENERIC_TYPE = CodedConcept("123037004", "SCT", "Anatomical Structure")

# Type-2 (required, may be empty) patient/study attributes highdicom copies off
# the first source image *by direct attribute access*. Real DICOM always carries
# them, but a non-conformant or partially de-identified instance may not — set
# them to empty so the export degrades safely instead of raising AttributeError.
_REQUIRED_SOURCE_ATTRS = (
    "PatientID",
    "PatientName",
    "PatientBirthDate",
    "PatientSex",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "StudyDate",
    "StudyTime",
    "AccessionNumber",
    "StudyID",
    "ReferringPhysicianName",
)


class SegExportError(Exception):
    """Raised when a Segmentation cannot be emitted as a conformant DICOM SEG
    (e.g. the mask grid does not line up 1:1 with the source slices)."""


def _ensure_source_attrs(ds: pydicom.Dataset) -> None:
    """Fill any missing type-2 attribute with an empty value so highdicom's
    attribute-copy never trips on a slightly non-conformant source instance."""
    for attr in _REQUIRED_SOURCE_ATTRS:
        if not hasattr(ds, attr):
            setattr(ds, attr, "")


def _algorithm(
    producer: str | None, model_id: str | None, author_kind: str | None, version: str | None
) -> tuple[hd.seg.SegmentAlgorithmTypeValues, hd.AlgorithmIdentificationSequence | None]:
    """Map the bvphoenix producer/author onto the SEG algorithm provenance.

    A human-drawn mask is ``MANUAL``; anything produced by a model/agent is
    ``AUTOMATIC`` with an identification sequence so the AI provenance is visible
    in the exported object (never silently presented as human work)."""
    if (producer or "").lower() == "manual" or (author_kind or "").lower() == "human":
        return hd.seg.SegmentAlgorithmTypeValues.MANUAL, None
    algo_id = hd.AlgorithmIdentificationSequence(
        name=model_id or producer or "bvphoenix",
        family=codes.cid7162.ArtificialIntelligence,
        version=version or "unknown",
    )
    return hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC, algo_id


def _segments(
    mask: np.ndarray, label_map: dict | None, default_label: str
) -> list[tuple[int, str]]:
    """Resolve ``(segment_number, label)`` for every positive value present in
    the mask, naming them from ``label_map`` where possible. Driving this off
    the actual mask values (not just ``label_map``) guarantees highdicom never
    sees a pixel value without a matching segment description."""
    values = [int(v) for v in np.unique(mask) if int(v) != 0]
    if not values:
        raise SegExportError("segmentation mask is empty (no foreground voxels)")
    lm = {str(k): str(v) for k, v in (label_map or {}).items()}
    single = len(values) == 1
    out: list[tuple[int, str]] = []
    for v in values:
        label = lm.get(str(v)) or (default_label if single else f"segment {v}")
        out.append((v, label))
    return out


def build_segmentation_seg(
    mask: np.ndarray,
    source_datasets: list[pydicom.Dataset],
    *,
    label_map: dict | None,
    default_label: str,
    producer: str | None = None,
    producer_version: str | None = None,
    model_id: str | None = None,
    author_kind: str | None = None,
    series_number: int = 4200,
    instance_number: int = 1,
) -> bytes:
    """Build a conformant DICOM SEG (Part 10 bytes) from an in-memory mask and
    the **sorted** source instances it was computed over.

    ``mask`` is ``(nz, ny, nx)`` uint8 label-map (0 = background, positive =
    segment number). ``source_datasets[k]`` MUST be the source slice for
    ``mask[k]`` (caller sorts both by ``volumes._sort_key``). highdicom derives
    the per-frame geometry + source references from ``source_datasets``.
    """
    if mask.ndim != 3:
        raise SegExportError(f"mask must be 3-D (nz, ny, nx), got shape {mask.shape}")
    nz = mask.shape[0]
    if nz != len(source_datasets):
        raise SegExportError(
            f"slice-count mismatch: mask has {nz} slices but the series has "
            f"{len(source_datasets)} instances — multi-stack / resampled series "
            "are not yet supported for DICOM SEG export"
        )
    if nz == 0:
        raise SegExportError("series has no instances")

    rows = int(getattr(source_datasets[0], "Rows", 0))
    cols = int(getattr(source_datasets[0], "Columns", 0))
    if (rows, cols) != (mask.shape[1], mask.shape[2]):
        raise SegExportError(
            f"mask plane {mask.shape[1]}x{mask.shape[2]} does not match source image {rows}x{cols}"
        )

    for ds in source_datasets:
        _ensure_source_attrs(ds)

    algorithm_type, algorithm_id = _algorithm(producer, model_id, author_kind, producer_version)
    segment_descriptions = [
        hd.seg.SegmentDescription(
            segment_number=num,
            segment_label=label,
            segmented_property_category=_GENERIC_CATEGORY,
            segmented_property_type=_GENERIC_TYPE,
            algorithm_type=algorithm_type,
            algorithm_identification=algorithm_id,
        )
        for num, label in _segments(mask, label_map, default_label)
    ]

    seg = hd.seg.Segmentation(
        source_images=source_datasets,
        pixel_array=mask,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=segment_descriptions,
        series_instance_uid=generate_uid(),
        series_number=series_number,
        sop_instance_uid=generate_uid(),
        instance_number=instance_number,
        manufacturer="bvphoenix",
        manufacturer_model_name=model_id or producer or "bvphoenix",
        software_versions="bvphoenix",
        device_serial_number="bvphoenix",
        content_label="BVPHOENIX",
        content_description=f"bvphoenix segmentation: {default_label}"[:64],
    )
    buf = io.BytesIO()
    seg.save_as(buf)
    return buf.getvalue()


def _build_from_storage(
    seg_ref: tuple[str, str],
    instance_refs: list[tuple[str, str]],
    meta: dict,
) -> bytes:
    """Synchronous core: fetch the mask + source instances from S3, sort, and
    build the SEG. Runs in a worker thread (S3 I/O + highdicom CPU) so the async
    request never blocks. ``instance_refs`` is every source ``(bucket, key)``."""
    from bvphoenix.storage import get_s3_storage

    storage = get_s3_storage()
    mask_bytes = storage.get_object_bytes(bucket=seg_ref[0], key=seg_ref[1])
    datasets: list[pydicom.Dataset] = []
    for bucket, key in instance_refs:
        blob = storage.get_object_bytes(bucket=bucket, key=key)
        # stop_before_pixels: geometry + SOP refs + patient/study attrs are all
        # in the header; the source pixel data is not needed to reference it.
        datasets.append(pydicom.dcmread(io.BytesIO(blob), stop_before_pixels=True, force=True))
    datasets.sort(key=_sort_key)

    rows = int(getattr(datasets[0], "Rows", 0))
    cols = int(getattr(datasets[0], "Columns", 0))
    nz = len(datasets)
    if rows <= 0 or cols <= 0:
        raise SegExportError("source instances carry no Rows/Columns")
    if len(mask_bytes) != nz * rows * cols:
        raise SegExportError(
            f"mask byte length {len(mask_bytes)} != nz*rows*cols "
            f"({nz}*{rows}*{cols}={nz * rows * cols}) — the mask grid does not "
            "match the source slices (multi-stack / resampled series)"
        )
    mask = np.frombuffer(mask_bytes, dtype=np.uint8).reshape(nz, rows, cols).copy()
    return build_segmentation_seg(mask, datasets, **meta)


async def export_segmentation_seg(db: AsyncSession, seg: Segmentation) -> bytes:
    """Load a stored Segmentation's mask + its source instances and emit a
    conformant, geo-referenced DICOM SEG. Storage-isolated: the caller returns
    the bytes inline (no bucket name / presigned URL leaves the platform).

    The DB lookup is async; the S3 reads + highdicom build run in a thread so a
    many-slice series does not block the event loop."""
    import asyncio

    instance_refs = (
        await db.execute(
            select(Instance.s3_bucket, Instance.s3_key).where(Instance.series_id == seg.series_id)
        )
    ).all()
    if not instance_refs:
        raise SegExportError("series has no source instances")

    meta = {
        "label_map": seg.label_map,
        "default_label": seg.label or "segmentation",
        "producer": seg.producer,
        "producer_version": getattr(seg, "producer_version", None),
        "model_id": getattr(seg, "model_id", None),
        "author_kind": getattr(seg, "author_kind", None),
    }
    return await asyncio.to_thread(
        _build_from_storage,
        (seg.s3_bucket, seg.s3_key),
        [(b, k) for b, k in instance_refs],
        meta,
    )


__all__ = [
    "SEGMENTATION_SOP_CLASS_UID",
    "SegExportError",
    "build_segmentation_seg",
    "export_segmentation_seg",
]
