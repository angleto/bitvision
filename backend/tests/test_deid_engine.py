"""Adversarial + property tests for the in-house PS3.15 de-identification engine.

These are the safety net that de-risks the table-driven engine: UID consistency
+ salt-dependence, per-patient date shifting with interval preservation, the
HIPAA age cap, idempotency (the reindex worker relies on it), private-tag
removal, nested-sequence recursion, SR-routes-to-review, and the key adversarial
case — PHI that leaks into an attribute the table does not cover must be caught
by the fail-closed verification pass, never served.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.tag import Tag
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.deid.errors import DeidVerificationError, RequiresReview
from bvphoenix.services.deid.operators import DeidOperators
from bvphoenix.services.deidentify import deidentify_dicom_bytes

_CT = "1.2.840.10008.5.1.4.1.1.2"
_BASIC_TEXT_SR = "1.2.840.10008.5.1.4.1.1.88.11"


def _make(**attrs: object) -> Dataset:
    ds = Dataset()
    ds.PatientName = attrs.pop("PatientName", "Rossi^Mario")
    ds.PatientID = attrs.pop("PatientID", "MRN-0001")
    ds.StudyInstanceUID = attrs.pop("StudyInstanceUID", generate_uid())
    ds.SeriesInstanceUID = attrs.pop("SeriesInstanceUID", generate_uid())
    ds.SOPInstanceUID = attrs.pop("SOPInstanceUID", generate_uid())
    ds.SOPClassUID = UID(str(attrs.pop("SOPClassUID", _CT)))
    ds.Modality = attrs.pop("Modality", "CT")
    for k, v in attrs.items():
        setattr(ds, k, v)
    return ds


def _ser(ds: Dataset) -> bytes:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def _scrub_read(ds: Dataset) -> Dataset:
    return pydicom.dcmread(BytesIO(deidentify_dicom_bytes(_ser(ds))))


# --- UID handling -----------------------------------------------------------


def test_study_uid_remapped_and_consistent_within_study():
    su = generate_uid()
    d1 = _scrub_read(_make(StudyInstanceUID=su))
    d2 = _scrub_read(_make(StudyInstanceUID=su, SOPInstanceUID=generate_uid()))
    assert str(d1.StudyInstanceUID) != su  # remapped
    assert str(d1.StudyInstanceUID) == str(d2.StudyInstanceUID)  # consistent within release


def test_sop_class_uid_kept_instance_uid_remapped_and_filemeta_synced():
    ds = _make()
    orig_class, orig_inst = str(ds.SOPClassUID), str(ds.SOPInstanceUID)
    out = _scrub_read(ds)
    assert str(out.SOPClassUID) == orig_class  # structural UID kept
    assert str(out.SOPInstanceUID) != orig_inst  # instance UID remapped
    assert str(out.file_meta.MediaStorageSOPInstanceUID) == str(out.SOPInstanceUID)


# --- dates ------------------------------------------------------------------


def test_dates_shifted_preserving_interval_and_birthdate_emptied():
    ds = _make(StudyDate="20200101", SeriesDate="20200201", PatientBirthDate="19500515")
    out = _scrub_read(ds)
    sd = datetime.strptime(str(out.StudyDate), "%Y%m%d").date()
    se = datetime.strptime(str(out.SeriesDate), "%Y%m%d").date()
    assert (se - sd).days == 31  # interval preserved
    assert str(out.StudyDate) != "20200101"  # absolute date shifted
    assert str(out.get("PatientBirthDate", "")) == ""  # birth date emptied


def test_age_capped_over_89():
    out = _scrub_read(_make(PatientAge="095Y"))
    assert str(out.PatientAge) == "090Y"


# --- pseudonyms + idempotency ----------------------------------------------


def test_patient_name_id_pseudonymized():
    out = _scrub_read(_make(PatientName="Rossi^Mario", PatientID="MRN-9"))
    assert str(out.PatientName).startswith("ANON")
    assert str(out.PatientID).startswith("ANON")
    assert out.PatientIdentityRemoved == "YES"


def test_forged_deid_markers_do_not_bypass_scrub():
    # An attacker pre-sets the de-id provenance tags on a file that still holds
    # real PHI. The engine must NOT trust them (no content-based short-circuit) —
    # it fully scrubs anyway. Regression for the removed forgeable idempotency
    # guard (idempotency is a DB-stamp property of the workflow, not the file).
    ds = _make(PatientName="Real^Patient", PatientID="MRN-REAL-0001")
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = "bitvision phoenix PS3.15 Basic Profile (phoenix-deid-2)"
    out = _scrub_read(ds)
    assert "Real" not in str(out.PatientName)
    assert str(out.PatientName).startswith("ANON")
    assert "REAL" not in str(out.PatientID).upper()


def test_untabled_person_name_is_removed():
    # A PN-VR tag absent from the full E.1-1 table (EvaluatorName, NDE domain)
    # must be removed by the deny-by-default VR rule, not kept.
    ds = _make()
    ds.EvaluatorName = "Bianchi^Luca"
    out = _scrub_read(ds)
    assert "EvaluatorName" not in out


def test_tabled_person_name_z_is_emptied():
    # ConsultingPhysicianName is an E.1-1 row with action Z: the tag survives
    # (Type 2 conformance) with an EMPTY value — never the original name.
    ds = _make()
    ds.ConsultingPhysicianName = "Bianchi^Luca"
    out = _scrub_read(ds)
    assert "ConsultingPhysicianName" in out
    assert str(out.ConsultingPhysicianName) == ""


def test_file_meta_ae_title_scrubbed():
    raw = pydicom.dcmread(BytesIO(_ser(_make())))
    raw.file_meta.SourceApplicationEntityTitle = "SITE_WS_01"
    buf = BytesIO()
    raw.save_as(buf, write_like_original=False)
    out = pydicom.dcmread(BytesIO(deidentify_dicom_bytes(buf.getvalue())))
    assert "SourceApplicationEntityTitle" not in out.file_meta


def test_cap_age_units():
    op = DeidOperators(salt="s", org_root_uid="1.2.3")
    assert op.cap_age("095Y") == "090Y"  # years over 89 → capped
    assert op.cap_age("085Y") == "085Y"  # under the cap, unchanged
    # M/W/D can't exceed 89y in 3 digits (999M≈83y), so they never cap — but the
    # generalised conversion must not corrupt a valid sub-cap month value.
    assert op.cap_age("011M") == "011M"


# --- private / nested / adversarial ----------------------------------------


def test_private_tags_removed():
    ds = _make()
    ds.add_new(Tag(0x0009, 0x0010), "LO", "ACME")  # private creator
    ds.add_new(Tag(0x0009, 0x1001), "LO", "secret-serial")  # private element
    out = _scrub_read(ds)
    assert Tag(0x0009, 0x0010) not in out
    assert Tag(0x0009, 0x1001) not in out


def test_nested_sequence_phi_scrubbed():
    # ReferencedImageSequence is a KEPT sequence (E.1-1 X/Z/U*: retain, remap
    # nested UIDs): the recursion must scrub PHI inside its items.
    ds = _make()
    item = Dataset()
    item.PatientID = "MRN-NESTED"
    item.PatientName = "Nested^Person"
    item.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    item.ReferencedSOPInstanceUID = "1.2.3.4.5.6.7.8"
    ds.ReferencedImageSequence = [item]
    out = _scrub_read(ds)
    nested = out.ReferencedImageSequence[0]
    assert str(nested.PatientID).startswith("ANON")  # recursion pseudonymised it
    # nested instance UID remapped, class UID (structural) kept
    assert str(nested.ReferencedSOPInstanceUID) != "1.2.3.4.5.6.7.8"
    assert str(nested.ReferencedSOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"


def test_request_attributes_sequence_removed():
    # E.1-1 marks RequestAttributesSequence X: the whole sequence goes (the
    # curated engine used to keep+recurse it; the full table is stricter).
    ds = _make()
    item = Dataset()
    item.RequestedProcedureID = "RP-1"
    ds.RequestAttributesSequence = [item]
    out = _scrub_read(ds)
    assert "RequestAttributesSequence" not in out


def test_verify_catches_phi_leaked_into_untabled_tag():
    # A distinctive patient-name token copied into a kept (un-tabled) text tag
    # must trip the fail-closed verifier rather than be served.
    ds = _make(PatientName="Zorblax^Quux")
    ds.WindowCenterWidthExplanation = "Zorblax reference window"
    with pytest.raises(DeidVerificationError):
        deidentify_dicom_bytes(_ser(ds))


def test_sr_routed_to_review():
    ds = _make(Modality="SR", SOPClassUID=_BASIC_TEXT_SR)
    with pytest.raises(RequiresReview):
        deidentify_dicom_bytes(_ser(ds))


# --- operator-level properties (pure, no settings) --------------------------


def test_remap_uid_salt_dependent_and_consistent():
    a = DeidOperators(salt="s1", org_root_uid="1.2.3")
    b = DeidOperators(salt="s2", org_root_uid="1.2.3")
    u = "1.2.840.111.222.333"
    assert a.remap_uid(u) == a.remap_uid(u)  # deterministic
    assert a.remap_uid(u) != b.remap_uid(u)  # salt-dependent → cross-deployment unlinkable
    assert a.remap_uid(u).startswith("1.2.3.")  # under org root
    assert len(a.remap_uid(u)) <= 64  # valid UID length


def test_date_offset_per_patient_in_bounds():
    p1 = DeidOperators(salt="s", org_root_uid="1.2.3", patient_key="patient-A", date_policy="shift")
    p2 = DeidOperators(salt="s", org_root_uid="1.2.3", patient_key="patient-B", date_policy="shift")
    assert p1.offset_days != 0
    assert 365 <= abs(p1.offset_days) <= 1095
    assert p1.offset_days != p2.offset_days  # distinct patients → distinct offsets


def test_remove_date_policy_empties_dates():
    op = DeidOperators(salt="s", org_root_uid="1.2.3", patient_key="A", date_policy="remove")
    assert op.shift_date("20200101", "DA") == ""
