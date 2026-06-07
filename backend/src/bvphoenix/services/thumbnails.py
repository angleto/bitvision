"""DICOM slice → JPEG conversion for series previews and the 2D viewer.

Applies DICOM windowing (WindowCenter / WindowWidth) when available,
otherwise auto-windows to the data's min/max. The caller can pass
``wc_delta`` / ``ww_delta`` to shift the window interactively —
matching the drag-to-adjust UX from a clinical PACS viewer.

MONOCHROME1 photometric interpretation (high pixel values = black, e.g.
inverted-looking chest X-ray film negatives) is detected and the pixel
array is inverted before windowing so the output JPEG is always shown
in the conventional "high values = bright" form.
"""

from __future__ import annotations

import io

import numpy as np
import pydicom
from PIL import Image


class NoPixelDataError(Exception):
    pass


# SOP classes that can never carry pixel data — Presentation State,
# Structured Report, Key Object Selection, Encapsulated PDF / CDA.
# Used by ``api/studies.series_thumbnail`` to skip these instances when
# picking a default slice for the preview card so that a CD whose first
# (or middle) instance is an SR doesn't render the "no pixel data"
# placeholder when an image is sitting one index away.
#
# Secondary Capture (``1.2.840.10008.5.1.4.1.1.7.x``) is intentionally
# *not* listed here — it does carry pixel data, even though
# ``services.volumes`` flags it as non-volumetric.
NO_PIXEL_DATA_SOP_CLASSES: frozenset[str] = frozenset(
    {
        # Presentation states (annotation overlays applied to a
        # referenced image, no pixel data of their own).
        "1.2.840.10008.5.1.4.1.1.11.1",  # Grayscale Softcopy Pres State
        "1.2.840.10008.5.1.4.1.1.11.2",  # Color Softcopy Pres State
        "1.2.840.10008.5.1.4.1.1.11.3",  # Pseudo-Color
        "1.2.840.10008.5.1.4.1.1.11.4",  # Blending
        "1.2.840.10008.5.1.4.1.1.11.5",  # XA/XRF Grayscale
        # Structured reports / key object selection.
        "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
        "1.2.840.10008.5.1.4.1.1.88.22",  # Enhanced SR
        "1.2.840.10008.5.1.4.1.1.88.33",  # Comprehensive SR
        "1.2.840.10008.5.1.4.1.1.88.34",  # Comprehensive 3D SR
        "1.2.840.10008.5.1.4.1.1.88.40",  # Procedure Log
        "1.2.840.10008.5.1.4.1.1.88.50",  # Mammography CAD SR
        "1.2.840.10008.5.1.4.1.1.88.65",  # Chest CAD SR
        "1.2.840.10008.5.1.4.1.1.88.67",  # X-Ray Radiation Dose SR
        "1.2.840.10008.5.1.4.1.1.88.59",  # Key Object Selection
        # Encapsulated documents — the bytes are a PDF/CDA blob, not
        # a raster.
        "1.2.840.10008.5.1.4.1.1.104.1",  # Encapsulated PDF
        "1.2.840.10008.5.1.4.1.1.104.2",  # Encapsulated CDA
        # Raw data — arbitrary instrument/acquisition bytes (e.g. MR
        # spectroscopy raw, tracking) with no standard PixelData. Seen in
        # TCIA MR collections (ReMIND / UPENN-GBM); without this the embed
        # path treats it as an image, the worker fails to decode it, and
        # the backfill re-offers the series forever.
        "1.2.840.10008.5.1.4.1.1.66",  # Raw Data Storage
    }
)


def is_image_sop_class(sop_class_uid: str | None) -> bool:
    """True when the given SOP Class UID can carry pixel data.

    A ``None`` UID is treated as "probably image-like" so unclassified
    legacy data still resolves through the imaging path. Callers that
    need strict classification should match against
    ``NO_PIXEL_DATA_SOP_CLASSES`` directly.
    """
    if not sop_class_uid:
        return True
    return sop_class_uid not in NO_PIXEL_DATA_SOP_CLASSES


