"""FHIR R4 Bundle serialization for the Health Record / GDPR export.

This adds a standards-based ``fhir-bundle.json`` *alongside* the
proprietary ``manifest.json`` (PHR-Bundle) that the fascicolo and GDPR
exports already emit. A FHIR-aware EHR can ingest a bitvision export
without bespoke parsing — turning the "the patient owns and carries the
data" thesis (note a8eeae3c) into an artefact every conformant system
reads. Nothing is *replaced*: the manifest stays canonical for round-
tripping back into bitvision; the FHIR bundle is the interop view.

Design (deliberately small and robust):

* **Plain-dict builders, no FHIR ORM at runtime.** The production
  API/worker images never import ``fhir.resources``. Conformance is
  falsifiable in CI instead: ``tests/test_fhir_export.py`` round-trips
  every emitted resource through ``fhir.resources`` R4B models, so a
  non-conformant change fails the lint+test gate. This keeps the hot
  path a transparent dict assembly and the image lean.

* **Bundle.type = "collection".** A portability set of resources with
  ``urn:uuid`` fullUrls and matching internal references — the right
  Bundle flavour for "here is a record for exchange" (a ``document``
  bundle would force a Composition we don't have).

* **Patient-safety guard-rail on DiagnosticReport.status.** FHIR
  ``final`` is reserved for *human-attested* reports: a
  ``canonical_synthesis`` a human **signed** (``synthesis:sign`` is a
  hard human-only gate — an agent token is refused even with the
  scope), or an ``original``/``derived`` report a human **authored and
  endorsed**. AI-authored (``author_kind='agent'``) or not-yet-attested
  content is ``preliminary`` at most and can never present as a
  clinician-final report to a downstream EHR. Every DiagnosticReport
  also carries an ``author-kind`` extension so AI provenance survives
  the export. ``reports:endorse`` *can* be held by an agent, so an
  agent-authored "endorsed" row is intentionally NOT promoted to
  ``final`` here — only a human signature lifts AI content to final.

* **Images via WADO-RS, never inline pixels / storage URLs.** Each
  ImagingStudy points at a single ``Endpoint`` (connectionType
  ``dicom-wado-rs``) whose address is the bitvision DICOMweb base; a
  client resolves ``{base}/studies/{StudyInstanceUID}`` itself. No
  bucket/key/presigned URL ever crosses the boundary.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

# Canonical bitvision FHIR namespace. Extension/identifier ``url`` /
# ``system`` values must be absolute URIs but need not resolve; pinning
# them to the project domain keeps them stable across deployments.
FHIR_NS = "https://bitvision.xeno.garden/fhir"
AUTHOR_KIND_EXTENSION = f"{FHIR_NS}/StructureDefinition/author-kind"
SOURCE_STATUS_EXTENSION = f"{FHIR_NS}/StructureDefinition/source-status"
AI_MODEL_EXTENSION = f"{FHIR_NS}/StructureDefinition/ai-model"
AI_PROVIDER_EXTENSION = f"{FHIR_NS}/StructureDefinition/ai-provider"
IDENTIFIER_SYSTEM_BASE = f"{FHIR_NS}/identifier"

# Standard external code systems.
DCM_SYSTEM = "http://dicom.nema.org/resources/ontology/DCM"
DICOM_UID_SYSTEM = "urn:dicom:uid"
ENDPOINT_CONNECTION_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/endpoint-connection-type"

# A fixed namespace so the synthetic Endpoint id is a deterministic
# function of the WADO base URL (stable bytes across reruns for one
# deployment, distinct across deployments).
_ENDPOINT_NS = uuid.UUID("6f4d2f8a-1c3b-4e2a-9b7d-0a1f2e3c4d5e")


def dicomweb_base_url(public_origin: str) -> str:
    """Public base URL of the DICOMweb (WADO-RS) surface for FHIR
    ImagingStudy.endpoint: same origin as the frontend, under
    ``/api/dicom`` (mirrors ``api.dicomweb._wado_base``)."""
    return public_origin.rstrip("/") + "/api/dicom"


def _urn(resource_id: str) -> str:
    """``urn:uuid:`` fullUrl / reference for a collection-bundle entry."""
    return f"urn:uuid:{resource_id}"


def _fhir_gender(sex: str | None) -> str | None:
    """Map a stored ``Patient.sex`` (DICOM-ish ``M``/``F``/``O`` or a
    word) to a FHIR ``AdministrativeGender`` code. ``None`` → omit the
    element; an unrecognised non-empty value → ``unknown`` (honest: we
    hold *a* value but can't map it)."""
    if not sex:
        return None
    s = sex.strip().lower()
    if not s:
        return None
    return {
        "m": "male",
        "male": "male",
        "f": "female",
        "female": "female",
        "o": "other",
        "other": "other",
    }.get(s, "unknown")


# --------------------------------------------------------------------------- #
# DiagnosticReport status guard-rail (pure; exhaustively unit-tested)
# --------------------------------------------------------------------------- #


def diagnostic_report_status(
    *, authority: str | None, status: str | None, author_kind: str | None
) -> str:
    """Map a ``ReportContent`` (authority, status, author_kind) to a FHIR
    ``DiagnosticReport.status`` code, conservatively.

    Returns one of: ``final`` | ``preliminary`` | ``cancelled``.

    ``final`` ONLY when human-attested:

    * ``canonical_synthesis`` + ``signed`` — the heavy workflow's
      signature is a hard human-only gate, so this is provably a
      clinician's responsibility (the content may have been AI-drafted;
      the ``author-kind`` extension still records that).
    * ``original``/``derived`` + ``endorsed`` + ``author_kind == 'human'``
      — a clinician's own report they validated. Agent-authored
      "endorsed" rows stay ``preliminary`` because ``reports:endorse``
      is grantable to agents.

    ``rejected`` / ``stale`` → ``cancelled`` (no longer an active
    report). Everything else (``draft``, unsigned ``final`` synthesis,
    ``extracted_auto``, any agent draft) → ``preliminary``: AI/unattested
    content never claims clinician-final to a downstream EHR.
    """
    if status in ("rejected", "stale"):
        return "cancelled"
    if authority == "canonical_synthesis" and status == "signed":
        return "final"
    if authority in ("original", "derived") and status == "endorsed" and author_kind == "human":
        return "final"
    return "preliminary"


# --------------------------------------------------------------------------- #
# Resource builders — plain dicts, FHIR R4(B)-shaped
# --------------------------------------------------------------------------- #


def patient_resource(
    *,
    patient_id: str,
    display_name: str | None = None,
    birth_date: str | None = None,
    sex: str | None = None,
    identifiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    res: dict[str, Any] = {"resourceType": "Patient", "id": patient_id}
    ids: list[dict[str, str]] = []
    for ident in identifiers or []:
        if not isinstance(ident, dict):
            continue
        val = ident.get("value")
        if not val:
            continue
        typ = ident.get("type")
        system = f"{IDENTIFIER_SYSTEM_BASE}/{typ}" if typ else IDENTIFIER_SYSTEM_BASE
        ids.append({"system": system, "value": str(val)})
    if ids:
        res["identifier"] = ids
    if display_name:
        res["name"] = [{"text": display_name}]
    gender = _fhir_gender(sex)
    if gender:
        res["gender"] = gender
    if birth_date:
        res["birthDate"] = str(birth_date)
    return res


def endpoint_resource(*, endpoint_id: str, address: str) -> dict[str, Any]:
    """A DICOMweb WADO-RS endpoint. ImagingStudy.endpoint points here so
    a client can retrieve pixels from the bitvision DICOMweb surface."""
    return {
        "resourceType": "Endpoint",
        "id": endpoint_id,
        "status": "active",
        "connectionType": {
            "system": ENDPOINT_CONNECTION_TYPE_SYSTEM,
            "code": "dicom-wado-rs",
        },
        "name": "bitvision DICOMweb (WADO-RS)",
        "payloadType": [{"text": "DICOM"}],
        "address": address,
    }


def _subject_reference(patient_ref: str | None) -> dict[str, str]:
    """A Reference to the subject patient, or a display-only Reference
    when the patient is not part of this export (valid FHIR: a Reference
    may carry ``display`` without ``reference``)."""
    if patient_ref:
        return {"reference": patient_ref}
    return {"display": "patient not included in this export"}


def imaging_study_resource(
    *,
    study_id: str,
    study_instance_uid: str | None,
    modalities: list[str] | None,
    started: str | None,
    description: str | None,
    patient_ref: str | None,
    endpoint_ref: str | None,
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "ImagingStudy",
        "id": study_id,
        "status": "available",
        "subject": _subject_reference(patient_ref),
    }
    if study_instance_uid:
        res["identifier"] = [{"system": DICOM_UID_SYSTEM, "value": f"urn:oid:{study_instance_uid}"}]
    mods = [{"system": DCM_SYSTEM, "code": m} for m in (modalities or []) if m]
    if mods:
        res["modality"] = mods
    if started:
        res["started"] = str(started)
    if description:
        res["description"] = description
    if endpoint_ref:
        res["endpoint"] = [{"reference": endpoint_ref}]
    return res


def diagnostic_report_resource(
    *,
    report_id: str,
    authority: str | None,
    status: str | None,
    author_kind: str | None,
    title: str | None,
    issued: str | None = None,
    effective: str | None = None,
    patient_ref: str | None = None,
    narrative_url: str | None = None,
    conclusion: str | None = None,
    model_id: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """One ``DiagnosticReport``. ``status`` is computed by the guard-rail
    above; ``author-kind`` + ``source-status`` extensions preserve the
    bitvision provenance for an honest round-trip."""
    res: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": report_id,
        "status": diagnostic_report_status(
            authority=authority, status=status, author_kind=author_kind
        ),
        "code": {"text": title or "Clinical report"},
    }
    if patient_ref:
        res["subject"] = {"reference": patient_ref}
    if effective:
        res["effectiveDateTime"] = str(effective)
    if issued:
        res["issued"] = str(issued)
    if conclusion:
        res["conclusion"] = conclusion
    if narrative_url:
        res["presentedForm"] = [
            {
                "contentType": "text/markdown",
                "url": narrative_url,
                "title": title or "report",
            }
        ]
    extensions: list[dict[str, Any]] = [
        {"url": AUTHOR_KIND_EXTENSION, "valueCode": author_kind or "human"}
    ]
    if authority or status:
        extensions.append(
            {
                "url": SOURCE_STATUS_EXTENSION,
                "valueString": f"{authority or '?'}/{status or '?'}",
            }
        )
    # Surface AI model provenance when present so it survives the export
    # (a downstream reader sees *which* model drafted agent content).
    if model_id:
        extensions.append({"url": AI_MODEL_EXTENSION, "valueString": model_id})
    if provider:
        extensions.append({"url": AI_PROVIDER_EXTENSION, "valueString": provider})
    res["extension"] = extensions
    return res


def document_reference_resource(
    *,
    doc_id: str,
    doc_kind: str | None,
    title: str | None,
    content_type: str | None,
    issued: str | None = None,
    document_date: str | None = None,
    patient_ref: str | None = None,
    file_url: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """One ``DocumentReference``. Prefers a relative ``url`` to the
    binary inside the same archive (fascicolo export); falls back to
    inlining the extracted ``text`` as base64 ``data`` so the bundle is
    self-contained when no binary travels with it (GDPR export)."""
    res: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "id": doc_id,
        "status": "current",
        "type": {"text": doc_kind or "document"},
    }
    if patient_ref:
        res["subject"] = {"reference": patient_ref}
    if issued:
        res["date"] = str(issued)

    attachment: dict[str, Any] = {}
    if content_type:
        attachment["contentType"] = content_type
    if title:
        attachment["title"] = title
    if document_date:
        attachment["creation"] = str(document_date)
    if file_url:
        attachment["url"] = file_url
    elif text:
        attachment.setdefault("contentType", "text/plain")
        attachment["data"] = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # ``content`` is 1..*; always emit one entry even when the attachment
    # is sparse so the resource stays valid.
    res["content"] = [{"attachment": attachment}]
    return res


def to_collection_bundle(
    resources: list[dict[str, Any]], *, timestamp: str | None = None, bundle_id: str | None = None
) -> dict[str, Any]:
    """Wrap resources in a ``collection`` Bundle with ``urn:uuid``
    fullUrls (matching the internal references the builders emit)."""
    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "id": bundle_id or str(uuid.uuid4()),
        "type": "collection",
        "entry": [{"fullUrl": _urn(r["id"]), "resource": r} for r in resources],
    }
    if timestamp:
        bundle["timestamp"] = timestamp
    return bundle


# --------------------------------------------------------------------------- #
# Adapters — map an export manifest dict to a FHIR Bundle
# --------------------------------------------------------------------------- #


# Resource types bitvision emits in an export Bundle — also what the
# CapabilityStatement advertises. Single source so the two can't drift.
EXPORTED_RESOURCE_TYPES: tuple[str, ...] = (
    "Patient",
    "ImagingStudy",
    "DiagnosticReport",
    "DocumentReference",
)


def capability_statement(
    *,
    software_version: str,
    fhir_base_url: str,
    dicomweb_base_url: str,
    date_iso: str,
) -> dict[str, Any]:
    """A FHIR R4 ``CapabilityStatement`` for bitvision's standards surface.

    PHI-free by construction — pure capability metadata. It is deliberately
    honest about what bitvision *is*: a system that **produces** FHIR R4
    Bundles (the Health Record / GDPR export) and retrieves imaging over
    **DICOMweb**, not a general FHIR REST server. So it declares the emitted
    resource types with documentation but advertises **no** REST interactions
    it does not implement — a conformance claim a closed lake cannot make and
    a client can falsify by GETting ``/api/fhir/metadata``.
    """
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": date_iso,
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "format": ["application/fhir+json"],
        "software": {"name": "bitvision phoenix", "version": software_version},
        "implementation": {
            "description": (
                "bitvision phoenix — patient-owned imaging platform. Produces "
                "FHIR R4 Bundles via the Health Record / GDPR export and serves "
                "imaging over DICOMweb (QIDO-RS / WADO-RS)."
            ),
            "url": fhir_base_url.rstrip("/"),
        },
        "rest": [
            {
                "mode": "server",
                "documentation": (
                    "Read / export surface. FHIR R4 Bundles (type=collection) are "
                    "produced by the export — see docs/fhir-export.md — and are not "
                    f"served via FHIR REST. Imaging is retrieved over DICOMweb at "
                    f"{dicomweb_base_url.rstrip('/')} (QIDO-RS query, WADO-RS "
                    "retrieve / metadata / frames / bulkdata). DiagnosticReport "
                    "carries status=final only for human-attested reports."
                ),
                "resource": [
                    {
                        "type": rtype,
                        "documentation": "Emitted in the bitvision export Bundle.",
                    }
                    for rtype in EXPORTED_RESOURCE_TYPES
                ],
            }
        ],
    }


