# Standards conformance — DICOMweb + FHIR

bitvision speaks two open healthcare standards on the way **out**: DICOMweb
(imaging) and FHIR R4 (the record). This page is the conformance statement;
its claims are gated in CI (`backend/tests/test_fhir_capability.py` +
`tests/test_dicomweb*.py`) against the registered routes and the real FHIR
models, so "open standard" is falsifiable rather than a brochure line — the
proof that a patient-owned platform can hand the data back, which a closed
data lake structurally cannot.

## FHIR R4 — CapabilityStatement

```
GET /api/fhir/metadata    →  application/fhir+json  (CapabilityStatement)
```

Public and PHI-free (pure capability metadata). It advertises, honestly, what
bitvision is: a system that **produces** FHIR R4 Bundles and serves imaging
over DICOMweb — not a general FHIR REST server. It declares the emitted
resource types and claims **no** REST interactions it does not implement.

* `fhirVersion`: `4.0.1`
* `format`: `application/fhir+json`
* Emitted resource types (in the export Bundle, see [fhir-export.md](fhir-export.md)):
  `Patient`, `ImagingStudy`, `DiagnosticReport`, `DocumentReference`.
* FHIR R4 Bundles are produced by the Health Record / GDPR export
  (`fhir-bundle.json`), not served via FHIR REST; imaging is referenced from
  `ImagingStudy.endpoint` (WADO-RS).

## DICOMweb (PS3.18) — QIDO-RS + WADO-RS

Base URL `https://<host>/api/dicom`. Full details and the access model live in
[dicomweb.md](dicomweb.md); the supported transactions are:

| Transaction | Endpoint |
| --- | --- |
| QIDO search studies | `GET /studies` |
| QIDO search series (in study) | `GET /studies/{study}/series` |
| QIDO search instances (in series) | `GET /studies/{study}/series/{series}/instances` |
| QIDO search instances (in study) | `GET /studies/{study}/instances` |
| QIDO relational series | `GET /series` |
| QIDO relational instances | `GET /instances` |
| WADO retrieve study | `GET /studies/{study}` |
| WADO retrieve series | `GET /studies/{study}/series/{series}` |
| WADO retrieve instance | `GET /studies/{study}/series/{series}/instances/{sop}` |
| WADO retrieve frames | `GET /studies/{study}/series/{series}/instances/{sop}/frames/{list}` |
| WADO retrieve bulkdata | `GET /studies/{study}/series/{series}/instances/{sop}/bulkdata/{tag}` |
| WADO retrieve metadata (study/series/instance) | `GET …/metadata` |

* **Transfer syntax**: the stored syntax is served verbatim; no transcoding.
  Frame media types: `application/octet-stream` (uncompressed), `image/jpeg`,
  `image/jls`, `image/jp2`, `image/dicom-rle`.
* **Patient scoping** is the query base: an out-of-scope UID is a `404`
  (inexpressible, not merely refused). **Storage isolation**: bytes stream
  through the backend; no bucket/key/presigned URL crosses the boundary.
  **De-identification** (PS3.15) is honoured on egress.
* `RetrieveURL` / `Content-Location` / `BulkDataURI` honour `X-Forwarded-Proto`
  (https behind the TLS-terminating proxy).

## Not yet advertised (tracked follow-ups)

WADO-RS rendered (JPEG/PNG), transfer-syntax transcoding via `Accept`, and
nested-sequence bulkdata. They are intentionally **absent** from the
CapabilityStatement / this statement until implemented — an overclaimed
conformance would undermine the credibility this surface is meant to build.
