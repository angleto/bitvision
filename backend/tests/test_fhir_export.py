"""FHIR R4 Bundle export conformance gate.

The export now ships a standards-based ``fhir-bundle.json`` alongside the
proprietary manifest (services/fhir_export.py). This suite makes that
conformance *falsifiable*:

* DB-free (runs in the PR lint+test gate): the DiagnosticReport status
  guard-rail is exhaustively pinned, and synthetic fascicolo + PHR
  manifests are turned into bundles whose every resource is round-tripped
  through the real ``fhir.resources`` R4B models — so a non-conformant
  shape (bad cardinality, value set, data type) fails CI.
* DB-backed (tagged-release gate): the real ``build_gdpr_bundle`` /
  ``build_export_zip`` outputs are unzipped and validated end-to-end.

The runtime exporter emits plain dicts; ``fhir.resources`` is a test-only
dependency, so the production images never carry the FHIR ORM.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.diagnosticreport import DiagnosticReport
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.endpoint import Endpoint
from fhir.resources.R4B.imagingstudy import ImagingStudy
from fhir.resources.R4B.patient import Patient

from bvphoenix.db.models.report_contents import (
    REPORT_CONTENT_AUTHOR_KINDS,
    REPORT_CONTENT_STATUSES,
)
from bvphoenix.services.fhir_export import (
    AUTHOR_KIND_EXTENSION,
    bundle_from_fascicolo_manifest,
    bundle_from_phr_bundle,
    diagnostic_report_status,
)
from tests.conftest import skip_if_no_db

_WADO = "https://bitvision.example/api/dicom"

_MODELS = {
    "Patient": Patient,
    "Endpoint": Endpoint,
    "ImagingStudy": ImagingStudy,
    "DiagnosticReport": DiagnosticReport,
    "DocumentReference": DocumentReference,
}


def _validate_bundle(bundle: dict) -> None:
    """Validate the envelope and EVERY contained resource against the
    FHIR R4B models. Validating each resource explicitly (not just the
    envelope) guarantees deep validation regardless of how the Bundle
    model treats its polymorphic ``entry.resource`` slot."""
    Bundle.model_validate(bundle)
    for entry in bundle["entry"]:
        res = entry["resource"]
        model = _MODELS[res["resourceType"]]
        model.model_validate(res)


def _resources_of(bundle: dict, resource_type: str) -> list[dict]:
    return [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == resource_type
    ]


# --------------------------------------------------------------------------- #
# DiagnosticReport status guard-rail (the patient-safety core)
# --------------------------------------------------------------------------- #


def test_status_matrix_never_final_on_unattested_or_ai() -> None:
    """Across the full (authority × status × author_kind) space, FHIR
    ``final`` appears ONLY for human-attested content; AI drafts and
    unattested rows are never ``final``."""
    authorities = ("original", "derived", "canonical_synthesis")
    final_combos: set[tuple[str, str, str]] = set()
    for authority in authorities:
        for status in REPORT_CONTENT_STATUSES:
            for author_kind in REPORT_CONTENT_AUTHOR_KINDS:
                result = diagnostic_report_status(
                    authority=authority, status=status, author_kind=author_kind
                )
                assert result in {"final", "preliminary", "cancelled"}
                # "MAI status=final su bozze AI": a not-yet-attested
                # status can never be final, whoever authored it.
                if status in {"draft", "extracted_auto", "final"}:
                    assert result == "preliminary"
                if status in {"rejected", "stale"}:
                    assert result == "cancelled"
                if result == "final":
                    final_combos.add((authority, status, author_kind))

    # The ONLY paths to FHIR final: a human-signed synthesis (signing is
    # a hard human-only gate, so author_kind is irrelevant — a human took
    # responsibility), or a human-authored, human-endorsed report.
    assert final_combos == {
        ("canonical_synthesis", "signed", "human"),
        ("canonical_synthesis", "signed", "agent"),
        ("original", "endorsed", "human"),
        ("derived", "endorsed", "human"),
    }


def test_agent_endorsed_is_not_final() -> None:
    """``reports:endorse`` can be held by an agent, so an agent-authored
    'endorsed' row must NOT be promoted to clinician-final."""
    assert (
        diagnostic_report_status(authority="original", status="endorsed", author_kind="agent")
        == "preliminary"
    )


# --------------------------------------------------------------------------- #
# Fascicolo (patient-scoped) bundle — DB-free
# --------------------------------------------------------------------------- #


def _fascicolo_manifest() -> dict:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    rid_human = str(uuid.uuid4())
    rid_agent = str(uuid.uuid4())
    did = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    return {
        "generated_at": now,
        "patient": {
            "id": pid,
            "display_name": "Mario Rossi",
            "birth_date": "1980-01-01",
            "sex": "M",
            "external_identifiers": [
                {"type": "fiscal-code", "value": "RSSMRA80A01H501U"},
                {"type": "MR", "value": "MRN-42"},
            ],
        },
        "studies": [
            {
                "id": sid,
                "study_instance_uid": "1.2.840.113619.2.55.3.12345",
                "study_description": "CT CHEST",
                "study_date": "2024-05-01",
                "modalities": ["CT"],
            }
        ],
        "reports": [
            {
                "id": rid_human,
                "authority": "original",
                "status": "endorsed",
                "author_kind": "human",
                "title": "Radiology report",
                "narrative_path": f"reports/{rid_human}.md",
                "created_at": now,
            },
            {
                "id": rid_agent,
                "authority": "canonical_synthesis",
                "status": "draft",
                "author_kind": "agent",
                "title": "AI synthesis (draft)",
                "narrative_path": f"reports/{rid_agent}.md",
                "model_id": "claude-opus-4-8",
                "provider": "anthropic",
                "created_at": now,
            },
        ],
        "documents": [
            {
                "id": did,
                "kind": "referto",
                "title": "referto.pdf",
                "file_content_type": "application/pdf",
                "file_path": f"documents/{did}.pdf",
                "document_date": "2024-05-01",
                "created_at": now,
            }
        ],
    }


def test_fascicolo_bundle_validates_and_links() -> None:
    manifest = _fascicolo_manifest()
    bundle = bundle_from_fascicolo_manifest(manifest, wado_base=_WADO)
    _validate_bundle(bundle)

    pid = manifest["patient"]["id"]
    patient = _resources_of(bundle, "Patient")[0]
    assert patient["gender"] == "male"
    assert {i["value"] for i in patient["identifier"]} == {"RSSMRA80A01H501U", "MRN-42"}

    # WADO-RS: exactly one Endpoint, referenced by the study.
    endpoints = _resources_of(bundle, "Endpoint")
    assert len(endpoints) == 1
    assert endpoints[0]["address"] == _WADO
    assert endpoints[0]["connectionType"]["code"] == "dicom-wado-rs"

    study = _resources_of(bundle, "ImagingStudy")[0]
    assert study["subject"]["reference"] == f"urn:uuid:{pid}"
    assert study["identifier"][0]["value"] == "urn:oid:1.2.840.113619.2.55.3.12345"
    assert study["modality"][0]["code"] == "CT"
    assert study["endpoint"][0]["reference"] == f"urn:uuid:{endpoints[0]['id']}"

    reports = {r["code"]["text"]: r for r in _resources_of(bundle, "DiagnosticReport")}
    human = reports["Radiology report"]
    agent = reports["AI synthesis (draft)"]
    # Guard-rail end-to-end: human-endorsed → final; AI draft → preliminary.
    assert human["status"] == "final"
    assert agent["status"] == "preliminary"
    # Every report carries its author-kind; images-by-reference narrative.
    for r in (human, agent):
        kinds = [e["valueCode"] for e in r["extension"] if e["url"] == AUTHOR_KIND_EXTENSION]
        assert kinds and kinds[0] in ("human", "agent")
        assert r["presentedForm"][0]["url"].startswith("reports/")
    assert agent["status"] != "final"  # AI draft is never clinician-final

    docref = _resources_of(bundle, "DocumentReference")[0]
    assert docref["subject"]["reference"] == f"urn:uuid:{pid}"
    assert docref["content"][0]["attachment"]["url"].startswith("documents/")


# --------------------------------------------------------------------------- #
# GDPR PHR bundle (multi-patient) — DB-free
# --------------------------------------------------------------------------- #


def _phr_bundle() -> dict:
    pid = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    return {
        "format": "bitvision.phr-bundle",
        "schema_version": 1,
        "exported_at": now,
        "patients": [
            {
                "id": pid,
                "display_name": "Anna Bianchi",
                "birth_date": "1975-03-12",
                "sex": "F",
                "external_identifiers": [{"type": "MR", "value": "MRN-7"}],
            }
        ],
        "studies": [
            {
                "id": str(uuid.uuid4()),
                "study_instance_uid": "1.2.3.4.5",
                "study_description": "MR BRAIN",
                "study_date": "2023-11-02",
                "modalities": ["MR"],
                "patient_id": pid,
            }
        ],
        "reports": [
            {
                "id": str(uuid.uuid4()),
                "authority_id": "canonical_synthesis",
                "status": "draft",
                "author_kind": "agent",
                "title": "Summary",
                "narrative_md": "No acute findings.",
                "model_id": "claude-opus-4-8",
                "provider": "anthropic",
                "created_at": now,
            }
        ],
        "patient_documents": [
            {
                "id": str(uuid.uuid4()),
                "document_kind": "referto",
                "title": "Discharge letter",
                "text": "Paziente dimesso in buone condizioni.",
                "content_type": "text/plain",
                "document_date": "2023-11-03",
                "created_at": now,
                "patient_id": pid,
            }
        ],
    }


def test_phr_bundle_validates_and_inlines_text() -> None:
    import base64

    phr = _phr_bundle()
    bundle = bundle_from_phr_bundle(phr, wado_base=_WADO)
    _validate_bundle(bundle)

    pid = phr["patients"][0]["id"]
    study = _resources_of(bundle, "ImagingStudy")[0]
    assert study["subject"]["reference"] == f"urn:uuid:{pid}"

    report = _resources_of(bundle, "DiagnosticReport")[0]
    # AI draft from the GDPR export is preliminary and carries its
    # narrative inline as the conclusion (no archive file to point at).
    assert report["status"] == "preliminary"
    assert report["conclusion"] == "No acute findings."
    # No subject link for author-grouped reports — and that's valid.
    assert "subject" not in report

    docref = _resources_of(bundle, "DocumentReference")[0]
    att = docref["content"][0]["attachment"]
    assert base64.b64decode(att["data"]).decode("utf-8").startswith("Paziente dimesso")
    assert docref["subject"]["reference"] == f"urn:uuid:{pid}"


def test_empty_manifest_yields_valid_empty_bundle() -> None:
    """A patient with no studies/reports/docs still produces a valid
    single-Patient bundle (and no dangling Endpoint)."""
    pid = str(uuid.uuid4())
    bundle = bundle_from_fascicolo_manifest(
        {"generated_at": datetime.now(UTC).isoformat(), "patient": {"id": pid}}, wado_base=_WADO
    )
    _validate_bundle(bundle)
    assert _resources_of(bundle, "Endpoint") == []
    assert len(_resources_of(bundle, "Patient")) == 1


# --------------------------------------------------------------------------- #
# DB-backed end-to-end gates (tagged release)
# --------------------------------------------------------------------------- #


@skip_if_no_db
@pytest.mark.asyncio
async def test_gdpr_zip_contains_valid_fhir_bundle(db_session, make_user, make_study) -> None:
    """The real GDPR ZIP carries a fhir-bundle.json that validates."""
    from bvphoenix.services.gdpr_export import build_gdpr_zip

    user = await make_user()
    await make_study(user, description="CT CHEST")

    zip_bytes, _ = await build_gdpr_zip(db_session, user)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "fhir-bundle.json"} <= names
        bundle = json.loads(zf.read("fhir-bundle.json"))

    _validate_bundle(bundle)
    assert len(_resources_of(bundle, "ImagingStudy")) == 1
    assert len(_resources_of(bundle, "Patient")) == 1


@skip_if_no_db
@pytest.mark.asyncio
async def test_fascicolo_zip_contains_valid_fhir_bundle(db_session, make_user, make_study) -> None:
    """The real fascicolo ZIP (in-memory builder) carries a valid,
    patient-linked fhir-bundle.json."""
    from bvphoenix.db.models import Patient
    from bvphoenix.services.patient_export import build_export_zip

    user = await make_user()
    patient = Patient(
        id=uuid.uuid4(), managed_by_subject_id=user.subject_id, display_name="Test Patient"
    )
    db_session.add(patient)
    await db_session.flush()
    await make_study(user, patient=patient, description="CT CHEST")

    zip_bytes, _ = await build_export_zip(db_session, user, patient, {"studies", "reports"})
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert "fhir-bundle.json" in zf.namelist()
        bundle = json.loads(zf.read("fhir-bundle.json"))

    _validate_bundle(bundle)
    study = _resources_of(bundle, "ImagingStudy")[0]
    assert study["subject"]["reference"] == f"urn:uuid:{patient.id}"
    assert _resources_of(bundle, "Endpoint")[0]["connectionType"]["code"] == "dicom-wado-rs"
