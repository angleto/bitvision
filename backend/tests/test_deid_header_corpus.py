"""TCIA Pseudo-PHI golden corpus — HARD gate for the PS3.15 header engine.

The pixel corpus (``BVP_PIXEL_DEID_CORPUS``) carries PHI only in pixels; this
gate covers the header half. With ``BVP_DEID_HEADER_CORPUS`` pointing at a
local corpus (built by ``bvphoenix-fetch-deid-header-corpus``), every instance
is run through ``deidentify_dicom_bytes`` and:

* no answer-key PHI value (len >= 4, not waived) survives anywhere in the
  output — HARD assert, header ground truth is exact (unlike the pixel boxes);
* no original structural UID survives (KeyedHashUID remap = no linkage);
* instances the engine withholds (``RequiresReview`` — SR/encapsulated — or a
  verification failure) count as SAFE, but their fraction is capped so
  fail-closed can never mask a broken engine.

Without the marker the corpus tests skip; the synthetic unit test at the
bottom always runs and pins the scorer itself.
"""

from __future__ import annotations

import os

import pytest

from bvphoenix.services.deid.errors import DeidVerificationError, RequiresReview
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.header_deid_eval import (
    HeaderCase,
    HeaderPhiValue,
    load_header_corpus,
    score_header_case,
)

_CORPUS = os.environ.get("BVP_DEID_HEADER_CORPUS")
_needs_corpus = pytest.mark.skipif(
    not _CORPUS, reason="BVP_DEID_HEADER_CORPUS not set (see bvphoenix-fetch-deid-header-corpus)"
)

# Fail-closed ceiling: withheld (RequiresReview / verify-failure) instances are
# safe but must stay a minority, or the engine is effectively broken and the
# "gate" would be vacuous.
MAX_WITHHELD_FRACTION = 0.30


@_needs_corpus
def test_header_corpus_no_phi_survives():
    cases = list(load_header_corpus(_CORPUS))
    assert cases, f"corpus at {_CORPUS} is empty"
    keyed = [c for c in cases if c.phi]
    assert keyed, "no instance matched the answer key — wrong key file or corpus?"

    withheld = 0
    failures: list[str] = []
    for case in cases:
        try:
            out = deidentify_dicom_bytes(case.dicom)
        except (RequiresReview, DeidVerificationError):
            withheld += 1  # withheld = never served = safe
            continue
        result = score_header_case(out, case)
        for phi in result.survivals:
            failures.append(f"{case.path.name}: {phi.category}={phi.value!r} survived the scrub")
        for uid in result.uid_leaks:
            failures.append(f"{case.path.name}: original UID {uid} survived (linkage leak)")

    assert not failures, "PHI survived the header engine:\n" + "\n".join(failures[:50])
    frac = withheld / len(cases)
    assert frac <= MAX_WITHHELD_FRACTION, (
        f"{withheld}/{len(cases)} instances withheld ({frac:.0%}) — fail-closed is "
        "hiding an engine problem, not passing the gate"
    )


# --- scorer self-test (always runs, no corpus needed) ------------------------


def _case_with_header_phi() -> HeaderCase:
    from io import BytesIO
    from pathlib import Path

    import pydicom
    from pydicom.dataset import FileMetaDataset
    from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

    ds = pydicom.Dataset()
    ds.PatientName = "Rossi^Mario"
    ds.PatientID = "MRN-445566"
    ds.PatientAddress = "Via Garibaldi 12, Torino"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = UID("1.2.840.10008.5.1.4.1.1.2")
    ds.Modality = "CT"
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return HeaderCase(
        path=Path("synthetic.dcm"),
        dicom=buf.getvalue(),
        sop_instance_uid=str(ds.SOPInstanceUID),
        phi=[
            HeaderPhiValue(value="Rossi", category="name"),
            HeaderPhiValue(value="Mario", category="name"),
            HeaderPhiValue(value="MRN-445566", category="id"),
            HeaderPhiValue(value="Via Garibaldi 12", category="address"),
        ],
        original_uids=(
            str(ds.StudyInstanceUID),
            str(ds.SeriesInstanceUID),
            str(ds.SOPInstanceUID),
        ),
    )


def test_scorer_passes_on_scrubbed_output():
    case = _case_with_header_phi()
    out = deidentify_dicom_bytes(case.dicom)
    result = score_header_case(out, case)
    assert result.passed, (result.survivals, result.uid_leaks)


def test_scorer_catches_unscrubbed_input():
    # Scoring the RAW bytes must light up: names + id + UIDs all survive.
    case = _case_with_header_phi()
    result = score_header_case(case.dicom, case)
    surviving = {p.value for p in result.survivals}
    assert "Rossi" in surviving and "MRN-445566" in surviving
    assert set(result.uid_leaks) == set(case.original_uids)


def test_scorer_honours_waiver_and_min_length():
    case = _case_with_header_phi()
    case.phi.append(HeaderPhiValue(value="CT", category="other"))  # < MIN_VALUE_LEN
    case.phi.append(HeaderPhiValue(value="Rossi", category="name", waived=True))
    result = score_header_case(case.dicom, case)
    values = {p.value for p in result.survivals}
    assert "CT" not in values
    # the un-waived duplicate of Rossi still fires (from _case_with_header_phi)
    assert "Rossi" in values


def test_load_corpus_absent_is_empty(tmp_path):
    assert list(load_header_corpus(tmp_path / "nope")) == []
