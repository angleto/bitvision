"""Format serializers for the training-cohort export (annotation overhaul P5).

The cohort selection, consent + tier + k-anonymity gating, synthetic
re-keying, burned-in-PHI gate, streaming, and dataset materialization all
live elsewhere (``services.training_cohort`` + the worker). This module is
the *pure* layer that turns one series' in-memory pixels + masks into the
on-disk artifacts a given training framework ingests:

- ``nnunet`` — nnU-Net v2 ``imagesTr/`` + ``labelsTr/`` NIfTI pairs +
  ``dataset.json`` (``channel_names`` / ``labels`` / ``numTraining``).
- ``monai`` — the same NIfTI pairs + an MSD-style ``dataset.json`` datalist
  (explicit ``training`` image/label pairs).
- ``coco`` — per-slice PNG of annotated slices + ``annotations/instances.json``
  (COCO detection/segmentation: bbox + uncompressed RLE per label).

Everything here is DB-free and S3-free so the geometry, label remapping,
and manifest shapes are unit-testable with synthetic ``pydicom`` datasets
(mirrors ``test_dicom_seg_export``).

Geometry model (deliberately simple + robust). The image volume and the
label volume are both built from the SAME source instances sorted by the
single canonical key (:func:`bvphoenix.services.volumes._sort_key`, the key
the packed volume + the DICOM SEG export already use), so they are aligned
voxel-for-voxel by construction — no world<->image projection is performed
here, which is exactly where silent geometry bugs hide. The NIfTI affine is
a spacing-only diagonal (RAS-equivalent), matching the existing
``segment_auto`` writer that feeds TotalSegmentator: nnU-Net/MONAI resample
by spacing and reorient internally, and image/label share the identical
affine, so alignment is preserved. Full direction-cosine orientation is a
deliberate non-goal here (it buys nothing for these consumers and is a
known footgun).
"""

from __future__ import annotations

import gzip
import io
from typing import Any

import numpy as np
import pydicom

from bvphoenix.services.volumes import _sort_key

# The export formats the bundle worker understands. ``bvphoenix`` is the
# original raw DICOM + .bin + labels.json layout (handled directly by the
# worker); the rest are produced through this module.
COHORT_FORMATS: tuple[str, ...] = ("bvphoenix", "nnunet", "monai", "coco")
NIFTI_FORMATS: tuple[str, ...] = ("nnunet", "monai")

# nnU-Net v2 conventions.
NNUNET_FILE_ENDING = ".nii.gz"
_CHANNEL_SUFFIX = "_0000"  # single-channel input


class CohortFormatError(Exception):
    """A series cannot be serialized into the requested format.

    Raised for recoverable, per-series problems (inconsistent slice sizes,
    a mask whose voxel count does not match the image volume). The worker
    catches it, records the series as skipped, and moves on — one bad
    series never fails the whole export."""


def _first_float(value: Any) -> float | None:
    """Coerce a DICOM attribute that may be scalar or MultiValue to float."""
    if value is None:
        return None
    # pydicom MultiValue (a list-like) and real lists both decay to the first
    # element; a str stays whole (so it can parse as a number).
    if not isinstance(value, str) and hasattr(value, "__iter__"):
        try:
            value = next(iter(value))
        except StopIteration:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slice_spacing_mm(datasets: list[pydicom.Dataset]) -> float:
    """Slice spacing from the sorted ImagePositionPatient z-deltas, falling
    back to SpacingBetweenSlices / SliceThickness, then 1.0mm.

    The measured inter-slice distance is more reliable than the header
    fields (which real-world DICOM frequently omits or lies about)."""
    zs = [
        float(ds.ImagePositionPatient[2])
        for ds in datasets
        if getattr(ds, "ImagePositionPatient", None) is not None
    ]
    if len(zs) >= 2:
        deltas = np.abs(np.diff(zs))
        deltas = deltas[deltas > 1e-4]
        if deltas.size:
            return float(np.median(deltas))
    first = datasets[0]
    return (
        _first_float(getattr(first, "SpacingBetweenSlices", None))
        or _first_float(getattr(first, "SliceThickness", None))
        or 1.0
    )


