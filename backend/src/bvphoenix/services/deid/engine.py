"""In-house PS3.15 de-identification engine — public entry point.

``scrub_dicom_bytes`` is the single function the ``services.deidentify`` facade
delegates to. It builds the per-dataset operators (capturing the original
PatientID + name BEFORE scrubbing so the date offset is per-patient and free
text can be cleaned), applies the table-driven action executor, stamps coded
provenance, and runs the fail-closed verification pass.
"""

from __future__ import annotations

import io

import pydicom

from bvphoenix.services.deid import provenance, verify
from bvphoenix.services.deid.actions import scrub_dataset
from bvphoenix.services.deid.operators import DeidOperators
from bvphoenix.services.deid.options import build_profile_options, get_engine_config
from bvphoenix.services.deid.profile_table import resolve_actions


def scrub_dicom_bytes(src: bytes) -> bytes:
    """Return a new DICOM byte string with PHI removed / pseudonymised per the
    PS3.15 Basic Profile + configured options.

    Raises ``pydicom.errors.InvalidDicomError`` on unparseable input,
    ``deid.errors.DeidVerificationError`` if the verification pass finds residual
    PHI, or ``deid.errors.RequiresReview`` for SR / encapsulated objects the
    header engine cannot fully scrub. Every caller treats these as "withhold the
    instance", so the engine never returns un-verified bytes.
    """
    cfg = get_engine_config()
    options = build_profile_options()

    ds = pydicom.dcmread(io.BytesIO(src))

    # NOTE: there is deliberately NO "already de-identified, skip" short-circuit
    # here based on the file's own PatientIdentityRemoved / DeidentificationMethod
    # tags — those are attacker-forgeable (a poisoned upload could pre-set them to
    # smuggle raw PHI through unscrubbed). Idempotency / "don't re-scrub" is a
    # property of the WORKFLOW (the DB ``deid_method_version`` stamp the reindex
    # worker checks), never inferred from file content. The engine always fully
    # scrubs whatever bytes it is handed.

    # Capture originals BEFORE scrubbing: the date offset is keyed on the
    # original PatientID, free-text cleaning needs the original name, and the
    # verification pass compares against hashed original identifier tokens.
    phi_hashes = verify.collect_phi_hashes(ds)
    patient_key = str(ds.get("PatientID", "") or "")
    patient_name = str(ds.get("PatientName", "") or "")
    extra_terms = tuple(t for t in patient_name.replace("^", " ").split() if len(t) >= 2)

    operators = DeidOperators(
        salt=cfg.salt,
        org_root_uid=cfg.org_root_uid,
        date_policy=options.date_policy,
        patient_key=patient_key,
        patient_name=patient_name,
        redact_terms=extra_terms,
    )
    actions = resolve_actions(options)
    scrub_dataset(ds, actions=actions, operators=operators, options=options)

    # Provenance on the top-level dataset only (PS3.3 C.12.1 + CID 7050).
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = provenance.method_string(options)
    ds.DeidentificationMethodCodeSequence = provenance.build_method_code_sequence(options)

    # File-meta (group 0002) is not walked by the executor — scrub it here.
    # AE titles identify the originating workstation/site; the implementation
    # version string can carry site/build text; private file-meta is vendor
    # data. Structural UIDs (SOP class, transfer syntax, implementation class)
    # are kept; MediaStorageSOPInstanceUID is re-synced to the remapped UID.
    fm = getattr(ds, "file_meta", None)
    if fm is not None:
        for kw in (
            "SourceApplicationEntityTitle",
            "SendingApplicationEntityTitle",
            "ReceivingApplicationEntityTitle",
            "ImplementationVersionName",
            "PrivateInformationCreatorUID",
            "PrivateInformation",
        ):
            if kw in fm:
                del fm[kw]
        for elem in list(fm):
            if elem.tag.is_private:
                del fm[elem.tag]
        if "SOPInstanceUID" in ds:
            fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID

    verify.assert_clean(ds, phi_hashes=phi_hashes, options=options)

    out = io.BytesIO()
    ds.save_as(out, write_like_original=False)
    return out.getvalue()


__all__ = ["scrub_dicom_bytes"]
