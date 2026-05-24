"""Convert external NIfTI / NRRD segmentation files into the internal
binary-mask format consumed by the viewer.

Internal mask format (matches what ``services.volumes`` produces for the
source volume, minus the header): a raw ``uint8`` buffer of
``nx * ny * nz`` bytes, x-fastest, with ``1`` = foreground, ``0`` =
background. Storing the mask without a header keeps the contract
symmetric with the float32 volume download — the viewer already knows
``(nx, ny, nz)`` from the sibling ``volume.raw`` request.

NIfTI import uses ``nibabel``; NRRD uses a hand-rolled parser (header
is 7-bit ASCII, payload is raw / optionally gzipped) so we don't pull
in SimpleITK just for this. Both paths converge on a ``numpy.uint8``
array shaped ``(nz, ny, nx)``, which is then flattened x-fastest.
"""

from __future__ import annotations

import gzip
import io
from dataclasses import dataclass

import numpy as np


class SegmentationImportError(ValueError):
    """Raised when the uploaded file cannot be interpreted or does not
    line up with the target volume. Callers translate to HTTP 400/422."""


@dataclass(frozen=True, slots=True)
class ImportedMask:
    data: bytes  # raw uint8, x-fastest
    dimensions: tuple[int, int, int]  # (nx, ny, nz)
    nonzero_voxels: int


def _resample_nearest(volume: np.ndarray, target_shape: tuple[int, int, int]) -> np.ndarray:
    """Nearest-neighbour resample ``volume`` (shape ``(nz, ny, nx)``) to
    ``target_shape`` in the same axis order. Used when the uploaded
    segmentation has different dims than the source volume (common when
    Slicer downsamples before segmenting)."""
    src_shape = volume.shape
    if src_shape == target_shape:
        return volume
    out = np.empty(target_shape, dtype=volume.dtype)
    z_idx = (np.arange(target_shape[0]) * src_shape[0] / target_shape[0]).astype(np.int64)
    y_idx = (np.arange(target_shape[1]) * src_shape[1] / target_shape[1]).astype(np.int64)
    x_idx = (np.arange(target_shape[2]) * src_shape[2] / target_shape[2]).astype(np.int64)
    z_idx = np.clip(z_idx, 0, src_shape[0] - 1)
    y_idx = np.clip(y_idx, 0, src_shape[1] - 1)
    x_idx = np.clip(x_idx, 0, src_shape[2] - 1)
    out[:] = volume[np.ix_(z_idx, y_idx, x_idx)]
    return out


def _to_mask(arr: np.ndarray) -> np.ndarray:
    """Binarise: anything non-zero becomes 1. Multi-label maps collapse
    to a single mask — the caller uploads one label at a time."""
    mask = (arr != 0).astype(np.uint8)
    return mask


def _parse_nifti(data: bytes) -> np.ndarray:
    """Returns an array shaped ``(nz, ny, nx)``. ``nibabel`` auto-detects
    .nii and .nii.gz based on content."""
    try:
        import nibabel as nib  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — dep pinned in pyproject
        raise SegmentationImportError("nibabel not installed") from exc

    try:
        # nibabel's FileHolder route handles both plain and gzipped data
        # when we give it a correctly-guessed fileobj.
        fh = io.BytesIO(gzip.decompress(data)) if data[:2] == b"\x1f\x8b" else io.BytesIO(data)
        holder = nib.FileHolder(fileobj=fh)
        img = nib.Nifti1Image.from_file_map({"header": holder, "image": holder})
        arr = np.asarray(img.dataobj)
    except Exception as exc:
        raise SegmentationImportError(f"invalid NIfTI file: {exc}") from exc

    # NIfTI is stored x-fastest (nx, ny, nz). Transpose to (nz, ny, nx)
    # so resampling / flattening matches the volume convention.
    if arr.ndim == 4:
        # Drop singleton time/channel dim if present (Slicer exports this way).
        if arr.shape[3] == 1:
            arr = arr[..., 0]
        else:
            raise SegmentationImportError(
                f"4D NIfTI with {arr.shape[3]} channels not supported; split first"
            )
    if arr.ndim != 3:
        raise SegmentationImportError(f"expected 3D volume, got {arr.ndim}D")
    return np.transpose(arr, (2, 1, 0))


