"""MPR (Multi-Planar Reformat) over a DICOM series (Sprint 5b).

The thumbnail / slice endpoint already serves axial slices (the
DICOM-acquired plane). MPR adds the missing two: ``coronal`` and
``sagittal``, reconstructed from the volume by SimpleITK reslice.

Strategy:

1. Load every instance of the series, sorted by ``ImagePositionPatient``
   along the slice normal (``ImageOrientationPatient`` cross product).
2. Stack into a 3D ``SimpleITK.Image`` with the right spacing
   ``(col_mm, row_mm, slice_mm)``.
3. For ``coronal`` / ``sagittal`` extract the 2D slice at ``idx`` along
   the requested axis. ``idx`` is bounded by the volume extent on
   that axis.
4. Apply DICOM windowing (Window Center / Width with optional
   ``wc_delta`` / ``ww_delta``) and rescale to uint8.
5. Encode JPEG.

Edge cases:

* Single-instance series → no MPR; raises :class:`MPRUnavailableError`.
* Mixed-SOP series (CT + SR + PR on the same series UID) → only
  pixel-bearing instances participate; the others are filtered out
  before stacking.
* Inconsistent in-plane size → :class:`MPRUnavailableError` with the
  specific row / column mismatch in the message.

Output is the JPEG bytes; the caller (api/studies.py) layers HTTP +
cache on top.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pydicom
import SimpleITK as sitk  # noqa: N813 — community-standard alias
from PIL import Image

from bvphoenix.services.thumbnails import is_image_sop_class

Plane = Literal["axial", "coronal", "sagittal"]


class MPRUnavailableError(ValueError):
    """Raised when the volume cannot be stacked or the requested
    plane / index is out of bounds."""


@dataclass(slots=True)
class VolumeMeta:
    nx: int  # columns
    ny: int  # rows
    nz: int  # slices
    spacing_mm: tuple[float, float, float]  # (column, row, slice)
    window_center: float
    window_width: float


def _slice_normal(orientation: list[float]) -> np.ndarray:
    """Compute the slice normal from ``ImageOrientationPatient``.

    DICOM PS3.3: the six-value tag is ``[Rx, Ry, Rz, Cx, Cy, Cz]``
    — row direction cosines followed by column direction cosines.
    The slice normal is the cross product R x C.
    """
    r = np.array(orientation[:3], dtype=np.float64)
    c = np.array(orientation[3:6], dtype=np.float64)
    return np.cross(r, c)


def _slice_position(ds: pydicom.Dataset, normal: np.ndarray) -> float:
    """Project ``ImagePositionPatient`` onto the slice normal.

    Sorting instances by this scalar yields the through-plane order
    even for oblique acquisitions where ``InstanceNumber`` is
    unreliable.
    """
    pos = getattr(ds, "ImagePositionPatient", None)
    if pos is None or len(pos) < 3:
        return 0.0
    return float(np.dot(np.array(pos[:3], dtype=np.float64), normal))


def _stack_volume(dcm_bytes_list: list[bytes]) -> tuple[np.ndarray, VolumeMeta]:
    """Stack instances into a contiguous ``(nz, ny, nx)`` numpy array.

    Returns the rescaled (slope/intercept applied) array as float32
    plus the volume metadata.
    """
    if not dcm_bytes_list:
        raise MPRUnavailableError("series has no pixel-bearing instances")

    datasets: list[pydicom.Dataset] = []
    for raw in dcm_bytes_list:
        ds = pydicom.dcmread(io.BytesIO(raw))
        if not is_image_sop_class(getattr(ds, "SOPClassUID", None)):
            continue
        if "PixelData" not in ds:
            continue
        datasets.append(ds)
    if len(datasets) < 2:
        raise MPRUnavailableError("MPR needs at least 2 image instances in the series")

    # Use the first dataset's orientation for sorting.
    orientation = list(getattr(datasets[0], "ImageOrientationPatient", [1, 0, 0, 0, 1, 0]))
    if len(orientation) < 6:
        orientation = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    normal = _slice_normal(orientation)
    if not np.any(normal):
        normal = np.array([0.0, 0.0, 1.0])
    datasets.sort(key=lambda d: _slice_position(d, normal))

    first = datasets[0]
    rows = int(first.Rows)
    cols = int(first.Columns)
    pixel_spacing = list(getattr(first, "PixelSpacing", [1.0, 1.0]))
    row_mm = float(pixel_spacing[0])
    col_mm = float(pixel_spacing[1])
    slice_mm = float(
        getattr(first, "SpacingBetweenSlices", None)
        or getattr(first, "SliceThickness", None)
        or 1.0
    )

    arr = np.empty((len(datasets), rows, cols), dtype=np.float32)
    for i, ds in enumerate(datasets):
        if int(ds.Rows) != rows or int(ds.Columns) != cols:
            raise MPRUnavailableError(
                f"instance {i} size {(int(ds.Rows), int(ds.Columns))} != first {(rows, cols)}"
            )
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        slc = ds.pixel_array.astype(np.float32) * slope + intercept
        arr[i] = slc

    wc = float(getattr(first, "WindowCenter", 0.0) or 0.0)
    ww = float(getattr(first, "WindowWidth", 0.0) or 0.0)
    if isinstance(wc, (list, tuple)):
        wc = float(wc[0])
    if isinstance(ww, (list, tuple)):
        ww = float(ww[0])
    if ww <= 0:
        # Fall back to data range when the DICOM doesn't carry a window.
        ww = float(arr.max() - arr.min())
        wc = float((arr.max() + arr.min()) / 2.0)

    meta = VolumeMeta(
        nx=cols,
        ny=rows,
        nz=len(datasets),
        spacing_mm=(col_mm, row_mm, slice_mm),
        window_center=wc,
        window_width=ww,
    )
    return arr, meta


def _to_sitk(arr: np.ndarray, meta: VolumeMeta) -> sitk.Image:
    """Wrap a ``(nz, ny, nx)`` numpy array into a ``sitk.Image`` with
    the right spacing tuple ``(x, y, z)``.
    """
    img = sitk.GetImageFromArray(arr)
    img.SetSpacing((meta.spacing_mm[0], meta.spacing_mm[1], meta.spacing_mm[2]))
    return img


def _window_to_uint8(slc: np.ndarray, *, wc: float, ww: float) -> np.ndarray:
    if ww <= 0:
        ww = max(1e-6, float(slc.max() - slc.min()) or 1.0)
    low = wc - ww / 2.0
    high = wc + ww / 2.0
    if high <= low:
        high = low + 1.0
    out = (slc - low) / (high - low) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


def _resize(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img
    scale = max_side / longest
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    pil = Image.fromarray(img)
    pil = pil.resize((new_w, new_h), Image.Resampling.LANCZOS)
    return np.array(pil)


def reslice_to_jpeg(
    dcm_bytes_list: list[bytes],
    *,
    plane: Plane,
    idx: int,
    wc_delta: float = 0.0,
    ww_delta: float = 0.0,
    max_side: int = 512,
    quality: int = 80,
) -> tuple[bytes, dict]:
    """Render one slice at ``idx`` along ``plane`` as JPEG.

    Axial path uses the raw stacked array (fastest, no interpolation).
    Coronal / sagittal use SimpleITK reslicing for completeness — the
    spacing is already correct so nearest-neighbour reformat is a
    plain numpy transpose; we keep the SimpleITK round-trip for
    consistency with future oblique-MPR extensions.
    """
    arr, meta = _stack_volume(dcm_bytes_list)
    nx, ny, nz = meta.nx, meta.ny, meta.nz

    if plane == "axial":
        if idx < 0 or idx >= nz:
            raise MPRUnavailableError(f"axial idx {idx} out of range [0, {nz})")
        slc = arr[idx, :, :]
    elif plane == "coronal":
        if idx < 0 or idx >= ny:
            raise MPRUnavailableError(f"coronal idx {idx} out of range [0, {ny})")
        # arr shape (nz, ny, nx) -> coronal slice at row idx is
        # (nz, nx). SimpleITK round-trip kept for symmetry.
        sitk_img = _to_sitk(arr, meta)
        # Extract a 2D slice with image axis order (x, y, z).
        # SimpleITK indexing is (x, y, z); use Slice() with ranges.
        slc_3d = sitk_img[:, idx : idx + 1, :]
        slc_arr = sitk.GetArrayFromImage(slc_3d)  # (nz, 1, nx)
        slc = slc_arr.squeeze(axis=1)  # (nz, nx)
    else:  # sagittal
        if idx < 0 or idx >= nx:
            raise MPRUnavailableError(f"sagittal idx {idx} out of range [0, {nx})")
        sitk_img = _to_sitk(arr, meta)
        slc_3d = sitk_img[idx : idx + 1, :, :]
        slc_arr = sitk.GetArrayFromImage(slc_3d)  # (nz, ny, 1)
        slc = slc_arr.squeeze(axis=2)  # (nz, ny)

    wc = meta.window_center + wc_delta
    ww = max(1.0, meta.window_width + ww_delta)
    img8 = _window_to_uint8(slc, wc=wc, ww=ww)
    img8 = _resize(img8, max_side)

    pil = Image.fromarray(img8, mode="L")
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    info = {
        "plane": plane,
        "idx": idx,
        "nx": nx,
        "ny": ny,
        "nz": nz,
        "spacing_mm": list(meta.spacing_mm),
        "window_center": wc,
        "window_width": ww,
    }
    return buf.getvalue(), info


__all__ = [
    "MPRUnavailableError",
    "Plane",
    "VolumeMeta",
    "reslice_to_jpeg",
]
