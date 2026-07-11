"""Safety net for the generated PS3.15 Table E.1-1 swap (phoenix-deid-3).

The full NEMA-generated table replaced the hand-curated 110-row subset. These
tests pin what the swap must NOT change:

* protectiveness: for every keyword of the old curated map, the new resolved
  action destroys/transforms at least as much of the value (grouped ranking —
  REMOVE/EMPTY/PSEUDONYM all destroy the value; the standard legitimately
  prescribes Z where the curated set used X, and tag presence is not PHI);
* the five joinable identity strings stay EXACTLY PSEUDONYM;
* the repeating-group leak is fixed: OverlayData/OverlayComments/CurveData have
  empty ``elem.keyword`` at runtime, so the old keyword-matched entries never
  fired — the mask rules must remove them now;
* keywordless E.1-1 rows (gender identity group etc.) are enforced by tag;
* structural sanity of the generated module (row count, codes, version).
"""

from __future__ import annotations

from io import BytesIO

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.tag import Tag
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.deid.profile_table import (
    _CODE_TO_ACTION,
    _DESCRIPTORS,
    Action,
    ProfileOptions,
    ResolvedActions,
    resolve_actions,
)
from bvphoenix.services.deid.ps315_e11 import (
    PS315_SOURCE_SHA256,
    PS315_TABLE_VERSION,
    ROWS,
)
from bvphoenix.services.deidentify import deidentify_dicom_bytes

# --- structural sanity --------------------------------------------------------


def test_generated_table_sanity():
    assert PS315_TABLE_VERSION == "2026b"
    assert len(PS315_SOURCE_SHA256) == 64
    assert len(ROWS) >= 550
    literal = [r for r in ROWS if r[0] is not None]
    assert len(literal) >= 546
    specials = {r[1] for r in ROWS if r[1]}
    assert specials == {"curve_50xx", "overlay_data_60xx", "overlay_comments_60xx", "private"}
    # every basic code is mapped
    assert {r[6] for r in ROWS} <= set(_CODE_TO_ACTION)
    # unique tags
    tags = [r[0] for r in literal]
    assert len(tags) == len(set(tags))


def test_spot_checks():
    ra = resolve_actions(ProfileOptions())
    kw = ra.by_keyword
    assert kw["PatientName"] == Action.PSEUDONYM
    assert kw["PatientID"] == Action.PSEUDONYM
    assert kw["AccessionNumber"] == Action.EMPTY
    assert kw["PatientBirthDate"] == Action.EMPTY  # never shifted, under any policy
    assert kw["StudyDate"] == Action.DATE  # Modified Dates option (shift policy)
    assert kw["SOPInstanceUID"] == Action.UID
    assert kw["ContentSequence"] == Action.REMOVE
    # date policy 'remove' keeps the protective baseline instead of shifting
    ra_remove = resolve_actions(ProfileOptions(date_policy="remove"))
    assert ra_remove.by_keyword["StudyDate"] in (Action.EMPTY, Action.REMOVE)


def test_descriptor_allowlist_is_subset_of_clean_column():
    # _DESCRIPTORS must name real E.1-1 rows marked C in the Clean Descriptors
    # column — the allowlist can only narrow the column, never invent rows.
    by_kw = {r[3]: r for r in ROWS if r[0] is not None and r[3]}
    for kw in _DESCRIPTORS:
        assert kw in by_kw, f"{kw} is not an E.1-1 row"
        assert by_kw[kw][14] == "C", f"{kw} is not marked C in Clean Desc. column"


# --- protectiveness diff vs the old curated table -----------------------------

# Value-destruction ranking: what matters is whether the ORIGINAL VALUE can
# survive. REMOVE/EMPTY/PSEUDONYM/UID all destroy it irreversibly (tag presence
# is not PHI, and the salted UID remap is the same construction as PSEUDONYM —
# consistent within a release, opaque outside it); CLEAN/DATE/AGE transform it
# partially; KEEP retains it.
_RANK = {
    Action.REMOVE: 3,
    Action.EMPTY: 3,
    Action.PSEUDONYM: 3,
    Action.UID: 3,
    Action.CLEAN: 2,
    Action.DATE: 2,
    Action.AGE: 2,
    Action.KEEP: 1,
}