def build_image_volume(
    datasets: list[pydicom.Dataset],
) -> tuple[np.ndarray, tuple[float, float, float], list[pydicom.Dataset]]:
    """Sort + stack a series into a ``(nz, ny, nx)`` float32 volume in
    modality units (RescaleSlope/Intercept applied).

    Returns ``(array, spacing_xyz, sorted_datasets)`` where ``spacing_xyz``
    is ``(col_mm, row_mm, slice_mm)`` (X, Y, Z) for the NIfTI affine. The
    sort key is the canonical one shared with the packed volume + SEG
    export, so a mask built from the same series aligns voxel-for-voxel.

    Raises :class:`CohortFormatError` on an empty series or inconsistent
    in-plane dimensions (mixed-size "series" are separate acquisitions, not
    a coherent volume)."""
    if not datasets:
        raise CohortFormatError("empty series")
    ordered = sorted(datasets, key=_sort_key)
    first = ordered[0]
    rows = int(first.Rows)
    cols = int(first.Columns)
    arr = np.empty((len(ordered), rows, cols), dtype=np.float32)
    for i, ds in enumerate(ordered):
        if int(ds.Rows) != rows or int(ds.Columns) != cols:
            raise CohortFormatError(
                f"inconsistent slice size {(int(ds.Rows), int(ds.Columns))} != {(rows, cols)}"
            )
        slope = _first_float(getattr(ds, "RescaleSlope", None))
        intercept = _first_float(getattr(ds, "RescaleIntercept", None))
        slc = ds.pixel_array.astype(np.float32)
        if slope is not None and slope != 1.0:
            slc = slc * slope
        if intercept:
            slc = slc + intercept
        arr[i] = slc
    pixel_spacing = getattr(first, "PixelSpacing", None)
    # DICOM PixelSpacing is [row spacing (Y), column spacing (X)].
    row_mm = _first_float(pixel_spacing[0]) if pixel_spacing else None
    col_mm = _first_float(pixel_spacing[1]) if pixel_spacing and len(pixel_spacing) > 1 else None
    slice_mm = _slice_spacing_mm(ordered)
    spacing_xyz = (col_mm or 1.0, row_mm or 1.0, slice_mm)
    return arr, spacing_xyz, ordered


def build_label_index(series_masks: list[list[dict[str, Any]]]) -> dict[str, int]:
    """Assign each distinct segmentation class a stable global integer
    (1..N; 0 = background), sorted by name for determinism.

    ``series_masks`` is one ``[{label, label_map}, ...]`` list per series.
    The class name is the ``label_map`` tissue name when present, else the
    segmentation's free-form ``label``. The dataset-wide index is what makes
    the per-case label volumes mutually consistent (nnU-Net/MONAI require a
    single global ``labels`` dict)."""
    names: set[str] = set()
    for masks in series_masks:
        for m in masks:
            label_map = m.get("label_map") or {}
            if label_map:
                names.update(str(v) for v in label_map.values() if v)
            elif m.get("label"):
                names.add(str(m["label"]))
    return {name: i for i, name in enumerate(sorted(names), start=1)}


def build_label_volume(
    masks: list[dict[str, Any]],
    shape_zyx: tuple[int, int, int],
    label_index: dict[str, int],
) -> np.ndarray | None:
    """Paint the series' segmentation masks into one ``(nz, ny, nx)`` uint8
    label volume using the dataset-wide ``label_index``.

    Each mask is the headerless raw uint8 ``.bin`` (the segmentation blob
    contract: x-fastest ``(nz, ny, nx)`` aligned to the same sorted series).
    Local mask values are mapped through ``label_map`` (``{"1": "liver"}``)
    to the global integer; a binary mask with no map uses the segmentation's
    ``label``. Masks are painted in a deterministic order (by global id) so
    overlaps resolve the same way every run. Returns ``None`` when no mask
    contributes a positive voxel.

    Raises :class:`CohortFormatError` if a mask's byte count does not match
    the image volume — a misaligned label must never ship as ground truth,
    so the series is dropped rather than silently mis-rasterized."""
    nz, ny, nx = shape_zyx
    expected = nz * ny * nx
    # uint8 covers the realistic case; widen if a cohort ever has >255 classes
    # so a global id never silently wraps in published ground truth.
    dtype = np.uint8 if max(label_index.values(), default=0) <= 255 else np.uint16
    out = np.zeros(shape_zyx, dtype=dtype)
    painted = False
    # Sort the (mask, local_value, global_id) paint operations by global id
    # so overlapping segments resolve deterministically (higher id wins).
    ops: list[tuple[int, np.ndarray]] = []
    for m in masks:
        raw = m.get("raw")
        if raw is None:
            continue
        buf = np.frombuffer(raw, dtype=np.uint8)
        if buf.size != expected:
            raise CohortFormatError(
                f"mask '{m.get('label')}' voxel count {buf.size} != volume {expected}"
            )
        vol = buf.reshape(shape_zyx)
        label_map = m.get("label_map") or {}
        local_values = [v for v in np.unique(vol) if v != 0]
        for lv in local_values:
            name = (
                str(label_map.get(str(int(lv)))) if label_map.get(str(int(lv))) else m.get("label")
            )
            gid = label_index.get(str(name)) if name else None
            if gid is None:
                continue
            ops.append((gid, vol == lv))
    for gid, sel in sorted(ops, key=lambda t: t[0]):
        out[sel] = gid
        painted = True
    return out if painted else None