def _parse_nrrd(data: bytes) -> np.ndarray:
    """Minimal NRRD v4/v5 parser — raw and gzip encodings only. Returns
    ``(nz, ny, nx)``. NRRD stores "fastest axis first" so the natural
    shape is already ``(nx, ny, nz)``; we transpose to match NIfTI."""
    # Split header / payload on the blank line required by the spec.
    sep = b"\n\n"
    idx = data.find(sep)
    if idx < 0:
        sep = b"\r\n\r\n"
        idx = data.find(sep)
    if idx < 0:
        raise SegmentationImportError("NRRD: missing header/body separator")
    header_bytes = data[:idx]
    payload = data[idx + len(sep) :]

    header: dict[str, str] = {}
    first = True
    for raw_line in header_bytes.split(b"\n"):
        line = raw_line.rstrip(b"\r").decode("ascii", errors="replace").strip()
        if not line or line.startswith("#"):
            continue
        if first:
            if not line.upper().startswith("NRRD"):
                raise SegmentationImportError("NRRD: missing magic")
            first = False
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            header[k.strip().lower()] = v.strip()

    try:
        sizes = [int(x) for x in header["sizes"].split()]
    except KeyError as exc:
        raise SegmentationImportError("NRRD: missing 'sizes' field") from exc
    if len(sizes) != 3:
        raise SegmentationImportError(f"NRRD: expected 3 sizes, got {len(sizes)}")
    encoding = header.get("encoding", "raw").lower()
    endian = header.get("endian", "little").lower()
    nrrd_type = header.get("type", "").lower()

    dtype_map = {
        "uchar": np.uint8,
        "unsigned char": np.uint8,
        "uint8": np.uint8,
        "uint8_t": np.uint8,
        "char": np.int8,
        "int8": np.int8,
        "short": np.int16,
        "signed short": np.int16,
        "int16": np.int16,
        "ushort": np.uint16,
        "unsigned short": np.uint16,
        "uint16": np.uint16,
        "int": np.int32,
        "int32": np.int32,
        "uint": np.uint32,
        "uint32": np.uint32,
        "float": np.float32,
        "double": np.float64,
    }
    dtype = dtype_map.get(nrrd_type)
    if dtype is None:
        raise SegmentationImportError(f"NRRD: unsupported type '{nrrd_type}'")

    if encoding in ("gz", "gzip"):
        payload = gzip.decompress(payload)
    elif encoding != "raw":
        raise SegmentationImportError(f"NRRD: unsupported encoding '{encoding}'")

    np_dtype = np.dtype(dtype)
    if np_dtype.itemsize > 1:
        np_dtype = np_dtype.newbyteorder("<" if endian == "little" else ">")
    expected = int(np.prod(sizes)) * np_dtype.itemsize
    if len(payload) < expected:
        raise SegmentationImportError("NRRD: payload shorter than declared sizes")
    arr = np.frombuffer(payload, dtype=np_dtype, count=int(np.prod(sizes)))
    # NRRD "sizes" is fastest-axis-first: (nx, ny, nz) for axial data.
    arr = arr.reshape(sizes[::-1])  # -> (nz, ny, nx)
    return arr.astype(arr.dtype.newbyteorder("="), copy=False)


def import_segmentation(
    *,
    data: bytes,
    filename: str,
    target_dims: tuple[int, int, int],
) -> ImportedMask:
    """Entry point used by the API route. ``target_dims`` is ``(nx, ny, nz)``
    as reported by the sibling ``volume.raw`` header. Returns a packed
    mask ready to upload to S3."""
    name = filename.lower()
    if name.endswith((".nii", ".nii.gz")):
        arr = _parse_nifti(data)
    elif name.endswith(".nrrd"):
        arr = _parse_nrrd(data)
    else:
        raise SegmentationImportError(
            f"unsupported extension on '{filename}' (expected .nii, .nii.gz, or .nrrd)"
        )

    tx_nx, tx_ny, tx_nz = target_dims
    # arr is (nz, ny, nx); resample if it doesn't match the target.
    arr = _resample_nearest(arr, (tx_nz, tx_ny, tx_nx))
    mask = _to_mask(arr)
    nonzero = int(mask.sum())

    # Flatten x-fastest so indexing matches volume.raw. ``mask`` is
    # ``(nz, ny, nx)`` contiguous; ``tobytes()`` walks in that order.
    packed = mask.tobytes(order="C")
    return ImportedMask(
        data=packed,
        dimensions=(tx_nx, tx_ny, tx_nz),
        nonzero_voxels=nonzero,
    )
