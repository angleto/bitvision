"""DICOM PS3.15 Annex E Basic Application Confidentiality Profile — policy layer.

The engine is **table-driven** over the FULL published Table E.1-1
(:mod:`ps315_e11`, generated from the NEMA part15 DocBook XML, edition pinned
+ sha256-recorded). This module is the POLICY on top of that data:

* raw standard action codes -> engine :class:`Action` (most-protective member
  of every combo code, so "X unless required for conformance" flavours never
  weaken the scrub);
* the project's deliberate flavours (:data:`_ENGINE_OVERRIDES` — consistent
  PSEUDONYM for the joinable identity strings, SR ContentSequence removal);
* PS3.15 *Options* as column-driven relaxations that only ever RETAIN MORE
  (Retain Patient Characteristics / Device Identity / Longitudinal Modified
  Dates); Clean Descriptors keeps the curated allowlist (the full column would
  retain-and-clean attributes the baseline removes = weaker).

Matching is **by tag, not keyword**: pydicom's ``elem.keyword`` is ``''`` for
every repeating-group element and ~24 E.1-1 rows have no pydicom keyword at
all — keyword matching silently dropped OverlayData/CurveData coverage (a real
leak vector, fixed by the tag/mask lookup + :data:`REPEATER_RULES`).

The executor (:mod:`actions`) still applies categorical **VR-based defaults**
for anything not in the table (all UID-VR remapped, all date/time-VR shifted,
un-tabled PN removed, free-narrative text cleaned) — PS3.15 Note 4's guidance
for Standard-Extended attributes the table cannot enumerate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from bvphoenix.services.deid.ps315_e11 import PS315_TABLE_VERSION, ROWS

__all__ = [
    "KEEP_UID_KEYWORDS",
    "PS315_TABLE_VERSION",
    "Action",
    "ProfileOptions",
    "ResolvedActions",
    "resolve_actions",
]


class Action(StrEnum):
    """PS3.15 action codes, plus our two flavours of replacement.

    Standard codes D/Z/X/K/C/U map here; ``PSEUDONYM`` is our consistent-hash
    flavour of D for identity strings we keep joinable, and ``AGE`` is the
    HIPAA >89 age-band cap applied when ages are retained.
    """

    REMOVE = "X"  # delete the element entirely
    EMPTY = "Z"  # keep the tag, zero-length value
    KEEP = "K"  # leave as-is
    PSEUDONYM = "P"  # consistent salted hash (identity strings: name/id/institution)
    UID = "U"  # consistent UID remap (also applied by VR rule)
    DATE = "DATE"  # date/time shift (also applied by VR rule)
    CLEAN = "C"  # free-text PHI scrub, retain the descriptor
    AGE = "AGE"  # retain age but cap to 090Y+ above 89 (HIPAA Safe Harbor)


@dataclass(frozen=True)
class ProfileOptions:
    """Resolved PS3.15 profile options (built from config in ``deid.options``)."""

    date_policy: str = "shift"  # "shift" (Retain Longitudinal, Modified Dates) | "remove"
    clean_descriptors: bool = True
    retain_patient_characteristics: bool = True
    retain_device_identity: bool = False
    retain_safe_private: bool = False
    method_version: str = "phoenix-deid-3"
    safe_private_version: str = "v1"


# UID-VR elements that are STRUCTURAL (class / transfer-syntax / implementation)
# and must be KEPT verbatim — remapping them would corrupt the object. They are
# not E.1-1 rows (they identify the encoding, not the patient); the VR rule
# consults this set for every un-tabled UID element.
KEEP_UID_KEYWORDS: frozenset[str] = frozenset(
    {
        "SOPClassUID",
        "MediaStorageSOPClassUID",
        "TransferSyntaxUID",
        "ImplementationClassUID",
        "ReferencedSOPClassUID",
        "RequestedSOPClassUID",
        "PrivateInformationCreatorUID",  # identifies the encoding, not the patient
    }
)

# Raw standard code -> engine action, most-protective member of each combo.
# "D" (replace with a dummy) maps to EMPTY: our dummy IS the empty value, and
# identity strings that must stay joinable are overridden to PSEUDONYM below.
# "X/Z/U*" marks sequences of referenced instances whose UIDs must be remapped
# for referential integrity: KEEP + recurse — the executor's VR rule remaps
# every nested UID (the standard's U reading), and removal would break
# legitimate intra-release references.
_CODE_TO_ACTION: dict[str, Action] = {
    "X": Action.REMOVE,
    "Z": Action.EMPTY,
    "D": Action.EMPTY,
    "U": Action.UID,
    "K": Action.KEEP,
    "C": Action.CLEAN,
    "X/Z": Action.REMOVE,
    "X/D": Action.REMOVE,
    "X/Z/D": Action.REMOVE,
    "Z/D": Action.EMPTY,
    "X/D/U": Action.UID,
    "K/C": Action.CLEAN,
    "X/Z/U*": Action.KEEP,
}

# The project's deliberate flavours, applied LAST (they win over any option
# relaxation). Keyed by pydicom keyword for readability; resolved to tags at
# import. Byte-for-byte compatible with the curated table this replaced.
_ENGINE_OVERRIDES: dict[str, Action] = {
    # Identity strings kept joinable across a release via the salted hash.
    "PatientName": Action.PSEUDONYM,
    "PatientID": Action.PSEUDONYM,
    "ReferringPhysicianName": Action.PSEUDONYM,
    "InstitutionName": Action.PSEUDONYM,
    "InstitutionalDepartmentName": Action.PSEUDONYM,
    # Kept-but-emptied order identifiers (standard Z; explicit for clarity).
    "AccessionNumber": Action.EMPTY,
    "StudyID": Action.EMPTY,
    # Birth date/time never shift (quasi-identifier): emptied under EVERY
    # date policy. (Their Modified-Dates column is empty in the standard too.)
    "PatientBirthDate": Action.EMPTY,
    "PatientBirthTime": Action.EMPTY,
    # SR content tree: the header engine cannot guarantee its free text is
    # clean (Clean Structured Content is unimplemented); remove on the egress
    # copy — verify.py also routes SRs to review.
    "ContentSequence": Action.REMOVE,
    "DataSetTrailingPadding": Action.REMOVE,
    # Curated-era coverage BEYOND the published table (not E.1-1 rows, but the
    # previous engine removed them and we never weaken): patient-ID typing
    # metadata, the responsible person's role, and the presentation-state text
    # container (its parent GraphicAnnotationSequence IS a row).
    "TypeOfPatientID": Action.REMOVE,
    "ResponsiblePersonRole": Action.REMOVE,
    "TextObjectSequence": Action.REMOVE,
}

# Free-text descriptors: kept by default, scrubbed (CLEAN) when the Clean
# Descriptors Option is on. Deliberately a curated ALLOWLIST rather than the
# full Clean-Descriptors column: the column marks ~330 rows whose baseline
# action already removes them — column-driven cleaning would retain-and-clean
# what the baseline removes, i.e. weaken the scrub. A consistency test asserts
# the allowlist is a subset of the column.
_DESCRIPTORS: frozenset[str] = frozenset(
    {
        "StudyDescription",
        "SeriesDescription",
        "ImageComments",
        "DerivationDescription",
        "AcquisitionDeviceProcessingDescription",
        "FrameComments",
        "ContrastBolusAgent",
        "ProtocolName",
        "PerformedProcedureStepDescription",
        "RequestedProcedureDescription",
        "ReasonForStudy",
        # NB: pydicom's keyword for (0040,1002) includes the article.
        "ReasonForTheRequestedProcedure",
    }
)

# Patient characteristics the Retain Patient Characteristics Option may keep.
# The standard's column marks MORE rows K (EthnicGroup, ...) — but this option
# is ON by default here, and a default-on option must never retain more than
# the engine it replaced did. Curated allowlist ∩ column K; a consistency test
# asserts every entry really is a K row.
_PATIENT_CHARACTERISTICS: frozenset[str] = frozenset(
    {
        "PatientSex",
        "PatientAge",
        "PatientWeight",
        "PatientSize",
        "PatientSexNeutered",
        "PregnancyStatus",
        "SmokingStatus",
    }
)

# Repeating-group rules (the E.1-1 rows with non-literal tags): curve data
# (50xx,xxxx) and overlay data/comments (60xx,3000)/(60xx,4000) can carry
# burned-in annotations and have NO pydicom keyword at runtime — they are
# matched by mask in the executor. Only even groups reach these (odd = private,
# handled first).
_PATIENT_AGE_TAG = 0x0010_1010


def _keyword_tags() -> dict[str, int]:
    return {r[3]: r[0] for r in ROWS if r[0] is not None and r[3]}


@dataclass(frozen=True)
class ResolvedActions:
    """The effective action map for one options set.

    ``by_tag`` covers every literal E.1-1 row; :meth:`repeater_action` covers
    the masked repeating groups; everything else falls to the executor's VR
    defaults."""

    by_tag: dict[int, Action]
    table_version: str = PS315_TABLE_VERSION
    # Keyword view for tests/diagnostics (keywordless rows excluded).
    by_keyword: dict[str, Action] = field(default_factory=dict)

    @staticmethod
    def repeater_action(group: int, element: int) -> Action | None:
        if group & 0xFF00 == 0x5000:
            return Action.REMOVE  # curve data (50xx,xxxx)
        if group & 0xFF00 == 0x6000 and element in (0x3000, 0x4000):
            return Action.REMOVE  # overlay data / overlay comments
        return None


def resolve_actions(options: ProfileOptions) -> ResolvedActions:
    """Build the effective tag→action map for the given options.

    Starts from the full-table strict baseline and RELAXES per enabled option
    (PS3.15 options only ever retain more); the engine overrides win last.
    Memoise at the call site if hot.
    """
    by_tag: dict[int, Action] = {}
    for tag, _special, _name, kw, _retired, _in_iod, basic, *cols in ROWS:
        if tag is None:
            continue
        by_tag[tag] = _CODE_TO_ACTION[basic]
        (_safe_priv, _uids, dev_id, _inst_id, pat_chars, _full_dates, mod_dates, *_rest) = cols
        # Default-ON option: allowlist ∩ column (never wider than the curated
        # engine). Default-OFF option (device): column-driven, the standard's
        # own definition applies when the operator explicitly enables it.
        if (
            options.retain_patient_characteristics
            and pat_chars == "K"
            and kw in _PATIENT_CHARACTERISTICS
        ) or (options.retain_patient_characteristics and tag == _PATIENT_AGE_TAG):
            by_tag[tag] = Action.AGE if tag == _PATIENT_AGE_TAG else Action.KEEP
        if options.retain_device_identity and dev_id == "K":
            by_tag[tag] = Action.KEEP
        if options.date_policy == "shift" and mod_dates == "C":
            by_tag[tag] = Action.DATE

    # Resolve keyword-named policy sets to tags via the DICOM dictionary, not
    # just the table rows: a few overrides deliberately cover attributes the
    # published table does not list.
    from pydicom.datadict import tag_for_keyword

    kw_tags = _keyword_tags()

    def _tag_of(kw: str) -> int | None:
        return kw_tags.get(kw) or tag_for_keyword(kw)

    if options.clean_descriptors:
        for kw in _DESCRIPTORS:
            tag = _tag_of(kw)
            if tag is not None:
                by_tag[tag] = Action.CLEAN
    for kw, action in _ENGINE_OVERRIDES.items():
        tag = _tag_of(kw)
        if tag is not None:
            by_tag[tag] = action

    by_keyword = {r[3]: by_tag[r[0]] for r in ROWS if r[0] is not None and r[3]}
    for kw in (*_DESCRIPTORS, *_ENGINE_OVERRIDES):
        tag = _tag_of(kw)
        if tag is not None and tag in by_tag:
            by_keyword[kw] = by_tag[tag]
    return ResolvedActions(by_tag=by_tag, by_keyword=by_keyword)