def _endpoint_id_for(wado_base: str) -> str:
    return str(uuid.uuid5(_ENDPOINT_NS, wado_base))


def bundle_from_fascicolo_manifest(
    manifest: dict[str, Any], *, wado_base: str | None
) -> dict[str, Any]:
    """Build a patient-scoped FHIR Bundle from a patient_export manifest.

    Single patient → every ImagingStudy / DiagnosticReport /
    DocumentReference resolves its ``subject`` unambiguously to that
    patient. Reflects exactly what the manifest carries (so a scoped or
    includes-filtered export yields a correspondingly scoped bundle).
    """
    patient = manifest.get("patient") or {}
    patient_id = patient.get("id")
    patient_ref = _urn(patient_id) if patient_id else None

    resources: list[dict[str, Any]] = []
    if patient_id:
        resources.append(
            patient_resource(
                patient_id=patient_id,
                display_name=patient.get("display_name"),
                birth_date=patient.get("birth_date"),
                sex=patient.get("sex"),
                identifiers=patient.get("external_identifiers"),
            )
        )

    studies = manifest.get("studies") or []
    endpoint_ref: str | None = None
    if studies and wado_base:
        endpoint_id = _endpoint_id_for(wado_base)
        resources.append(endpoint_resource(endpoint_id=endpoint_id, address=wado_base))
        endpoint_ref = _urn(endpoint_id)
    for s in studies:
        sid = s.get("id")
        if not sid:
            continue
        resources.append(
            imaging_study_resource(
                study_id=sid,
                study_instance_uid=s.get("study_instance_uid"),
                modalities=s.get("modalities"),
                started=s.get("study_date"),
                description=s.get("study_description"),
                patient_ref=patient_ref,
                endpoint_ref=endpoint_ref,
            )
        )

    for r in manifest.get("reports") or []:
        rid = r.get("id")
        if not rid:
            continue
        created = r.get("created_at")
        resources.append(
            diagnostic_report_resource(
                report_id=rid,
                authority=r.get("authority"),
                status=r.get("status"),
                author_kind=r.get("author_kind"),
                title=r.get("title"),
                issued=created,
                effective=created,
                patient_ref=patient_ref,
                narrative_url=r.get("narrative_path"),
                model_id=r.get("model_id"),
                provider=r.get("provider"),
            )
        )

    for d in manifest.get("documents") or []:
        did = d.get("id")
        if not did:
            continue
        resources.append(
            document_reference_resource(
                doc_id=did,
                doc_kind=d.get("kind"),
                title=d.get("title"),
                content_type=d.get("file_content_type"),
                issued=d.get("created_at"),
                document_date=d.get("document_date"),
                patient_ref=patient_ref,
                file_url=d.get("file_path"),
                text=d.get("text"),
            )
        )

    return to_collection_bundle(resources, timestamp=manifest.get("generated_at"))


