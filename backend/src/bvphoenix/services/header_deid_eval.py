"""Golden-corpus evaluation of the PS3.15 HEADER engine (TCIA Pseudo-PHI).

Sibling of :mod:`pixel_deid_eval`, for the other half of the problem: the
pixel corpus carries PHI only in pixels, so it never exercises the header
engine. This module consumes the TCIA *Pseudo-PHI-DICOM-Data* collection
(CC BY 4.0, synthetic PHI in headers AND pixels, with a ground-truth answer
key) and asserts that no known PHI value survives ``deidentify_dicom_bytes``.

Corpus layout (built by ``bvphoenix-fetch-deid-header-corpus``)::

    <root>/<patient>/<series>/*.dcm
    <root>/answer_key_header.json    # {sop_instance_uid: [{value, category,
                                     #   element?, waived?}]}

Marker-gated like the pixel corpus: the corpus is never committed and the test
(``test_deid_header_corpus.py``) skips unless ``BVP_DEID_HEADER_CORPUS`` points
at a local dir. Unlike the noisy pixel boxes, header ground truth is exact, so
survivals are a HARD failure when the corpus is present. Per-value ``waived``
(with a comment in the JSON) is the escape hatch for verified false positives.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset

ANSWER_KEY_NAME = "answer_key_header.json"

# Values shorter than this are skipped: 2-3 char tokens (sex codes, laterality,
# initials) collide with legitimate clinical values far too often to assert on.
MIN_VALUE_LEN = 4

# Binary VRs are the pixel pipeline's domain (burned-in PHI, M4/M5) — the
# HEADER sweep must not scan raw byte payloads (a name's ASCII happening to
# occur inside PixelData is not a header survival).
_BINARY_VRS = frozenset({"OB", "OW", "OD", "OF", "OL", "OV", "UN"})


def _norm(token: str) -> str:
    """NFKD + strip diacritics + upper + collapse whitespace — the same
    folding as ``deid.verify._norm`` so the corpus and the engine's own
    verification agree on what counts as 'the same string'."""
    folded = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.upper().split())


@dataclass(frozen=True)
class HeaderPhiValue:
    value: str
    category: str = "other"
    element: str | None = None
    waived: bool = False


@dataclass
class HeaderCase:
    """One corpus instance: raw bytes + the PHI values planted in its header."""

    path: Path
    dicom: bytes
    sop_instance_uid: str
    phi: list[HeaderPhiValue] = field(default_factory=list)
    # Original structural UIDs (linkage identifiers): must never survive.
    original_uids: tuple[str, ...] = ()


# Identifying attributes whose RAW values constitute the ground truth when the
# answer key is DERIVED from the corpus itself (the Pseudo-PHI collection
# plants its synthetic PHI in exactly these headers; TCIA distributes UID
# crosswalks, not a per-value key). Every PN element is additionally swept
# regardless of this list.
_DERIVE_ATTRS: tuple[tuple[str, str], ...] = (
    ("PatientID", "id"),
    ("OtherPatientIDs", "id"),
    ("AccessionNumber", "id"),
    ("StudyID", "id"),
    ("PatientAddress", "address"),
    ("RegionOfResidence", "address"),
    ("PatientTelephoneNumbers", "phone"),
    ("InstitutionName", "institution"),
    ("InstitutionAddress", "address"),
    ("PatientBirthDate", "date"),
    ("StudyDate", "date"),
    ("SeriesDate", "date"),
    ("AcquisitionDate", "date"),
    ("ContentDate", "date"),
    ("PatientMotherBirthName", "name"),
    ("MilitaryRank", "other"),
    ("EthnicGroup", "other"),
    ("Occupation", "other"),
    # NB: AdditionalPatientHistory is deliberately NOT ground truth — it is
    # clinical narrative ("RENAL CA"), and the same clinical content
    # legitimately survives in CLEANed descriptors (ReasonForStudy, ...).
)

# Canned non-identifying sentinel values that appear across many attributes
# (refusals, unknowns) — never ground truth.
_DERIVE_STOPLIST: frozenset[str] = frozenset(
    {"PATIENT REFUSED", "REFUSED", "UNKNOWN", "NONE", "OTHER", "DECLINED", "NOT SPECIFIED"}
)


def derive_case_phi(ds: Dataset) -> list[HeaderPhiValue]:
    """Ground truth derived from an instance's OWN raw header.

    Valid for this corpus by construction: the synthetic PHI *is* what sits in
    the identifying attributes. Every PN value (any element with VR PN,
    components split on ``^``) plus the curated attribute list above.
    """
    phi: list[HeaderPhiValue] = []
    seen: set[str] = set()

    def _add(value: str, category: str) -> None:
        text = value.strip()
        if len(text) < MIN_VALUE_LEN or text in seen or _norm(text) in _DERIVE_STOPLIST:
            return
        seen.add(text)
        phi.append(HeaderPhiValue(value=text, category=category))

    stack: list = [ds]
    while stack:
        node = stack.pop()
        for elem in node:
            if elem.VR == "SQ":
                stack.extend(elem.value or [])
            elif elem.VR == "PN" and elem.value is not None:
                values = elem.value if isinstance(elem.value, (list, tuple)) else [elem.value]
                for v in values:
                    text = str(v)
                    _add(text.replace("^", " "), "name")
                    for part in text.split("^"):
                        _add(part, "name")
    for keyword, category in _DERIVE_ATTRS:
        value = getattr(ds, keyword, None)
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            _add(str(v), category)
    return phi


def load_answer_key(root: Path) -> dict[str, list[HeaderPhiValue]]:
    path = root / ANSWER_KEY_NAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    out: dict[str, list[HeaderPhiValue]] = {}
    for sop_uid, values in data.items():
        out[str(sop_uid)] = [
            HeaderPhiValue(
                value=str(v.get("value", "")),
                category=str(v.get("category", "other")),
                element=v.get("element"),
                waived=bool(v.get("waived", False)),
            )
            for v in values
            if isinstance(v, dict) and str(v.get("value", "")).strip()
        ]
    return out


def load_header_corpus(root: str | Path) -> Iterator[HeaderCase]:
    """Yield every ``*.dcm`` under ``root`` with its answer-key PHI values.

    Returns nothing when the root does not exist (skip-if-absent, matching
    ``pixel_deid_eval.load_public_corpus``). Instances without an answer-key
    entry still yield (with empty ``phi``): the UID-remap and engine-success
    assertions apply to them regardless.
    """
    rootp = Path(root)
    if not rootp.exists():
        return
    key = load_answer_key(rootp)
    for path in sorted(rootp.rglob("*.dcm")):
        blob = path.read_bytes()
        try:
            ds = pydicom.dcmread(io.BytesIO(blob), stop_before_pixels=True, force=True)
        except Exception:
            continue
        sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "")
        uids = tuple(
            u
            for u in (
                str(getattr(ds, "StudyInstanceUID", "") or ""),
                str(getattr(ds, "SeriesInstanceUID", "") or ""),
                sop_uid,
                str(getattr(ds, "FrameOfReferenceUID", "") or ""),
            )
            if u
        )
        yield HeaderCase(
            path=path,
            dicom=blob,
            sop_instance_uid=sop_uid,
            phi=key.get(sop_uid, []),
            original_uids=uids,
        )


def sweep_values(ds: Dataset) -> Iterator[str]:
    """Every element value in ``ds`` stringified, recursing into sequences and
    the file meta — the haystack a surviving PHI value would have to hide in.
    PN components are additionally split on ``^`` so 'ROSSI^MARIO' also yields
    its parts."""
    metas = [m for m in (getattr(ds, "file_meta", None), ds) if m is not None]
    stack: list = metas
    while stack:
        node = stack.pop()
        for elem in node:
            if elem.VR == "SQ":
                stack.extend(elem.value or [])
                continue
            if elem.VR in _BINARY_VRS:
                continue
            value = elem.value
            if value is None:
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            for v in values:
                text = str(v).strip()
                if not text:
                    continue
                yield text
                if elem.VR == "PN" and "^" in text:
                    yield from (p for p in text.split("^") if p)


@dataclass
class HeaderCaseResult:
    survivals: list[HeaderPhiValue] = field(default_factory=list)
    uid_leaks: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.survivals and not self.uid_leaks


def score_header_case(out_bytes: bytes, case: HeaderCase) -> HeaderCaseResult:
    """Assert the scrubbed output against the case's ground truth.

    A PHI value survives when its normalized form appears TOKEN-BOUNDED in any
    normalized output value ("MARY" must not match inside "PRIMARY"; multi-word
    values still match across their own internal spaces). Structural UIDs must
    differ everywhere (the engine's KeyedHashUID remap) — an original UID
    anywhere in the output is a re-identification linkage leak.
    """
    ds = pydicom.dcmread(io.BytesIO(out_bytes), force=True)
    haystack = [_norm(v) for v in sweep_values(ds)]
    result = HeaderCaseResult()
    for phi in case.phi:
        if phi.waived:
            continue
        needle = _norm(phi.value)
        if len(needle) < MIN_VALUE_LEN:
            continue
        pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(needle)}(?![A-Z0-9])")
        if any(pattern.search(h) for h in haystack):
            result.survivals.append(phi)
    raw_haystack = list(sweep_values(ds))
    for uid in case.original_uids:
        if any(uid in h for h in raw_haystack):
            result.uid_leaks.append(uid)
    return result


__all__ = [
    "ANSWER_KEY_NAME",
    "MIN_VALUE_LEN",
    "HeaderCase",
    "HeaderCaseResult",
    "HeaderPhiValue",
    "load_answer_key",
    "load_header_corpus",
    "score_header_case",
    "sweep_values",
]
