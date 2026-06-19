"""Burned-in pixel PHI: risk classification (and, later, redaction).

Background
----------
``services.deidentify`` scrubs the DICOM *header*. It deliberately leaves
**pixel data untouched** — patient identifiers burned into the pixels
(ultrasound vendor banners, secondary-capture screenshots, dose-report
screenshots, scanned documents) are a separate problem and the single
remaining PHI leak on the public-egress paths (``training_cohort_export``,
OpenData publish), which currently stream header-only-scrubbed bytes.

This module is the burned-in-pixel pipeline. It is built ``a poco a poco``:

* **M0 (here):** :func:`classify_pixel_risk` — a cheap, header-only gate that
  decides whether an instance *may* carry burned-in PHI. The egress paths use
  it to **block** high-risk instances (route them to human review) instead of
  shipping them. No pixel decode, no OCR, no model: just classification.
* **M4:** ``clean_pixel_data`` — template masking + OCR + regex redaction.
* **M5:** the VLM tier for dense-overlay hard cases.

Design rule (DICOM PS3.15 "Clean Pixel Data Option" + NIH/NCI MIDI-B 2024-25):
*no automated method removes 100% of burned-in PHI*, so the classifier is
deliberately **over-inclusive** — it errs toward ``high`` (→ human review)
rather than risk a false ``none``. ``BurnedInAnnotation == "NO"`` is treated as
unreliable and never downgrades a modality-based ``high`` (vendors set "NO"
on US frames that clearly carry text).

The classifier reads only header attributes (Modality, SOPClassUID,
BurnedInAnnotation, ImageType, BodyPartExamined), all of which precede
``PixelData`` — so it runs on a dataset parsed with ``stop_before_pixels=True``
(as the ingest path does) without decoding a single pixel.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pydicom
from PIL import Image
from pydicom.multival import MultiValue
from pydicom.uid import ExplicitVRLittleEndian

from bvphoenix.services.deidentify import _EMAIL_RE, _PHONE_RE, _TAX_ID_RE
from bvphoenix.services.thumbnails import NO_PIXEL_DATA_SOP_CLASSES

PixelRiskLevel = Literal["none", "low", "high"]

# Secondary Capture image storage family (1.2.840.10008.5.1.4.1.1.7[.x]):
# single- and multi-frame SC. These are screenshots / scanned documents
# wrapped as DICOM and are the canonical burned-in-PHI carrier.
_SECONDARY_CAPTURE_PREFIX = "1.2.840.10008.5.1.4.1.1.7"

# Encapsulated documents (PDF / CDA / STL) and the X-Ray Radiation Dose SR
# carry free-text PHI even though they have no raster PixelData; there is no
# safe automated pixel path for them, so they are flagged high → review.
_ENCAPSULATED_SOP_CLASSES: frozenset[str] = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.104.1",  # Encapsulated PDF
        "1.2.840.10008.5.1.4.1.1.104.2",  # Encapsulated CDA
        "1.2.840.10008.5.1.4.1.1.104.3",  # Encapsulated STL
    }
)
_XRAY_DOSE_SR_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.88.67"  # X-Ray Radiation Dose SR

# Modalities that routinely burn text into the image: ultrasound, secondary
# capture, screenshots, ECG/waveform renders, endoscopy, dermatology, generic
# "other". Cross-sectional CT/MR/PT *primary* image data is handled separately
# (face risk only). XC = external-camera photography, GM = general microscopy,
# IO = intra-oral, ES = endoscopy, OT = other.
HIGH_RISK_MODALITIES: frozenset[str] = frozenset(
    {"US", "SC", "OT", "XC", "ECG", "IO", "GM", "DOC", "ES", "OP"}
)

# Modalities whose head/face acquisitions carry recognizable visual features
# (3D facial reconstruction risk — PS3.15 "Clean Recognizable Visual Features
# Option"). Flagged ``low``; actual de-facing is a later milestone.
FACE_RISK_MODALITIES: frozenset[str] = frozenset({"CT", "MR", "PT"})
_FACE_BODY_PARTS: frozenset[str] = frozenset(
    {"HEAD", "SKULL", "FACE", "BRAIN", "ORBIT", "ORBITS", "SINUS", "TMJ", "NECK"}
)


@dataclass(frozen=True)
class PixelRisk:
    """Outcome of :func:`classify_pixel_risk`.

    ``level`` is the gate decision; ``reasons`` is a stable, machine-readable
    list of why (for audit + the review queue's ``details``).
    ``burned_in_annotation`` / ``recognizable_visual_features`` echo the raw
    DICOM tag values (0028,0301 / 0028,0302) for provenance.
    """

    level: PixelRiskLevel
    reasons: tuple[str, ...] = ()
    burned_in_annotation: str | None = None
    recognizable_visual_features: str | None = None

    @property
    def is_high(self) -> bool:
        return self.level == "high"


def _str_values(ds: pydicom.Dataset, keyword: str) -> list[str]:
    """Return a DICOM element's value(s) as a list of upper-cased strings.

    Handles single-valued, multi-valued (``MultiValue``), and absent elements
    uniformly so callers don't special-case ``ImageType`` (multi-valued) vs
    ``Modality`` (single).
    """
    value = ds.get(keyword, None)
    if value is None:
        return []
    if isinstance(value, (MultiValue, list, tuple)):
        return [str(v).strip().upper() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text.upper()] if text else []


def _first(ds: pydicom.Dataset, keyword: str) -> str | None:
    vals = _str_values(ds, keyword)
    return vals[0] if vals else None


def classify_pixel_risk(ds: pydicom.Dataset) -> PixelRisk:
    """Classify an instance's burned-in-PHI pixel risk from its header alone.

    Returns:
        * ``high`` — may carry burned-in PHI in pixels (or is a text-bearing
          encapsulated/dose document): must be redacted + human-reviewed
          before any public egress.
        * ``low`` — cross-sectional head/face data with recognizable-visual-
          feature risk (no burned-in text expected); informational until
          de-facing lands.
        * ``none`` — no burned-in-pixel risk (cross-sectional primary image
          data, or a no-pixel-data object like a presentation state / generic
          SR / raw data).

    Over-inclusive by design: a false ``high`` only costs a human review; a
    false ``none`` leaks PHI.
    """
    modality = (_first(ds, "Modality") or "").upper()
    sop = (_first(ds, "SOPClassUID") or "").strip()
    image_type = _str_values(ds, "ImageType")
    burned = _first(ds, "BurnedInAnnotation")
    rvf = _first(ds, "RecognizableVisualFeatures")

    reasons: list[str] = []

    # Explicit positive burned-in flag is authoritative for ``high``. A "NO"
    # is NOT trusted (vendors mislabel), so it never appears here.
    if burned == "YES":
        reasons.append("burned_in_annotation_yes")

    # Secondary capture / encapsulated document / dose-report SR.
    if sop.startswith(_SECONDARY_CAPTURE_PREFIX):
        reasons.append("secondary_capture_sop")
    if sop in _ENCAPSULATED_SOP_CLASSES:
        reasons.append("encapsulated_document_sop")
    if sop == _XRAY_DOSE_SR_SOP_CLASS:
        reasons.append("xray_dose_report_sr")

    # High-risk modality.
    if modality in HIGH_RISK_MODALITIES:
        reasons.append(f"high_risk_modality:{modality}")

    # ImageType secondary / screen-capture markers. Plain DERIVED is NOT a
    # signal (legitimate CT/MR reformats are DERIVED); SECONDARY and SCREEN
    # SAVE/SCREENSHOT are.
    if "SECONDARY" in image_type:
        reasons.append("image_type_secondary")
    if any("SCREEN" in v for v in image_type):
        reasons.append("image_type_screen_capture")

    # Overlay planes (groups 6000-60FF) carry graphic/text annotations rendered
    # over the image — a classic burned-in-PHI carrier even on cross-sectional
    # CT/MR. The header engine removes OverlayData, but an overlay already
    # rasterised into the displayed pixels would be invisible to it, so flag the
    # instance for review. (Does not catch text baked into the matrix with no
    # overlay + no BurnedInAnnotation tag — that residual needs OCR, M4.)
    if any(0x6000 <= elem.tag.group <= 0x60FF for elem in ds):
        reasons.append("overlay_plane_present")

    if reasons:
        return PixelRisk("high", tuple(reasons), burned, rvf)

    # No-pixel-data SOP classes that aren't one of the text-bearing special
    # cases above (presentation states, generic SR, KOS, raw data) carry no
    # burned-in pixel PHI.
    if sop and sop in NO_PIXEL_DATA_SOP_CLASSES:
        return PixelRisk("none", ("no_pixel_data_sop",), burned, rvf)

    # Cross-sectional head/face → recognizable-visual-feature risk.
    if modality in FACE_RISK_MODALITIES:
        body = (_first(ds, "BodyPartExamined") or "").upper()
        if rvf == "YES" or body in _FACE_BODY_PARTS:
            why = "recognizable_visual_features_yes" if rvf == "YES" else f"face_region:{body}"
            return PixelRisk("low", (why,), burned, rvf)

    return PixelRisk("none", (), burned, rvf)


def classify_pixel_risk_bytes(blob: bytes) -> PixelRisk:
    """Classify burned-in-pixel risk straight from raw DICOM bytes.

    Parses the header only (``stop_before_pixels=True``) so the public-egress
    gate can decide, authoritatively from the bytes about to be shipped,
    whether an instance may carry burned-in PHI — independent of any persisted
    ``pixel_phi_risk`` (which may be NULL for pre-existing rows). ``force=True``
    tolerates preamble-less objects, matching ``dicom_ingest.parse_dicom_header``.

    Fail-closed: if the bytes can't be parsed as DICOM, the instance is
    classified ``high`` (excluded + flagged) rather than assumed safe — a
    corrupt blob must never slip un-screened onto a public path.
    """
    try:
        ds = pydicom.dcmread(io.BytesIO(blob), stop_before_pixels=True, force=True)
    except Exception:
        return PixelRisk("high", ("unparseable_dicom",))
    return classify_pixel_risk(ds)


# ===========================================================================
# M4: automated burned-in-pixel redaction (Tesseract tier)
# ===========================================================================
# The default mode masks EVERY detected text box (over-redaction, recall-first —
# distinguishing PHI from clinical scan text reliably is the VLM tier, M5).
# Selective mode keeps only PHI-shaped tokens. Either way a high-risk instance
# ALWAYS stays residual_suspect (human review) — the redaction reduces what the
# reviewer sees, it never auto-clears (MIDI-B: no automated pass is 100%).

# Broad PHI shapes for the selective classifier (reuses the header regexes).
_DATE_ANY_RE = re.compile(r"\b\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}\b")
_MRN_RE = re.compile(r"\b[A-Z]{0,4}[-/ ]?\d{5,}\b")
_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ'’]{2,}$")


@dataclass(frozen=True)
class TextBox:
    x: int
    y: int
    w: int
    h: int
    text: str
    conf: float


@dataclass
class PixelDeidResult:
    out_bytes: bytes
    risk: PixelRisk
    residual_suspect: bool
    redactions: list[dict] = field(default_factory=list)
    detected_text: bool = False
    decode_failed: bool = False
    # For the low-risk (face) path: which defacer ran / why it did not. None when
    # de-facing was not attempted (high-risk path or de-facing disabled).
    face_deid_reason: str | None = None


def _frames_view(arr: np.ndarray, ds: pydicom.Dataset) -> list[np.ndarray]:
    """Views into ``arr`` (writes propagate back) split per frame.

    Grayscale: (H,W) or (F,H,W). Colour: (H,W,3) or (F,H,W,3)."""
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if samples >= 3:
        return [arr[i] for i in range(arr.shape[0])] if arr.ndim == 4 else [arr]
    return [arr[i] for i in range(arr.shape[0])] if arr.ndim == 3 else [arr]


def _frame_to_ocr_image(frame: np.ndarray, photometric: str) -> Image.Image:
    a = np.asarray(frame).astype(np.float32)
    if a.ndim == 3:
        a = a.mean(axis=2)
    lo, hi = float(a.min()), float(a.max())
    a = (a - lo) / (hi - lo) * 255.0 if hi > lo else np.zeros_like(a)
    if photometric == "MONOCHROME1":
        a = 255.0 - a
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), mode="L")


def detect_text_boxes(
    img: Image.Image, *, languages: str = "eng", min_conf: float = 30.0
) -> list[TextBox]:
    """Tesseract word boxes above ``min_conf``. Returns [] if the tesseract
    binary is unavailable — a high-risk image with no detections is treated as
    residual (human review) by the caller, never as clean."""
    try:
        import pytesseract

        data = pytesseract.image_to_data(img, lang=languages, output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    boxes: list[TextBox] = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if text and conf >= min_conf:
            boxes.append(
                TextBox(
                    int(data["left"][i]),
                    int(data["top"][i]),
                    int(data["width"][i]),
                    int(data["height"][i]),
                    text,
                    conf,
                )
            )
    return boxes


def _looks_like_phi(text: str) -> bool:
    t = text.strip()
    return bool(
        _TAX_ID_RE.search(t)
        or _EMAIL_RE.search(t)
        or _PHONE_RE.search(t)
        or _DATE_ANY_RE.search(t)
        or _MRN_RE.search(t)
        or _NAME_RE.match(t)
    )


def classify_text_phi(boxes: list[TextBox]) -> list[TextBox]:
    """Selective filter: keep only PHI-shaped detections (the default mode masks
    every detected box)."""
    return [b for b in boxes if _looks_like_phi(b.text)]


def _black_value(ds: pydicom.Dataset) -> int:
    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "").upper()
    if photometric == "MONOCHROME1":
        bits = int(getattr(ds, "BitsStored", 8) or 8)
        return (1 << bits) - 1  # high value renders black under MONOCHROME1
    return 0


def _clip(x: int, y: int, w: int, h: int, shape: tuple) -> tuple[int, int, int, int]:
    height, width = int(shape[0]), int(shape[1])
    x0 = max(0, min(int(x), width))
    y0 = max(0, min(int(y), height))
    x1 = max(0, min(int(x + w), width))
    y1 = max(0, min(int(y + h), height))
    return x0, y0, x1, y1


def redact_frames(
    frames: list[np.ndarray], boxes_per_frame: list[list[tuple[int, int, int, int]]], *, black: int
) -> None:
    for frame, boxes in zip(frames, boxes_per_frame, strict=True):
        for x, y, w, h in boxes:
            x0, y0, x1, y1 = _clip(x, y, w, h, frame.shape)
            if x1 > x0 and y1 > y0:
                if frame.ndim == 2:
                    frame[y0:y1, x0:x1] = black
                else:
                    frame[y0:y1, x0:x1, :] = 0


def reencode_pixel_data(ds: pydicom.Dataset, arr: np.ndarray) -> None:
    """Write (modified) pixels back as uncompressed Explicit VR Little Endian.

    A compressed source is implicitly decompressed by ``pixel_array``; we re-emit
    uncompressed (simplest correct round-trip) and fix the transfer syntax."""
    ds.PixelData = np.ascontiguousarray(arr).tobytes()
    ds["PixelData"].VR = "OW" if int(getattr(ds, "BitsAllocated", 8) or 8) > 8 else "OB"
    if getattr(ds, "file_meta", None) is not None:
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian


_AUTO = object()  # sentinel: resolve the VLM engine / defacer from config


def _deface_low_risk(
    ds: pydicom.Dataset, src: bytes, risk: PixelRisk, face_defacer: object
) -> PixelDeidResult:
    """Low-risk (recognizable-visual-feature) path. With de-facing disabled
    (default) the instance ships unchanged. With a defacer configured it is
    masked + flagged for human review; provenance is written only on accept."""
    from bvphoenix.services.face_deid import get_defacer

    defacer = get_defacer() if face_defacer is _AUTO else face_defacer
    if defacer is None:
        # De-facing disabled → preserve today's behaviour (ship as-is).
        return PixelDeidResult(out_bytes=src, risk=risk, residual_suspect=False)
    body = _first(ds, "BodyPartExamined") or ""
    try:
        arr = np.asarray(ds.pixel_array)
    except Exception:
        return PixelDeidResult(
            out_bytes=src,
            risk=risk,
            residual_suspect=True,
            decode_failed=True,
            face_deid_reason="decode_failed",
        )
    frames = _frames_view(arr, ds)
    result = defacer.deface(frames, body_part=body)
    if not result.applied:
        # Nothing masked (NullDefacer, or an ROI-bearing body part the heuristic
        # refuses): features NOT removed, so ship as-is but mark residual_suspect
        # so an egress gate can hold it for review.
        return PixelDeidResult(
            out_bytes=src, risk=risk, residual_suspect=True, face_deid_reason=result.reason
        )
    redact_frames(frames, result.boxes_per_frame, black=_black_value(ds))
    reencode_pixel_data(ds, arr)
    out = io.BytesIO()
    ds.save_as(out, write_like_original=False)
    redactions = [
        {"x": x, "y": y, "w": w, "h": h, "text": "<face>", "conf": -1.0}
        for boxes in result.boxes_per_frame
        for (x, y, w, h) in boxes
    ]
    return PixelDeidResult(
        out_bytes=out.getvalue(),
        risk=risk,
        residual_suspect=True,  # human confirms before RecognizableVisualFeatures=NO
        redactions=redactions,
        face_deid_reason=result.reason,
    )


def clean_pixel_data(
    src: bytes,
    *,
    languages: str = "eng",
    min_conf: float = 30.0,
    selective: bool | None = None,
    vlm_engine: object = _AUTO,
    face_defacer: object = _AUTO,
) -> PixelDeidResult:
    """Redact burned-in text from a high-risk instance's pixels.

    Returns the redacted bytes + the redaction boxes. ``residual_suspect`` is
    ALWAYS True for high-risk (a human must confirm); provenance
    (BurnedInAnnotation=NO + 113101) is written only after that accept, via
    :func:`mark_pixels_clean`. ``none`` risk passes through untouched.

    ``low`` (face) risk is routed to the de-facing seam: with de-facing disabled
    (default) it ships unchanged; with a defacer configured the result carries
    ``residual_suspect=True`` + ``face_deid_reason`` (the signal an egress gate
    consumes to hold it for human review), and ``RecognizableVisualFeatures=NO``
    + CID 7050 ``113102`` are written only after accept via
    :func:`mark_visual_features_removed`. NOTE: wiring an egress gate
    (PixelPhiCheck / training_cohort_export) to that signal is follow-up work;
    today the signal is produced but not yet consumed (de-facing is off by
    default, so this is not a current-behavior regression).

    ``selective`` chooses the redaction mode for the high-risk text path:
    ``None`` resolves from config (``BVP_PIXEL_DEID_REDACTION_MODE``, default
    over-redact); ``True`` keeps clinical scan text and masks only PHI-shaped
    boxes; ``False`` masks every detected box.

    ``vlm_engine`` is the M5 hard-case tier: when an OCR-blank high-risk frame is
    found (dense overlay / OCR miss), the engine is consulted for extra boxes.
    Default ``_AUTO`` resolves it from config (off unless enabled); pass an
    engine instance to force it, or ``None`` to disable.
    """
    ds = pydicom.dcmread(io.BytesIO(src))
    risk = classify_pixel_risk(ds)
    if risk.level == "none":
        return PixelDeidResult(out_bytes=src, risk=risk, residual_suspect=False)
    if risk.level == "low":
        return _deface_low_risk(ds, src, risk, face_defacer)
    try:
        arr = np.asarray(ds.pixel_array)
    except Exception:
        # Undecodable pixels can't be redacted → block (no output, review).
        return PixelDeidResult(out_bytes=src, risk=risk, residual_suspect=True, decode_failed=True)

    if selective is None:
        from bvphoenix.config import get_settings

        selective = (get_settings().pixel_deid_redaction_mode or "").strip().lower() == "selective"

    if vlm_engine is _AUTO:
        from bvphoenix.services.pixel_phi_engine import get_pixel_phi_engine

        engine = get_pixel_phi_engine()
    else:
        engine = vlm_engine

    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "").upper()
    black = _black_value(ds)
    frames = _frames_view(arr, ds)
    redactions: list[dict] = []
    boxes_per_frame: list[list[tuple[int, int, int, int]]] = []
    detected = False
    for frame in frames:
        ocr_img = _frame_to_ocr_image(frame, photometric)
        boxes = detect_text_boxes(ocr_img, languages=languages, min_conf=min_conf)
        if boxes:
            detected = True
        keep = classify_text_phi(boxes) if selective else boxes
        frame_boxes = [(b.x, b.y, b.w, b.h) for b in keep]
        redactions.extend(
            {"x": b.x, "y": b.y, "w": b.w, "h": b.h, "text": b.text, "conf": b.conf} for b in keep
        )
        # M5 hard-case tier: an OCR-blank high-risk frame (dense overlay the
        # cheap tier couldn't read) is escalated. NullEngine over-redacts the
        # frame; HttpEngine queries the in-cluster model. Only when enabled.
        if engine is not None and not boxes:
            for x, y, w, h in engine.detect_boxes(np.asarray(ocr_img)):
                frame_boxes.append((x, y, w, h))
                redactions.append({"x": x, "y": y, "w": w, "h": h, "text": "<vlm>", "conf": -1.0})
        boxes_per_frame.append(frame_boxes)
    redact_frames(frames, boxes_per_frame, black=black)
    reencode_pixel_data(ds, arr)
    out = io.BytesIO()
    ds.save_as(out, write_like_original=False)
    return PixelDeidResult(
        out_bytes=out.getvalue(),
        risk=risk,
        residual_suspect=True,  # high-risk always needs human confirmation
        redactions=redactions,
        detected_text=detected,
    )


# CID 7050 Clean Pixel Data Option — stamped ONLY after a human confirms the
# redacted result, never automatically.
_CLEAN_PIXEL_CODE = ("113101", "DCM", "Clean Pixel Data Option")


def mark_pixels_clean(ds: pydicom.Dataset) -> None:
    """Set BurnedInAnnotation=NO + append the Clean Pixel Data Option code. Call
    ONLY after a human confirms the redacted image carries no residual PHI."""
    from pydicom.dataset import Dataset as _DS
    from pydicom.sequence import Sequence as _Seq

    ds.BurnedInAnnotation = "NO"
    existing = list(getattr(ds, "DeidentificationMethodCodeSequence", []) or [])
    if not any(getattr(e, "CodeValue", None) == "113101" for e in existing):
        item = _DS()
        item.CodeValue, item.CodingSchemeDesignator, item.CodeMeaning = _CLEAN_PIXEL_CODE
        ds.DeidentificationMethodCodeSequence = _Seq([*existing, item])


# CID 7050 Clean Recognizable Visual Features Option, stamped ONLY after a human
# confirms the defaced image, never automatically.
_VISUAL_FEATURES_CODE = ("113102", "DCM", "Clean Recognizable Visual Features Option")


def mark_visual_features_removed(ds: pydicom.Dataset) -> None:
    """Set RecognizableVisualFeatures=NO + append the Clean Recognizable Visual
    Features Option code. Call ONLY after a human confirms the defaced image
    carries no recognizable face."""
    from pydicom.dataset import Dataset as _DS
    from pydicom.sequence import Sequence as _Seq

    ds.RecognizableVisualFeatures = "NO"
    existing = list(getattr(ds, "DeidentificationMethodCodeSequence", []) or [])
    if not any(getattr(e, "CodeValue", None) == "113102" for e in existing):
        item = _DS()
        item.CodeValue, item.CodingSchemeDesignator, item.CodeMeaning = _VISUAL_FEATURES_CODE
        ds.DeidentificationMethodCodeSequence = _Seq([*existing, item])


__all__ = [
    "FACE_RISK_MODALITIES",
    "HIGH_RISK_MODALITIES",
    "PixelDeidResult",
    "PixelRisk",
    "PixelRiskLevel",
    "TextBox",
    "classify_pixel_risk",
    "classify_pixel_risk_bytes",
    "classify_text_phi",
    "clean_pixel_data",
    "detect_text_boxes",
    "mark_pixels_clean",
    "mark_visual_features_removed",
    "redact_frames",
    "reencode_pixel_data",
]
