"""DICOM PS3.15 Annex E Basic Application Confidentiality Profile — attribute table.

The engine is **table-driven**: ``BASIC_PROFILE_ACTIONS`` maps the direct
identifiers of PS3.15 Table E.1-1 to an :class:`Action`, and the executor
(:mod:`bvphoenix.services.deid.actions`) applies categorical **VR-based rules**
for everything else (all UID-VR elements remapped, all date/time-VR elements
shifted, all private tags removed unless safe-listed). Together these cover the
profile without needing to enumerate all ~400 rows by hand.

This table is a curated, high-coverage subset authored from the published DICOM
PS3.15 standard (NEMA). It is deliberately the **strict baseline** (most
protective action per attribute); the named PS3.15 *Options* only ever RELAX it
(retain more), via :func:`resolve_actions`. A subset of the published profile.
TODO(M3+): regenerate the full table from the NEMA part15 DocBook XML and diff
against this curated set; the golden corpus (TCIA Pseudo-PHI) is the safety net.

Values are pydicom keywords (not raw tags) for readability; the executor falls
back to VR rules for elements whose keyword is absent here or empty (unknown
public / private tags).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
    UID = "U"  # consistent UID remap (applied by VR rule; here for completeness)
    DATE = "DATE"  # date/time shift (applied by VR rule)
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
    method_version: str = "phoenix-deid-2"
    safe_private_version: str = "v1"


# UID-VR elements that are STRUCTURAL (class / transfer-syntax / implementation)
# and must be KEPT verbatim — remapping them would corrupt the object. Every
# other UID-VR element (instance/series/study/frame-of-reference/referenced
# instance/concatenation/irradiation-event/...) is consistently remapped by the
# VR rule so linkage is broken across releases but preserved within one.
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

# Patient characteristics: quasi-identifiers that are clinically useful. Strict
# baseline empties them; the Retain Patient Characteristics Option keeps them
# (PatientAge capped to the 090Y+ band above 89).
_PATIENT_CHARACTERISTICS: frozenset[str] = frozenset(
    {
        "PatientSex",
        "PatientAge",
        "PatientWeight",
        "PatientSize",
        "PatientSexNeutered",
        "PregnancyStatus",
        "SmokingStatus",
        "PatientState",
    }
)

# Device identity: serial numbers / station identifiers. Strict baseline removes
# them; the Retain Device Identity Option keeps them.
_DEVICE_IDENTITY: frozenset[str] = frozenset(
    {
        "DeviceSerialNumber",
        "DeviceUID",
        "PlateID",
        "GantryID",
        "CassetteID",
        "DetectorID",
        "StationName",
        "DeviceLabel",
    }
)

# Free-text descriptors: kept by default, scrubbed (CLEAN) when the Clean
# Descriptors Option is on. They routinely carry incidental PHI ("MR BRAIN
# MARIO ROSSI").
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
        "ReasonForRequestedProcedure",
    }
)

# Strict baseline: the most-protective action per direct identifier.
# Identity strings we keep joinable -> PSEUDONYM; order numbers -> EMPTY;
# everything else identifying -> REMOVE. Characteristics/device default to the
# protective action and are relaxed by options. (Subset of PS3.15 Table E.1-1.)
BASIC_PROFILE_ACTIONS: dict[str, Action] = {
    # --- Patient identity ---
    "PatientName": Action.PSEUDONYM,
    "PatientID": Action.PSEUDONYM,
    "IssuerOfPatientID": Action.REMOVE,
    "TypeOfPatientID": Action.REMOVE,
    "OtherPatientIDs": Action.REMOVE,
    "OtherPatientIDsSequence": Action.REMOVE,
    "OtherPatientNames": Action.REMOVE,
    "PatientBirthName": Action.REMOVE,
    "PatientMotherBirthName": Action.REMOVE,
    "PatientBirthDate": Action.EMPTY,
    "PatientBirthTime": Action.EMPTY,
    "PatientAddress": Action.REMOVE,
    "PatientTelephoneNumbers": Action.REMOVE,
    "PatientTelecomInformation": Action.REMOVE,
    "CountryOfResidence": Action.REMOVE,
    "RegionOfResidence": Action.REMOVE,
    "EthnicGroup": Action.REMOVE,
    "Occupation": Action.REMOVE,
    "PatientReligiousPreference": Action.REMOVE,
    "MilitaryRank": Action.REMOVE,
    "BranchOfService": Action.REMOVE,
    "PatientComments": Action.REMOVE,
    "PatientInsurancePlanCodeSequence": Action.REMOVE,
    "MedicalRecordLocator": Action.REMOVE,
    "MedicalAlerts": Action.REMOVE,
    "Allergies": Action.REMOVE,
    "AdditionalPatientHistory": Action.REMOVE,
    "ResponsiblePerson": Action.REMOVE,
    "ResponsiblePersonRole": Action.REMOVE,
    "ResponsibleOrganization": Action.REMOVE,
    "PatientInstitutionResidence": Action.REMOVE,
    "ConfidentialityConstraintOnPatientDataDescription": Action.REMOVE,
    # Characteristics (relaxed to KEEP/AGE by retain_patient_characteristics).
    "PatientSex": Action.EMPTY,
    "PatientAge": Action.EMPTY,
    "PatientWeight": Action.EMPTY,
    "PatientSize": Action.EMPTY,
    "PatientSexNeutered": Action.EMPTY,
    "PregnancyStatus": Action.EMPTY,
    "SmokingStatus": Action.EMPTY,
    "PatientState": Action.EMPTY,
    # --- Order / visit identifiers ---
    "AccessionNumber": Action.EMPTY,
    "StudyID": Action.EMPTY,
    "AdmissionID": Action.REMOVE,
    "IssuerOfAdmissionID": Action.REMOVE,
    "ServiceEpisodeID": Action.REMOVE,
    "ServiceEpisodeDescription": Action.REMOVE,
    "CurrentPatientLocation": Action.REMOVE,
    "VisitComments": Action.REMOVE,
    "AdmittingDiagnosesDescription": Action.REMOVE,
    "AdmittingDiagnosesCodeSequence": Action.REMOVE,
    "PerformedProcedureStepID": Action.REMOVE,
    "RequestedProcedureID": Action.REMOVE,
    "ScheduledProcedureStepID": Action.REMOVE,
    "OrderEnteredBy": Action.REMOVE,
    "OrderEntererLocation": Action.REMOVE,
    "OrderCallbackPhoneNumber": Action.REMOVE,
    "OrderCallbackTelecomInformation": Action.REMOVE,
    # --- Staff identity (institution/physician/operator) ---
    "ReferringPhysicianName": Action.PSEUDONYM,
    "ReferringPhysicianAddress": Action.REMOVE,
    "ReferringPhysicianTelephoneNumbers": Action.REMOVE,
    "ReferringPhysicianIdentificationSequence": Action.REMOVE,
    "PhysiciansOfRecord": Action.REMOVE,
    "PhysiciansOfRecordIdentificationSequence": Action.REMOVE,
    "PerformingPhysicianName": Action.REMOVE,
    "PerformingPhysicianIdentificationSequence": Action.REMOVE,
    "NameOfPhysiciansReadingStudy": Action.REMOVE,
    "PhysiciansReadingStudyIdentificationSequence": Action.REMOVE,
    "OperatorsName": Action.REMOVE,
    "OperatorIdentificationSequence": Action.REMOVE,
    "RequestingPhysician": Action.REMOVE,
    "RequestingService": Action.REMOVE,
    "ScheduledPerformingPhysicianName": Action.REMOVE,
    "InstitutionName": Action.PSEUDONYM,
    "InstitutionAddress": Action.REMOVE,
    "InstitutionalDepartmentName": Action.PSEUDONYM,
    "InstitutionCodeSequence": Action.REMOVE,
    # Device identity (relaxed to KEEP by retain_device_identity).
    "DeviceSerialNumber": Action.REMOVE,
    "DeviceUID": Action.REMOVE,
    "PlateID": Action.REMOVE,
    "GantryID": Action.REMOVE,
    "CassetteID": Action.REMOVE,
    "DetectorID": Action.REMOVE,
    "StationName": Action.REMOVE,
    "DeviceLabel": Action.REMOVE,
    # --- SR / content authoring identity (free-text PHI carriers) ---
    "ContentCreatorName": Action.REMOVE,
    "ContentCreatorIdentificationCodeSequence": Action.REMOVE,
    "VerifyingObserverName": Action.REMOVE,
    "VerifyingObserverSequence": Action.REMOVE,
    "VerifyingOrganization": Action.REMOVE,
    "PersonName": Action.REMOVE,
    "ReviewerName": Action.REMOVE,
    "AuthorObserverSequence": Action.REMOVE,
    "ParticipantSequence": Action.REMOVE,
    "TextComments": Action.REMOVE,
    "TextString": Action.REMOVE,
    # --- Locations / scheduling ---
    "ScheduledStudyLocation": Action.REMOVE,
    "ScheduledStudyLocationAETitle": Action.REMOVE,
    "PerformedStationAETitle": Action.REMOVE,
    "PerformedStationName": Action.REMOVE,
    "PerformedLocation": Action.REMOVE,
    "ScheduledProcedureStepLocation": Action.REMOVE,
    "ScheduledStationName": Action.REMOVE,
    "ScheduledStationAETitle": Action.REMOVE,
    # --- Data-bearing elements that can hide PHI / annotations ---
    "OverlayData": Action.REMOVE,
    "OverlayComments": Action.REMOVE,
    "CurveData": Action.REMOVE,
    "GraphicAnnotationSequence": Action.REMOVE,
    "TextObjectSequence": Action.REMOVE,
    "ContentSequence": Action.REMOVE,  # SR content tree — header engine cannot
    # guarantee its free text is clean; remove on the egress copy (the full
    # Clean Structured Content option is future work; verify.py also routes SRs
    # to review).
    "DataSetTrailingPadding": Action.REMOVE,
}


def resolve_actions(options: ProfileOptions) -> dict[str, Action]:
    """Build the effective keyword→action map for the given options.

    Starts from the strict baseline and RELAXES per enabled option (PS3.15
    options only ever retain more). Memoise at the call site if hot.
    """
    actions = dict(BASIC_PROFILE_ACTIONS)
    if options.retain_patient_characteristics:
        for kw in _PATIENT_CHARACTERISTICS:
            actions[kw] = Action.AGE if kw == "PatientAge" else Action.KEEP
    if options.retain_device_identity:
        for kw in _DEVICE_IDENTITY:
            actions[kw] = Action.KEEP
    if options.clean_descriptors:
        for kw in _DESCRIPTORS:
            actions[kw] = Action.CLEAN
    return actions


__all__ = [
    "BASIC_PROFILE_ACTIONS",
    "KEEP_UID_KEYWORDS",
    "Action",
    "ProfileOptions",
    "resolve_actions",
]
