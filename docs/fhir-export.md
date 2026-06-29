# FHIR R4 Bundle — the interoperable export view

Every Health Record (Fascicolo) and GDPR/PHR export now carries a
`fhir-bundle.json` next to the proprietary `manifest.json`. It is the
**same record** rendered as an [HL7 FHIR R4](https://hl7.org/fhir/R4/)
`Bundle`, so any FHIR-aware EHR can ingest a bitvision export without a
bespoke parser.

This is *additive*: `manifest.json` (the
[PHR-Bundle](phr-bundle.md)) stays the canonical, lossless payload for
round-tripping back into bitvision; `fhir-bundle.json` is the
standards-based interop projection. It turns the project's first
principle — *the patient owns and carries the data* — into an artefact
every conformant system reads, the opposite of a one-way institutional
data lake.

## Shape

A `collection` Bundle (a portability set, not a clinical document) with
`urn:uuid` `fullUrl`s and matching internal references:

| Resource | Source | Notes |
| --- | --- | --- |
| `Patient` | patient demographics | name, `birthDate`, `gender`, identifiers (fiscal code, MRN, …) |
| `Endpoint` | DICOMweb base URL | one per bundle; `connectionType = dicom-wado-rs` |
| `ImagingStudy` | each study | DICOM `StudyInstanceUID` as `urn:oid:` identifier, `modality` (DCM coding), `endpoint` → the WADO-RS `Endpoint` |
| `DiagnosticReport` | each report content | status per the guard-rail below; `author-kind` extension; narrative via `presentedForm` (fascicolo) or `conclusion` (GDPR) |
| `DocumentReference` | each patient document | binary by relative `url` (fascicolo) or inline base64 `data` (GDPR) |

### Images by reference, never inline

Pixels never travel inside the FHIR bundle. `ImagingStudy.endpoint`
points at the bitvision [DICOMweb](dicomweb.md) WADO-RS surface; a client
resolves `{base}/studies/{StudyInstanceUID}` itself. No bucket, key, or
presigned URL ever crosses the boundary.

## Patient-safety guard-rail: `DiagnosticReport.status`

A downstream EHR must never mistake an AI draft for a clinician-attested
report. `DiagnosticReport.status` is therefore computed conservatively
from the bitvision report workflow:

| bitvision report | FHIR `status` |
| --- | --- |
| `canonical_synthesis` **signed** | `final` — signing is a hard human-only gate (`synthesis:sign`); a human took responsibility |
| `original`/`derived` **endorsed** by a **human author** | `final` — a clinician's own validated report |
| `original`/`derived` **endorsed**, author = agent | `preliminary` — `reports:endorse` can be held by an agent, so this is not promoted |
| `draft` / `extracted_auto` / unsigned synthesis | `preliminary` — never `final` on a draft |
| `rejected` / `stale` | `cancelled` |

So **`status = final` is reserved for human-attested content.** Every
`DiagnosticReport` also carries an `author-kind` extension
(`human` | `agent`) and a `source-status` extension recording the exact
bitvision `authority/status`, so AI provenance survives the export
(plus `ai-model` / `ai-provider` extensions when an agent drafted it).

Extension URLs live under `https://bitvision.xeno.garden/fhir/StructureDefinition/`.

## Conformance gate

The runtime exporter (`backend/src/bvphoenix/services/fhir_export.py`)
emits plain dicts — the production API/worker images do **not** carry a
FHIR ORM. Conformance is instead made falsifiable in CI:
`backend/tests/test_fhir_export.py` round-trips every emitted resource
through the real [`fhir.resources`](https://pypi.org/project/fhir.resources/)
R4B models (a test-only dependency) and exhaustively pins the
`DiagnosticReport.status` guard-rail. A non-conformant change fails the
lint+test gate.
