# DICOMweb read surface (QIDO-RS + WADO-RS)

bitvision exposes a conformant [DICOMweb](https://www.dicomstandard.org/using/dicomweb)
read surface so any standards-aware client — OHIF, 3D Slicer, a PACS — can
query and pull imaging directly, without a bespoke integration. It closes the
prior inbound-only (STOW-RS) asymmetry.

**Base URL:** `https://<host>/api/dicom`

Point a DICOMweb client at that base URL. QIDO and WADO live under it with the
standard PS3.18 paths. Authenticate with the same session/bearer token as the
rest of the API.

**Access model.** QIDO query and WADO metadata are open to anonymous callers
for **public (OpenData) studies** — the public library is discoverable and
browsable with no credentials. WADO **retrieve** (pulling the original `.dcm`
bytes) requires the `download:dicom` permission, exactly as the rest of the
platform's file-export does: a study owner, an admin, or a share-link grantee.
Whether CC-licensed OpenData should additionally be anonymously *retrievable*
(not just browsable) is a deliberate data-governance decision, tracked
separately.

## What a client sees is what it is allowed to see

Every query is built on `visible_studies_filter`, and every UID lookup
resolves *inside* that filtered set. A study UID outside the caller's scope is
a `404` — indistinguishable from a non-existent study. Cross-patient access is
not refused, it is **inexpressible**.

Instance bytes are **streamed through the backend** (`multipart/related`); a
bucket name, object key, or presigned URL never crosses the response boundary
(storage isolation). De-identification is honoured on egress: a share-link
recipient whose grant requires it, or a T3 (training-opt-in) study, is scrubbed
per DICOM PS3.15 before the bytes leave.

## QIDO-RS (query) — `application/dicom+json`

| Endpoint | Returns |
| --- | --- |
| `GET /studies` | studies |
| `GET /studies/{study}/series` | series in a study |
| `GET /studies/{study}/series/{series}/instances` | instances in a series |
| `GET /studies/{study}/instances` | instances in a study |

Supported matching keys (others are ignored, not rejected):
`StudyInstanceUID`, `SeriesInstanceUID`, `SOPInstanceUID`, `PatientID`
(the platform's opaque patient UUID — a grouping key, never a name),
`ModalitiesInStudy`, `Modality`, `StudyDate` (exact `YYYYMMDD` or range
`YYYYMMDD-YYYYMMDD`), `StudyDescription` (with `*` wildcards). Controls:
`limit` (default 100, max 1000), `offset`. No matches → `204`.

`PatientName` is always empty and the DICOM header carries no patient name
through the read surface.

## WADO-RS (retrieve)

| Endpoint | Returns |
| --- | --- |
| `GET /studies/{study}` | all instances, `multipart/related; type="application/dicom"` |
| `GET /studies/{study}/series/{series}` | series instances (multipart) |
| `GET /studies/{study}/series/{series}/instances/{sop}` | one instance (multipart) |
| `GET /studies/{study}/metadata` | study metadata, `application/dicom+json` |
| `GET /studies/{study}/series/{series}/metadata` | series metadata |
| `GET /studies/{study}/series/{series}/instances/{sop}/metadata` | instance metadata |

Retrieve serves the stored transfer syntax (no transcoding). Metadata is the
DICOM-JSON header with pixel data excluded; it emits no `BulkDataURI`, so there
are no dangling links.

## Not yet implemented (tracked follow-ups)

* WADO-RS **frames** / **bulkdata** / **rendered** retrieval, and
  transfer-syntax transcoding (the OHIF pixel-streaming optimization). Clients
  that need pixels use full-instance WADO retrieve, which Slicer does natively.
* QIDO relational roots `GET /series` and `GET /instances` (the hierarchical
  forms above are implemented).
* A formal DICOMweb conformance statement (separate task) will enumerate the
  supported attributes and options.