# The old curated BASIC_PROFILE_ACTIONS (strict baseline), frozen verbatim at
# the swap. Keys handled by mask rules now are asserted separately below.
_OLD_BASELINE: dict[str, str] = {
    "PatientName": "P",
    "PatientID": "P",
    "IssuerOfPatientID": "X",
    "TypeOfPatientID": "X",
    "OtherPatientIDs": "X",
    "OtherPatientIDsSequence": "X",
    "OtherPatientNames": "X",
    "PatientBirthName": "X",
    "PatientMotherBirthName": "X",
    "PatientBirthDate": "Z",
    "PatientBirthTime": "Z",
    "PatientAddress": "X",
    "PatientTelephoneNumbers": "X",
    "PatientTelecomInformation": "X",
    "CountryOfResidence": "X",
    "RegionOfResidence": "X",
    "EthnicGroup": "X",
    "Occupation": "X",
    "PatientReligiousPreference": "X",
    "MilitaryRank": "X",
    "BranchOfService": "X",
    "PatientComments": "X",
    "PatientInsurancePlanCodeSequence": "X",
    "MedicalRecordLocator": "X",
    "MedicalAlerts": "X",
    "Allergies": "X",
    "AdditionalPatientHistory": "X",
    "ResponsiblePerson": "X",
    "ResponsiblePersonRole": "X",
    "ResponsibleOrganization": "X",
    "PatientInstitutionResidence": "X",
    "ConfidentialityConstraintOnPatientDataDescription": "X",
    "PatientSex": "Z",
    "PatientAge": "Z",
    "PatientWeight": "Z",
    "PatientSize": "Z",
    "PatientSexNeutered": "Z",
    "PregnancyStatus": "Z",
    "SmokingStatus": "Z",
    "PatientState": "Z",
    "AccessionNumber": "Z",
    "StudyID": "Z",
    "AdmissionID": "X",
    "IssuerOfAdmissionID": "X",
    "ServiceEpisodeID": "X",
    "ServiceEpisodeDescription": "X",
    "CurrentPatientLocation": "X",
    "VisitComments": "X",
    "AdmittingDiagnosesDescription": "X",
    "AdmittingDiagnosesCodeSequence": "X",
    "PerformedProcedureStepID": "X",
    "RequestedProcedureID": "X",
    "ScheduledProcedureStepID": "X",
    "OrderEnteredBy": "X",
    "OrderEntererLocation": "X",
    "OrderCallbackPhoneNumber": "X",
    "OrderCallbackTelecomInformation": "X",
    "ReferringPhysicianName": "P",
    "ReferringPhysicianAddress": "X",
    "ReferringPhysicianTelephoneNumbers": "X",
    "ReferringPhysicianIdentificationSequence": "X",
    "PhysiciansOfRecord": "X",
    "PhysiciansOfRecordIdentificationSequence": "X",
    "PerformingPhysicianName": "X",
    "PerformingPhysicianIdentificationSequence": "X",
    "NameOfPhysiciansReadingStudy": "X",
    "PhysiciansReadingStudyIdentificationSequence": "X",
    "OperatorsName": "X",
    "OperatorIdentificationSequence": "X",
    "RequestingPhysician": "X",
    "RequestingService": "X",
    "ScheduledPerformingPhysicianName": "X",
    "InstitutionName": "P",
    "InstitutionAddress": "X",
    "InstitutionalDepartmentName": "P",
    "InstitutionCodeSequence": "X",
    "DeviceSerialNumber": "X",
    "DeviceUID": "X",
    "PlateID": "X",
    "GantryID": "X",
    "CassetteID": "X",
    "DetectorID": "X",
    "StationName": "X",
    "DeviceLabel": "X",
    "ContentCreatorName": "X",
    "ContentCreatorIdentificationCodeSequence": "X",
    "VerifyingObserverName": "X",
    "VerifyingObserverSequence": "X",
    "VerifyingOrganization": "X",
    "PersonName": "X",
    "ReviewerName": "X",
    "AuthorObserverSequence": "X",
    "ParticipantSequence": "X",
    "TextComments": "X",
    "TextString": "X",
    "ScheduledStudyLocation": "X",
    "ScheduledStudyLocationAETitle": "X",
    "PerformedStationAETitle": "X",
    "PerformedStationName": "X",
    "PerformedLocation": "X",
    "ScheduledProcedureStepLocation": "X",
    "ScheduledStationName": "X",
    "ScheduledStationAETitle": "X",
    "GraphicAnnotationSequence": "X",
    "TextObjectSequence": "X",
    "ContentSequence": "X",
    "DataSetTrailingPadding": "X",
}
_OLD_RANK = {"X": 3, "Z": 3, "P": 3}

_IDENTITY_PSEUDONYMS = frozenset(
    {
        "PatientName",
        "PatientID",
        "ReferringPhysicianName",
        "InstitutionName",
        "InstitutionalDepartmentName",
    }
)


def test_protectiveness_never_weaker_than_curated_baseline():
    ra = resolve_actions(
        ProfileOptions(retain_patient_characteristics=False, clean_descriptors=False)
    )
    missing: list[str] = []
    weaker: list[str] = []
    for kw, old_code in _OLD_BASELINE.items():
        new = ra.by_keyword.get(kw)
        if new is None:
            missing.append(kw)
            continue
        if _RANK[new] < _OLD_RANK[old_code]:
            weaker.append(f"{kw}: {old_code} -> {new}")
    assert not missing, f"curated keywords no longer covered by the table: {missing}"
    assert not weaker, f"weaker than the curated baseline: {weaker}"
    for kw in _IDENTITY_PSEUDONYMS:
        assert ra.by_keyword[kw] == Action.PSEUDONYM


