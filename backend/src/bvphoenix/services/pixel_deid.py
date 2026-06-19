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
from dataclasses import dataclass
from typing import Literal

import pydicom
from pydicom.multival import MultiValue

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


__all__ = [
    "FACE_RISK_MODALITIES",
    "HIGH_RISK_MODALITIES",
    "PixelRisk",
    "PixelRiskLevel",
    "classify_pixel_risk",
    "classify_pixel_risk_bytes",
]
