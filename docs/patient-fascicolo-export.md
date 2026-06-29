# Patient Fascicolo Export (ZIP bundle)

The fascicolo export endpoint streams a single ZIP archive containing
everything from a patient's record — demographics, diagnostic studies,
reports, annotations and clinical documents — so patients (or clinicians
with the appropriate grants) can take their data elsewhere.

## Endpoint

```
GET /api/patients/{patient_id}/export
```

### Query parameters

| Param | Default | Values | Notes |
|-------|---------|--------|-------|
| `include` | `studies,reports,documents,annotations` | comma-separated subset of `studies`, `reports`, `documents`, `annotations`, `dicom` | Filters which sections appear in the archive |
| `format` | `zip` | `zip` | Reserved for future formats (e.g. `tar.gz`) |

### Permissions

- Baseline: the caller must have `read:metadata` on the patient. Without
  it the endpoint returns `404` (mirrors the "hide existence" pattern
  used throughout the read API).
- Per-study re-check: each study is checked for `read:metadata`
  (for inclusion in `studies` / DICOM) and `read:annotations`
  (for `reports` / `annotations`). Non-readable studies are silently
  skipped — the archive only ever contains resources the caller can see.
- DICOM inclusion (`include=dicom`) additionally requires
  `download:dicom` on the patient; requesting it without the grant
  returns `403`.

Permission resolution goes through
`services/permissions.py::can_patient`, `can` and
`effective_permissions_on_patient` — the same predicates the read
endpoints use — so a shared fascicolo link inherits exactly the access
the grant encodes.

## Archive layout

```
fascicolo-<name>-<patient_id>.zip
├── manifest.json
├── fhir-bundle.json                         # FHIR R4 Bundle (interop view)
├── reports/
│   ├── <study_id>_<version>.txt
│   └── <study_id>_<version>.<ext>        # original blob, if any
├── documents/
│   ├── <doc_id>.txt                      # text sidecar, if present
│   └── <doc_id>.<ext>                    # original file, if any
├── annotations/
│   └── <study_id>.json                   # all annotations for that study
└── studies/
    └── <study_id>/
        └── series_<n>/
            ├── manifest.json             # series + instance metadata
            └── <sop_instance_uid>.dcm    # only when include=dicom granted
```

### `manifest.json` (root)

Self-describing index of the archive:

```json
{
  "generated_at": "2026-04-17T10:15:00+00:00",
  "patient": { "id": "...", "display_name": "...", "birth_date": "...", ... },
  "includes": ["studies", "reports", "documents", "annotations"],
  "counts": {
    "studies": 3,
    "reports": 5,
    "documents": 7,
    "annotations": 12,
    "dicom_files": 0
  },
  "studies": [ ... ],
  "reports": [ ... ],
  "documents": [ ... ],
  "annotations": [ ... ]
}
```

Each entry in the flat `reports` / `documents` arrays carries a
`file_path` field when a blob sits alongside in the archive, so an
importer can re-associate metadata with files without filename parsing.

### `fhir-bundle.json` (root)

The same record as an HL7 FHIR R4 `Bundle` (Patient / ImagingStudy /
DiagnosticReport / DocumentReference), added so any FHIR-aware EHR can
ingest the export without bespoke parsing. Images are referenced via
WADO-RS and only human-attested reports are marked
`DiagnosticReport.status = final`. See [fhir-export.md](fhir-export.md).

## Response headers

- `Content-Disposition: attachment; filename="fascicolo-<name>-<id>.zip"`
- `Content-Type: application/zip`
- `X-Export-Studies`, `X-Export-Reports`, `X-Export-Documents`,
  `X-Export-Annotations`, `X-Export-Dicom-Files` — count hints for UIs
  that want to show a summary without parsing the manifest.

## Frontend integration

The patient fascicolo page
(`frontend/src/app/patients/[id]/page.tsx`) surfaces an
**"Esporta fascicolo"** button next to the patient's name. Clicking it
fetches the default bundle (no DICOM) with the bearer token from
local storage and triggers a browser download. The button is available
to any viewer with read access — what ends up in the bundle still
reflects that viewer's grants.

## Implementation notes

- Built with Python stdlib `zipfile` + `StreamingResponse`.
- Archive is currently assembled in-memory. This is fine for fascicoli
  without DICOM (manifest + text reports + small PDFs stay well under a
  few MB); it is **not** fine once raw `.dcm` files are pulled in.
- **TODO (async job path):** when a caller requests `include=dicom` and
  the patient has non-trivial study counts, we want to enqueue an ARQ
  job that writes the ZIP to S3 under a time-limited key and return a
  pending-export handle the UI polls. The endpoint carries a module-
  level TODO pointing at this work; the synchronous path is kept for
  small / metadata-only exports.
- Unavailable S3 blobs (deleted / migrated keys) are skipped silently
  and marked with `file_error: "blob unavailable"` in the relevant
  manifest entry, so a partial archive is still usable.
