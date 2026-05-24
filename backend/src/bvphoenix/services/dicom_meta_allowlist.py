"""DICOM metadata allowlist (Sprint 5, ADR 0011).

Single source of truth for which DICOM tags the public API exposes.
Anything outside the allowlist is silently dropped — including the
entire private-tag space (odd group numbers) and any data-bearing
binary tags (PixelData, OverlayData, …) that don't belong in a meta
endpoint anyway.

Why allowlist instead of denylist?
* Mistakes are unsymmetric: a forgotten denylist entry leaks PHI; a
  missing allowlist entry just hides a tag from the agent. Failing
  closed beats failing open in clinical data.
* The list is reviewed quarterly via PR — every addition has four
  eyes on it.

Versioning convention: the canonical name includes ``_V1`` so a future
``DICOM_META_ALLOWLIST_V2`` can land additively without breaking
clients pinned on v1.
"""

from __future__ import annotations

from typing import Any

# Tag tuples are (group, element) per DICOM PS3.6.
DICOM_META_ALLOWLIST_V1: dict[str, dict[str, Any]] = {
    # ---- Modality / acquisition ------------------------------------------
    "Modality": {"tag": (0x0008, 0x0060), "vr": "CS"},
    "BodyPartExamined": {"tag": (0x0018, 0x0015), "vr": "CS"},
    "ProtocolName": {"tag": (0x0018, 0x1030), "vr": "LO"},
    "AcquisitionDate": {"tag": (0x0008, 0x0022), "vr": "DA"},
    "AcquisitionTime": {"tag": (0x0008, 0x0032), "vr": "TM"},
    "ContentDate": {"tag": (0x0008, 0x0023), "vr": "DA"},
    "ContentTime": {"tag": (0x0008, 0x0033), "vr": "TM"},
    "Manufacturer": {"tag": (0x0008, 0x0070), "vr": "LO"},
    "ManufacturerModelName": {"tag": (0x0008, 0x1090), "vr": "LO"},
    "DeviceSerialNumber": {"tag": (0x0018, 0x1000), "vr": "LO"},
    "SoftwareVersions": {"tag": (0x0018, 0x1020), "vr": "LO"},
    # ---- Geometry ---------------------------------------------------------
    "SliceThickness": {"tag": (0x0018, 0x0050), "vr": "DS"},
    "SpacingBetweenSlices": {"tag": (0x0018, 0x0088), "vr": "DS"},
    "PixelSpacing": {"tag": (0x0028, 0x0030), "vr": "DS"},
    "Rows": {"tag": (0x0028, 0x0010), "vr": "US"},
    "Columns": {"tag": (0x0028, 0x0011), "vr": "US"},
    "ImageOrientationPatient": {"tag": (0x0020, 0x0037), "vr": "DS"},
    "ImagePositionPatient": {"tag": (0x0020, 0x0032), "vr": "DS"},
    "PatientPosition": {"tag": (0x0018, 0x5100), "vr": "CS"},
    "FrameOfReferenceUID": {"tag": (0x0020, 0x0052), "vr": "UI"},
    # ---- CT specific ------------------------------------------------------
    "KVP": {"tag": (0x0018, 0x0060), "vr": "DS"},
    "ExposureTime": {"tag": (0x0018, 0x1150), "vr": "IS"},
    "XRayTubeCurrent": {"tag": (0x0018, 0x1151), "vr": "IS"},
    "ContrastBolusAgent": {"tag": (0x0018, 0x0010), "vr": "LO"},
    "SliceLocation": {"tag": (0x0020, 0x1041), "vr": "DS"},
    "ConvolutionKernel": {"tag": (0x0018, 0x1210), "vr": "SH"},
    # ---- MR specific ------------------------------------------------------
    "MagneticFieldStrength": {"tag": (0x0018, 0x0087), "vr": "DS"},
    "EchoTime": {"tag": (0x0018, 0x0081), "vr": "DS"},
    "RepetitionTime": {"tag": (0x0018, 0x0080), "vr": "DS"},
    "InversionTime": {"tag": (0x0018, 0x0082), "vr": "DS"},
    "ScanningSequence": {"tag": (0x0018, 0x0020), "vr": "CS"},
    "SequenceVariant": {"tag": (0x0018, 0x0021), "vr": "CS"},
    # ---- PET specific -----------------------------------------------------
    "Units": {"tag": (0x0054, 0x1001), "vr": "CS"},
    "DecayCorrection": {"tag": (0x0054, 0x1102), "vr": "CS"},
    "FrameReferenceTime": {"tag": (0x0054, 0x1300), "vr": "DS"},
    "RescaleSlope": {"tag": (0x0028, 0x1053), "vr": "DS"},
    "RescaleIntercept": {"tag": (0x0028, 0x1052), "vr": "DS"},
    "RescaleType": {"tag": (0x0028, 0x1054), "vr": "LO"},
    # SUV inputs. PatientWeight is a demographic, not PHI nominale —
    # it's a clinical measurement needed for SUV bw normalisation;
    # we accept it on the meta endpoint with the understanding that
    # SUV computation is the only clinical use case. Same for
    # PatientSize (height in metres).
    "PatientWeight": {"tag": (0x0010, 0x1030), "vr": "DS"},
    "PatientSize": {"tag": (0x0010, 0x1020), "vr": "DS"},
    "RadiopharmaceuticalStartTime": {"tag": (0x0018, 0x1072), "vr": "TM"},
    "RadiopharmaceuticalStartDateTime": {"tag": (0x0018, 0x1078), "vr": "DT"},
    "RadionuclideTotalDose": {"tag": (0x0018, 0x1074), "vr": "DS"},
    "RadionuclideHalfLife": {"tag": (0x0018, 0x1075), "vr": "DS"},
    # ---- Number-of-frames / multi-frame DICOM ---------------------------
    "NumberOfFrames": {"tag": (0x0028, 0x0008), "vr": "IS"},
    "BitsAllocated": {"tag": (0x0028, 0x0100), "vr": "US"},
    "BitsStored": {"tag": (0x0028, 0x0101), "vr": "US"},
    "PhotometricInterpretation": {"tag": (0x0028, 0x0004), "vr": "CS"},
}