def write_nifti(arr_zyx: np.ndarray, spacing_xyz: tuple[float, float, float]) -> bytes:
    """Serialize a ``(nz, ny, nx)`` array to gzipped NIfTI bytes (.nii.gz).

    Spacing-only diagonal affine — see the module docstring. Mirrors
    ``bvworkers.tasks.segment_auto._write_nifti`` (the writer that feeds
    TotalSegmentator), so images and masks land in the same convention the
    rest of the platform already produces."""
    import nibabel as nib

    sx, sy, sz = spacing_xyz
    affine = np.diag([sx, sy, sz, 1.0]).astype(np.float64)
    # nibabel image space is (X, Y, Z); we hold (Z, Y, X).
    data_xyz = np.transpose(arr_zyx, (2, 1, 0))
    img = nib.Nifti1Image(np.ascontiguousarray(data_xyz), affine)
    return gzip.compress(img.to_bytes())


def nnunet_image_name(case_id: str) -> str:
    """nnU-Net training image path for a case (single channel ``_0000``)."""
    return f"imagesTr/{case_id}{_CHANNEL_SUFFIX}{NNUNET_FILE_ENDING}"


def nnunet_label_name(case_id: str) -> str:
    """nnU-Net training label path for a case."""
    return f"labelsTr/{case_id}{NNUNET_FILE_ENDING}"


def nnunet_dataset_json(
    *, modality: str, label_index: dict[str, int], num_training: int
) -> dict[str, Any]:
    """nnU-Net v2 ``dataset.json``. ``labels`` is name->int with background
    pinned at 0; ``channel_names`` carries the single input modality."""
    labels = {"background": 0}
    labels.update(dict(sorted(label_index.items(), key=lambda kv: kv[1])))
    return {
        "channel_names": {"0": modality or "image"},
        "labels": labels,
        "numTraining": num_training,
        "file_ending": NNUNET_FILE_ENDING,
    }


def monai_dataset_json(
    *,
    modality: str,
    label_index: dict[str, int],
    cases: list[tuple[str, str]],
    name: str = "bvphoenix-cohort",
) -> dict[str, Any]:
    """MSD/MONAI-style ``dataset.json`` datalist (explicit image/label pairs
    under ``training``). ``labels`` + ``modality`` are int->name (MSD
    convention, the inverse of nnU-Net)."""
    labels = {"0": "background"}
    labels.update({str(gid): nm for nm, gid in sorted(label_index.items(), key=lambda kv: kv[1])})
    return {
        "name": name,
        "description": "bitvision de-identified training cohort",
        "tensorImageSize": "3D",
        "modality": {"0": modality or "image"},
        "labels": labels,
        "numTraining": len(cases),
        "numTest": 0,
        "training": [{"image": f"./{img}", "label": f"./{lbl}"} for img, lbl in cases],
        "test": [],
    }


def default_window(ds: pydicom.Dataset, arr: np.ndarray) -> tuple[float, float]:
    """Pick a window (center, width) for 8-bit COCO rendering: the DICOM's
    own WindowCenter/Width when present, else the volume's data range."""
    wc = _first_float(getattr(ds, "WindowCenter", None))
    ww = _first_float(getattr(ds, "WindowWidth", None))
    if wc is not None and ww is not None and ww > 0:
        return wc, ww
    lo, hi = float(arr.min()), float(arr.max())
    return (lo + hi) / 2.0, max(1.0, hi - lo)