def read_dicom_wc_ww(ds: pydicom.Dataset) -> tuple[float, float] | None:
    """Read WindowCenter (0028,1050) / WindowWidth (0028,1051) from a dataset.

    Returns ``None`` when either tag is missing so callers can fall back to
    a computed default. When either tag is multi-valued (DICOM allows it),
    we take the first entry — that matches the clinical PACS convention
    and what :func:`dicom_to_jpeg` already does.
    """
    wc = getattr(ds, "WindowCenter", None)
    ww = getattr(ds, "WindowWidth", None)
    if wc is None or ww is None:
        return None
    if isinstance(wc, pydicom.multival.MultiValue):
        wc = wc[0]
    if isinstance(ww, pydicom.multival.MultiValue):
        ww = ww[0]
    try:
        return float(wc), float(ww)
    except (TypeError, ValueError):
        return None


def dicom_to_jpeg(
    dcm_bytes: bytes,
    *,
    quality: int = 80,
    max_side: int = 512,
    wc_delta: float = 0,
    ww_delta: float = 0,
) -> bytes:
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
    if "PixelData" not in ds and "FloatPixelData" not in ds and "DoubleFloatPixelData" not in ds:
        raise NoPixelDataError("DICOM instance has no pixel data")
    raw = ds.pixel_array

    # Normalise the array shape:
    #
    # * Single frame greyscale: ``(H, W)`` — pass-through.
    # * Multi-frame greyscale: ``(F, H, W)`` — pick the middle frame.
    # * Single-frame RGB: ``(H, W, 3)`` or ``(H, W, 4)`` — pass-through.
    # * Multi-frame RGB:   ``(F, H, W, 3)`` — pick the middle frame.
    # * DICOM also occasionally surfaces leading singleton axes
    #   (e.g. ``(1, 1, H, 3)``) when the SOP class wraps a still image
    #   in a multi-frame envelope; ``squeeze`` collapses them safely.
    raw = np.asarray(raw)
    while raw.ndim > 2 and raw.shape[0] == 1 and (raw.ndim != 3 or raw.shape[-1] not in (3, 4)):
        raw = raw[0]
    if raw.ndim == 4 and raw.shape[-1] in (3, 4):
        # Multi-frame colour: middle frame.
        raw = raw[raw.shape[0] // 2]
    elif raw.ndim == 3 and raw.shape[-1] not in (3, 4):
        # Multi-frame greyscale: middle frame.
        raw = raw[raw.shape[0] // 2]

    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "").upper().strip()
    is_rgb_pixel_array = raw.ndim == 3 and raw.shape[-1] in (3, 4)

    # Colour images (RGB / palette / YBR after pydicom auto-conversion)
    # short-circuit windowing — DICOM windowing is a single-channel
    # luminance transform, applying it to RGB just clips colour. We
    # rescale to uint8 and hand it to PIL as-is.
    if is_rgb_pixel_array:
        rgb = raw.astype(np.float32)
        rmin = float(rgb.min())
        rmax = float(rgb.max())
        rgb = (rgb - rmin) / (rmax - rmin) * 255.0 if rmax > rmin else np.zeros_like(rgb)
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        # Drop alpha if present — JPEG can't represent it.
        if rgb.shape[-1] == 4:
            rgb = rgb[..., :3]
        img = Image.fromarray(rgb, mode="RGB")
    else:
        arr = raw.astype(np.float32)

        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        if slope != 1.0 or intercept != 0.0:
            arr = arr * slope + intercept

        # MONOCHROME1 = high pixel = black (film negative convention).
        # Flip intensity early so the rest of the pipeline — windowing,
        # display — speaks the ordinary MONOCHROME2 dialect.
        is_monochrome1 = photometric == "MONOCHROME1"
        if is_monochrome1:
            arr = float(arr.max()) - arr

        wcww = read_dicom_wc_ww(ds)
        if wcww is not None:
            wc, ww = wcww
            # WindowCenter stored with MONOCHROME1 is expressed in the
            # original (un-inverted) space; flip it to match the
            # inverted arr.
            if is_monochrome1:
                wc = float(arr.max() + arr.min()) - wc
        else:
            wc = float((arr.min() + arr.max()) / 2)
            ww = float(arr.max() - arr.min())

        wc += wc_delta
        ww = max(1.0, ww + ww_delta)
        lo = wc - ww / 2
        hi = wc + ww / 2

        if hi <= lo:
            hi = lo + 1.0
        arr = np.clip((arr - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr, mode="L")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