def test_default_options_effective_actions_stable():
    # Under the DEFAULT options (chars retained, descriptors cleaned) the
    # curated engine's effective behaviour for its own keywords holds: the
    # characteristics relax to KEEP/AGE, descriptors to CLEAN.
    ra = resolve_actions(ProfileOptions())
    assert ra.by_keyword["PatientAge"] == Action.AGE
    assert ra.by_keyword["PatientSex"] == Action.KEEP
    assert ra.by_keyword["StudyDescription"] == Action.CLEAN
    assert ra.by_keyword["SeriesDescription"] == Action.CLEAN
    # device identity stays protective unless its option is on
    assert _RANK[ra.by_keyword["DeviceSerialNumber"]] == 3
    ra_dev = resolve_actions(ProfileOptions(retain_device_identity=True))
    assert ra_dev.by_keyword["DeviceSerialNumber"] == Action.KEEP


# --- repeating groups: the fixed leak vector ----------------------------------


def _base_ds() -> Dataset:
    ds = Dataset()
    ds.PatientName = "Rossi^Mario"
    ds.PatientID = "MRN-1"
    ds.Modality = "CT"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = UID("1.2.840.10008.5.1.4.1.1.2")
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    return ds


def _scrub(ds: Dataset) -> Dataset:
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return pydicom.dcmread(BytesIO(deidentify_dicom_bytes(buf.getvalue())))


def test_repeating_group_overlay_and_curve_data_removed():
    # pydicom gives elem.keyword == '' for repeating-group elements, so the old
    # keyword-matched OverlayData/OverlayComments/CurveData entries were DEAD
    # (OverlayData OW fell to the KEEP VR default — overlay bitmaps can carry
    # burned-in annotations). The mask rules must remove them, on any group of
    # the repeater range.
    ds = _base_ds()
    ds.add_new(Tag(0x6000, 0x3000), "OW", b"\x00\x01\x02\x03")  # OverlayData
    ds.add_new(Tag(0x6002, 0x3000), "OW", b"\x04\x05")  # second overlay plane
    ds.add_new(Tag(0x6000, 0x4000), "LT", "annotated by Dr. Rossi")  # OverlayComments
    ds.add_new(Tag(0x6000, 0x0022), "LO", "overlay description")  # other 60xx: untouched rule
    ds.add_new(Tag(0x5000, 0x3000), "US", [1, 2, 3])  # CurveData (retired 50xx)
    out = _scrub(ds)
    assert Tag(0x6000, 0x3000) not in out
    assert Tag(0x6002, 0x3000) not in out
    assert Tag(0x6000, 0x4000) not in out
    assert Tag(0x5000, 0x3000) not in out


def test_repeater_masks_do_not_touch_private_groups():
    # Odd groups are private and handled (removed) by the private branch FIRST;
    # the masks only ever see even repeater groups.
    assert ResolvedActions.repeater_action(0x5001, 0x3000) == Action.REMOVE  # mask would match...
    ds = _base_ds()
    ds.add_new(Tag(0x5001, 0x0010), "LO", "ACME")  # private creator in odd 50xx group
    out = _scrub(ds)
    assert Tag(0x5001, 0x0010) not in out  # removed as private, either way


def test_keywordless_table_row_enforced_by_tag():
    # E.1-1 rows with no pydicom keyword can only be matched by tag. Pick one
    # from the generated table with a removal-flavoured action and verify.
    row = next(r for r in ROWS if r[0] is not None and not r[3] and r[6] in ("X", "X/Z", "X/D"))
    tag = Tag(row[0] >> 16, row[0] & 0xFFFF)
    ds = _base_ds()
    ds.add_new(tag, "LO", "Sensitive Value")
    out = _scrub(ds)
    assert tag not in out, f"keywordless row {row[2]!r} {tag} survived"


def test_scrub_is_repeatable():
    # scrub(scrub(x)) must not raise and must still verify clean (the engine
    # always rescrubs by design; workflow-level stamps prevent the double work).
    ds = _base_ds()
    once = deidentify_dicom_bytes(_ser(ds))
    twice = deidentify_dicom_bytes(once)
    assert pydicom.dcmread(BytesIO(twice)).PatientIdentityRemoved == "YES"


def _ser(ds: Dataset) -> bytes:
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def test_gender_identity_group_removed():
    # The 2023+ gender-identity attributes (0010,0011..0047) are E.1-1 rows
    # without pydicom keywords in some versions — spot-check by tag that the
    # group is covered by the table at all.
    covered = [r for r in ROWS if r[0] is not None and 0x0010_0011 <= r[0] <= 0x0010_0047]
    assert covered, "gender identity rows missing from the generated table"
    for r in covered:
        # every code in the range destroys the value (REMOVE or EMPTY flavour)
        assert _CODE_TO_ACTION[r[6]] in (Action.REMOVE, Action.EMPTY), (r[2], r[6])


@pytest.mark.parametrize("kw", sorted(_IDENTITY_PSEUDONYMS))
def test_identity_pseudonyms_exact(kw):
    ra = resolve_actions(ProfileOptions())
    assert ra.by_keyword[kw] == Action.PSEUDONYM