def window_to_uint8(slc: np.ndarray, *, wc: float, ww: float) -> np.ndarray:
    """Map a 2-D slice to uint8 through a linear window (mirrors
    ``services.mpr._window_to_uint8``)."""
    if ww <= 0:
        ww = max(1.0, float(slc.max() - slc.min()) or 1.0)
    low = wc - ww / 2.0
    high = wc + ww / 2.0
    if high <= low:
        high = low + 1.0
    out = (slc - low) / (high - low) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def encode_png(slc_uint8: np.ndarray) -> bytes:
    """Encode a 2-D uint8 array as grayscale PNG bytes."""
    from PIL import Image

    out = io.BytesIO()
    Image.fromarray(slc_uint8, mode="L").save(out, format="PNG", optimize=True)
    return out.getvalue()


def rle_encode_2d(mask: np.ndarray) -> dict[str, Any]:
    """Uncompressed COCO RLE for a 2-D binary mask.

    COCO RLE counts alternating runs of 0s then 1s in COLUMN-MAJOR
    (Fortran) order, starting with a 0-run. pycocotools decodes this list
    form directly."""
    h, w = mask.shape
    flat = np.asarray(mask, dtype=bool).ravel(order="F")
    if flat.size == 0:
        return {"size": [int(h), int(w)], "counts": [0]}
    change = np.flatnonzero(np.diff(flat.view(np.int8))) + 1
    bounds = np.concatenate(([0], change, [flat.size]))
    run_lengths = np.diff(bounds)
    counts: list[int] = []
    if flat[0]:  # mask starts on a foreground pixel — emit a leading 0-run
        counts.append(0)
    counts.extend(int(x) for x in run_lengths)
    return {"size": [int(h), int(w)], "counts": counts}


class CocoBuilder:
    """Accumulate COCO detection/segmentation annotations across slices.

    One image per emitted slice; one annotation per (global label present on
    that slice) with an axis-aligned bbox (xywh), uncompressed RLE, and
    pixel area. Categories come from the dataset-wide ``label_index``."""

    def __init__(self, label_index: dict[str, int]) -> None:
        self._label_index = label_index
        self._images: list[dict[str, Any]] = []
        self._annotations: list[dict[str, Any]] = []
        self._next_image_id = 1
        self._next_ann_id = 1

    def add_slice(self, file_name: str, label_slice: np.ndarray) -> bool:
        """Add the slice + an annotation per present label. Returns True iff
        at least one labeled object was found (callers skip empty slices)."""
        present = [int(v) for v in np.unique(label_slice) if v != 0]
        if not present:
            return False
        h, w = label_slice.shape
        image_id = self._next_image_id
        self._next_image_id += 1
        self._images.append(
            {"id": image_id, "file_name": file_name, "height": int(h), "width": int(w)}
        )
        for gid in present:
            sel = label_slice == gid
            ys, xs = np.nonzero(sel)
            x0, y0 = int(xs.min()), int(ys.min())
            bw, bh = int(xs.max() - x0 + 1), int(ys.max() - y0 + 1)
            self._annotations.append(
                {
                    "id": self._next_ann_id,
                    "image_id": image_id,
                    "category_id": gid,
                    "bbox": [x0, y0, bw, bh],
                    "area": int(sel.sum()),
                    "iscrowd": 0,
                    "segmentation": rle_encode_2d(sel),
                }
            )
            self._next_ann_id += 1
        return True

    def has_images(self) -> bool:
        return bool(self._images)

    def build(self) -> dict[str, Any]:
        categories = [
            {"id": gid, "name": name}
            for name, gid in sorted(self._label_index.items(), key=lambda kv: kv[1])
        ]
        return {
            "info": {
                "description": "bitvision de-identified training cohort (COCO)",
                "note": "grayscale PNG windowed per slice; segmentation = uncompressed RLE",
            },
            "images": self._images,
            "annotations": self._annotations,
            "categories": categories,
        }


__all__ = [
    "COHORT_FORMATS",
    "NIFTI_FORMATS",
    "CocoBuilder",
    "CohortFormatError",
    "build_image_volume",
    "build_label_index",
    "build_label_volume",
    "default_window",
    "encode_png",
    "monai_dataset_json",
    "nnunet_dataset_json",
    "rle_encode_2d",
    "window_to_uint8",
    "write_nifti",
]
