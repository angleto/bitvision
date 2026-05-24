# DICOM upload — web drag-drop and STOW-RS

This document describes the two HTTP paths a user or client can take to
push DICOM data into bitvision phoenix, how the server validates the
payload, how groups of files are turned into `Study` → `Series` →
`Instance` rows, and what happens after ingest.

The original ingestion path — the `bvphoenix-import` CLI described in
`backend/src/bvphoenix/cli/import_dicom.py` — remains the fastest route
for bulk imports (thousands of slices at a time) and is unchanged. The
web endpoints described here share the validation and persistence logic
via `services/dicom_ingest.py`.

---

## 1. Endpoints

Both endpoints require an authenticated user. The resulting `Study`
rows are owned by the caller's `subjects.id`.

### `POST /api/dicom/studies`

Browser-friendly multipart upload used by the drag-drop UI at
`/upload`.

- **Content-Type**: `multipart/form-data`
- **Fields**:
  - `files` (one or many) — individual `.dcm` files. The field name is
    repeated for each file (`files=…`, `files=…`). Extensionless DICOM
    is also accepted; validation happens server-side.
  - `tier` (optional, default `t1`) — contribution tier, one of
    `t1`–`t4`.
  - `is_public` (optional, default `false`) — when `true` the study is
    published publicly on creation.
- **Response**: `201 Created` with a JSON summary (see below).

### `POST /api/dicom/stow-rs`

DICOMweb Store over Web (PS3.18 §10.5). Intended for clinical
workstations and scripts that already speak STOW-RS.

- **Content-Type**: `multipart/related; type="application/dicom"; boundary=…`
- **Body**: one or more parts, each carrying a single DICOM Part-10
  object in its body. Part headers (including `Content-Type:
  application/dicom`) are ignored — we assume each part is a DICOM
  object and revalidate server-side.
- **Response**: `200 OK` with the same JSON summary as
  `POST /api/dicom/studies`.

Both endpoints cap per-file size at **512 MiB** and per-request size
(STOW-RS) at **4 GiB**.

---

## 2. Validation

Each payload goes through two fast checks:

1. **Magic bytes (preamble)** — bytes 128–131 must equal ASCII `DICM`.
   Pure Part-10 files always have this preamble.
2. **pydicom parse** — `pydicom.dcmread(..., stop_before_pixels=True)`.
   Run with `force=True` when the preamble is absent, so DIMSE exports
   and non-Part-10 files also succeed.

After parsing, the three UID tags `StudyInstanceUID`,
`SeriesInstanceUID` and `SOPInstanceUID` must all be present. Without
them the instance is not addressable and the upload is rejected for
that file.

A single bad file never aborts a batch: the error is captured in the
response `errors[]` list and the remaining files still ingest.

---

## 3. Persistence

For each valid instance the server:

1. Looks up or creates a `Study` keyed by `StudyInstanceUID`. New
   studies inherit the caller as `owner_subject_id`, the requested
   tier, and the `is_public` flag.
2. Looks up or creates a `Series` keyed by `SeriesInstanceUID` and
   links it to the study.
3. Uploads the raw bytes to S3 under
   `studies/<study>/series/<series>/<sop>.dcm` in the raw bucket
   (`BVP_S3_BUCKET_RAW`, default `bvphoenix-raw`).
4. Creates an `Instance` row with the S3 location, size, SHA-256 of
   the bytes, and SOP-level metadata.
5. Duplicate SOPInstanceUIDs are counted as
   `instances_existing` and re-upload is skipped.

After the batch, every touched series and study is marked
`ingestion_complete = true`. A `pack_volume` job is enqueued for each
touched series so the viewer doesn't pay the packing cost on first
open.

---

## 4. Response schema

```json
{
  "studies_created": ["1.2.826.0.1.3680043.8.498.…"],
  "series_created": ["1.2.826.0.1.3680043.8.498.…"],
  "instances_created": 123,
  "instances_existing": 0,
  "errors": [
    { "filename": "bogus.dcm", "message": "not a DICOM file: …" }
  ],
  "study_ids": ["8f0c…"],
  "series_ids": ["93b1…"]
}
```

- `studies_created` / `series_created` list the DICOM UIDs touched in
  this request. "Created" here means "present in the batch"; if the UID
  already existed the row is reused.
- `study_ids` / `series_ids` are the internal PostgreSQL UUIDs of the
  same resources, handy for redirecting the user straight into the
  viewer (`/studies/<id>`).
- `instances_existing` is the count of SOPInstanceUIDs that were
  already known. This is the main dedup signal when re-uploading a
  folder.

---

## 5. Frontend

The React uploader lives at `frontend/src/app/upload/page.tsx` and
delegates to `frontend/src/components/DicomUploader.tsx`. It uses
`XMLHttpRequest` rather than `fetch` to get upload progress events and
renders a per-file list, a total-bytes summary, and a completion
report.

Client-side validation is intentionally shallow (extension hint, empty
file guard). Magic-byte and pydicom checks are authoritative on the
server — this avoids shipping a Part-10 parser to the browser and
keeps validation rules in one place.

---

## 6. Relationship to the CLI

The CLI (`bvphoenix-import`) is the fastest path for bulk imports and
does not depend on the HTTP stack. Both the CLI and the web endpoints
share the core DICOM parsing helpers; the web endpoints additionally
use `services/dicom_ingest.DicomIngestor`, which is an async wrapper
over the same S3 + ORM operations. The CLI is intentionally left on
its own synchronous codepath to keep its single-process semantics and
bootstrap story simple.

---

## 7. CD / folder / ZIP / mixed-file upload (_planned_)

The two endpoints described above handle DICOM files only. For the
full-CD workflow — a DICOMDIR tree, an arbitrary folder, a ZIP, or a
mix of DICOM + PDF + images + notes uploaded together into a patient
fascicolo — a planned `POST /api/upload/bulk` endpoint will parse
`DICOMDIR` when present, auto-classify non-DICOM files by magic
bytes, and route each file to the right pool (studies, documents,
etc.). Until it lands, drop raw `.dcm` files through the endpoints
above and attach non-DICOM assets as PatientDocuments via the
patient-fascicolo API.

Per upload CD completo con DICOMDIR + altri file eterogenei vedi
[`cd-upload-guide.md`](./cd-upload-guide.md).