def bundle_from_phr_bundle(phr: dict[str, Any], *, wado_base: str | None) -> dict[str, Any]:
    """Build a FHIR Bundle from a GDPR PHR-Bundle (``build_gdpr_bundle``).

    Multi-patient ("all my data"). Studies and documents carry a
    ``patient_id`` so their ``subject`` is linked; reports are grouped by
    *author* in the PHR-Bundle and carry no patient link, so their
    DiagnosticReport.subject is omitted (valid FHIR) while status +
    author-kind still convey the guard-rail.
    """
    resources: list[dict[str, Any]] = []

    patient_refs: dict[str, str] = {}
    for p in phr.get("patients") or []:
        pid = p.get("id")
        if not pid:
            continue
        patient_refs[pid] = _urn(pid)
        resources.append(
            patient_resource(
                patient_id=pid,
                display_name=p.get("display_name"),
                birth_date=p.get("birth_date"),
                sex=p.get("sex"),
                identifiers=p.get("external_identifiers"),
            )
        )

    studies = phr.get("studies") or []
    endpoint_ref: str | None = None
    if studies and wado_base:
        endpoint_id = _endpoint_id_for(wado_base)
        resources.append(endpoint_resource(endpoint_id=endpoint_id, address=wado_base))
        endpoint_ref = _urn(endpoint_id)
    for s in studies:
        sid = s.get("id")
        if not sid:
            continue
        resources.append(
            imaging_study_resource(
                study_id=sid,
                study_instance_uid=s.get("study_instance_uid"),
                modalities=s.get("modalities"),
                started=s.get("study_date"),
                description=s.get("study_description"),
                patient_ref=patient_refs.get(s.get("patient_id") or ""),
                endpoint_ref=endpoint_ref,
            )
        )

    for r in phr.get("reports") or []:
        rid = r.get("id")
        if not rid:
            continue
        created = r.get("created_at")
        resources.append(
            diagnostic_report_resource(
                report_id=rid,
                authority=r.get("authority_id"),
                status=r.get("status"),
                author_kind=r.get("author_kind"),
                title=r.get("title"),
                issued=created,
                effective=created,
                patient_ref=None,
                conclusion=r.get("narrative_md"),
                model_id=r.get("model_id"),
                provider=r.get("provider"),
            )
        )

    for d in phr.get("patient_documents") or []:
        did = d.get("id")
        if not did:
            continue
        resources.append(
            document_reference_resource(
                doc_id=did,
                doc_kind=d.get("document_kind"),
                title=d.get("title"),
                content_type=d.get("content_type"),
                issued=d.get("created_at"),
                document_date=d.get("document_date"),
                patient_ref=patient_refs.get(d.get("patient_id") or ""),
                text=d.get("text"),
            )
        )

    return to_collection_bundle(resources, timestamp=phr.get("exported_at"))


__all__ = [
    "AUTHOR_KIND_EXTENSION",
    "EXPORTED_RESOURCE_TYPES",
    "SOURCE_STATUS_EXTENSION",
    "bundle_from_fascicolo_manifest",
    "bundle_from_phr_bundle",
    "capability_statement",
    "diagnostic_report_resource",
    "diagnostic_report_status",
    "dicomweb_base_url",
    "document_reference_resource",
    "endpoint_resource",
    "imaging_study_resource",
    "patient_resource",
    "to_collection_bundle",
]