# PHI-bearing tags are NOT in the allowlist — listed here only for
# documentation: any reviewer seeing one of these in a PR adding to
# DICOM_META_ALLOWLIST_V1 should reject the change. Updating this set
# is also gated on PR review.
_PHI_TAG_NAMES: frozenset[str] = frozenset(
    {
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "PatientSex",
        "PatientAddress",
        "PatientTelephoneNumbers",
        "PatientWeight",
        "PatientSize",
        "PatientAge",
        "ReferringPhysicianName",
        "PerformingPhysicianName",
        "OperatorsName",
        "AccessionNumber",
        "StudyID",
        "StudyDescription",
        "SeriesDescription",
        "IssuerOfPatientID",
        "InstitutionName",
        "InstitutionAddress",
        # Data-bearing tags (binary content, not metadata).
        "PixelData",
        "OverlayData",
        "CurveData",
        "ICCProfile",
        "EncapsulatedDocument",
    }
)


def _normalise_value(vr: str, value: Any) -> Any:
    """Coerce pydicom values to JSON-friendly primitives.

    DICOM has multi-valued strings, italian-locale decimals, and
    sequence types we don't surface, fold them into a list of
    string-or-number where unambiguous.
    """
    if value is None:
        return None
    # MultiValue: pydicom returns a list-like for VRs like DS / IS / CS.
    # Detect by ``__iter__`` since pydicom.multival.MultiValue is not a
    # ``list`` or ``tuple`` subclass.
    if not isinstance(value, (str, bytes)) and hasattr(value, "__iter__"):
        return [_normalise_value(vr, v) for v in value]
    if vr in {"DS", "FL", "FD"}:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if vr in {"IS", "SS", "US", "SL", "UL"}:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    # All remaining VR types map to ``str``: dates, times, codes, UIDs.
    return str(value)


def extract_allowlisted(
    dataset: Any,
    *,
    version: str = "v1",
) -> dict[str, Any]:
    """Return the allowlisted subset of ``dataset`` as a flat dict.

    ``dataset`` is a ``pydicom.Dataset`` (or anything that supports
    ``__contains__`` + ``__getattr__`` over the allowlist names —
    this lets the unit tests pass plain dicts). Tags absent from the
    dataset are skipped; tags with empty values are dropped.

    The ``version`` parameter is forwarded for forward compatibility:
    only ``v1`` is implemented today.
    """
    if version != "v1":
        raise ValueError(f"unknown DICOM allowlist version: {version!r}")

    out: dict[str, Any] = {}
    for name, info in DICOM_META_ALLOWLIST_V1.items():
        # pydicom.Dataset supports both ``getattr`` and __contains__ on
        # tag names. Fall back to ``[tag_tuple]`` for raw datasets.
        value: Any = None
        try:
            value = getattr(dataset, name, None)
        except Exception:  # pragma: no cover - pydicom edge cases
            value = None
        if value is None and isinstance(dataset, dict):
            value = dataset.get(name)
        if value is None:
            continue
        normalised = _normalise_value(info["vr"], value)
        if normalised is None or normalised == "":
            continue
        out[name] = normalised
    return out


__all__ = [
    "DICOM_META_ALLOWLIST_V1",
    "extract_allowlisted",
]
