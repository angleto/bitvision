"""PS3.15 / DICOM PS3.16 CID 7050 de-identification provenance.

Builds the coded ``DeidentificationMethodCodeSequence`` (0012,0064) that records
which confidentiality options were applied, plus the human-readable
``DeidentificationMethod`` string.
"""

from __future__ import annotations

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from bvphoenix.services.deid.profile_table import ProfileOptions

# CID 7050 "De-identification Method" coded entries (DCM coding scheme).
_BASIC = ("113100", "Basic Application Confidentiality Profile")
_CLEAN_DESCRIPTORS = ("113105", "Clean Descriptors Option")
_MODIFIED_DATES = ("113107", "Retain Longitudinal Temporal Information Modified Dates Option")
_RETAIN_CHARACTERISTICS = ("113108", "Retain Patient Characteristics Option")
_RETAIN_DEVICE = ("113109", "Retain Device Identity Option")
_RETAIN_SAFE_PRIVATE = ("113111", "Retain Safe Private Option")


def method_codes(options: ProfileOptions) -> list[tuple[str, str]]:
    codes: list[tuple[str, str]] = [_BASIC]
    if options.clean_descriptors:
        codes.append(_CLEAN_DESCRIPTORS)
    if options.date_policy == "shift":
        codes.append(_MODIFIED_DATES)
    if options.retain_patient_characteristics:
        codes.append(_RETAIN_CHARACTERISTICS)
    if options.retain_device_identity:
        codes.append(_RETAIN_DEVICE)
    if options.retain_safe_private:
        codes.append(_RETAIN_SAFE_PRIVATE)
    return codes


def _code_item(value: str, meaning: str) -> Dataset:
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = "DCM"
    item.CodeMeaning = meaning
    return item


def build_method_code_sequence(options: ProfileOptions) -> Sequence:
    return Sequence(_code_item(value, meaning) for value, meaning in method_codes(options))


def method_string(options: ProfileOptions) -> str:
    return f"bitvision phoenix PS3.15 Basic Profile ({options.method_version})"


__all__ = ["build_method_code_sequence", "method_codes", "method_string"]
