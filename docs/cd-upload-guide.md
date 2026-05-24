# Upload CD / folder / ZIP / mixed files: operational guide

> **Status**: _planned_. The endpoint `POST /api/upload/bulk` and the
> services `file_classifier`, `dicomdir_parser`, `document_type_heuristic`,
> as well as the `UniversalUploader` UI cited on this page are not yet
> implemented. Today, pure DICOM uploads go through the endpoints
> described in [`dicom-upload.md`](./dicom-upload.md).

This page explains how the bit.vision "universal" upload works:
a CD from the hospital, an arbitrary folder, a ZIP, or a heterogeneous
mix of DICOM + PDF + images + notes. The backend detects the type
of each file, parses any `DICOMDIR`, and routes each item to the
correct pool (DICOM studies, PatientDocument, etc.) within the
Health Record.

For "pure DICOM" upload (multipart of `.dcm` files only or DICOMweb STOW-RS),
see [`dicom-upload.md`](./dicom-upload.md).

---

## 1. What happens when you drag a CD

Scenario: you have a CD from the hospital containing a tree like:

```
CD_ROOT/
├── DICOMDIR
└── DICOM/
    ├── IMG0001/
    │   ├── IM000001.dcm
    │   ├── IM000002.dcm
    │   └── ...
    └── IMG0002/
        └── ...
```

You drag the entire `CD_ROOT/` folder into the Drive UI of the Fascicolo:

1. The browser sends all files with their `relative_paths`.
2. The backend endpoint `POST /api/upload/bulk` receives the batch.
3. It detects the presence of `DICOMDIR` and uses the parser
   (`services.dicomdir_parser`).
4. The parser extracts the Patient / Study / Series / Image hierarchy.
5. For each image referenced in the `DICOMDIR`, it locates the
   corresponding file in `relative_paths`, reads the bytes, and creates
   the `Instance`.
6. Ingestion result: 1+ `Study`, N `Series`, M `Instance`.
7. The created studies go into the "Studies" folder of the Fascicolo
   (or into `target_folder_id` if specified).

---

## 2. What happens with a ZIP

The backend opens the ZIP in-memory and processes the resulting tree
with the same pipeline as point 1. It also supports nested ZIPs
(`file.zip` inside `folder.zip`).

---

## 3. Auto-classify for all files

The `file_classifier` service identifies:

- **DICOM**: magic bytes `DICM` at offset 128.
- **PDF**: prefix `%PDF`.
- **Images**: JPEG / PNG / GIF / TIFF magic bytes.
- **Text**: extension `.txt` / `.md` / `.json`.
- **Audio / Video**: ID3, MP4 `ftyp`, RIFF `WAVE`.
- **Archive**: ZIP magic `PK`.

Full magic bytes table: see
[`architecture.md`](./architecture.md#3-upload-pipeline-planned) §3.

---

## 4. `document_type` heuristic

For non-DICOM files that become `PatientDocument`, the
`document_type_heuristic` service picks the type based on the filename:

| Filename pattern (regex)            | `document_type`               |
|-------------------------------------|-------------------------------|
| `consent`, `consenso`               | `consent`                     |
| `discharge`, `dimiss`               | `discharge_letter`            |
| `ricett`, `prescription`            | `prescription`                |
| `lab`, `analisi`, `esame labor`     | `lab_result`                  |
| `referral`, `rinvia`, `richies`     | `referral`                    |
| `er_report`, `pronto socc`          | `er_report`                   |
| `referto`, `report`, `radiolog`     | `er_report` (note: ambiguous) |
| IMAGE without keyword               | `personal_notebook`           |
| Other                               | `clinical_note`               |

---

## 5. Manual override

The upload UI (`UniversalUploader`) shows the detected type BEFORE
confirming. The user can change the routing per file via a dropdown.
The `manual_override[]` parameter is sent to the backend and
overrides the auto-detect.

---

## 6. Endpoint response

Example response of `POST /api/upload/bulk`:

```json
{
  "uploaded": {
    "studies_created": [
      {"id": "...", "name": "CT CHEST 2024-03-15", "series_count": 3}
    ],
    "documents_created": [
      {"id": "...", "name": "labs.pdf", "document_type": "lab_result", "kind": "pdf"}
    ],
    "skipped": [
      {"filename": "thumbs.db", "reason": "unknown format"}
    ]
  },
  "dicomdir_found": true,
  "zip_archives_found": 0,
  "total_files": 48
}
```

---

## 7. Troubleshooting

### Corrupted DICOMDIR
The parser raises `DicomdirParseError`. The backend falls back:
it processes each `.dcm` individually with direct `pydicom`.

### File misidentified
Manual override via dropdown in `UniversalUploader`.

### Large file (>500 MB)
Skipped for now. For video / audio the fallback is: skip. Future
feature: chunked upload.

### Mix of DICOM + PDF in the same folder
OK. The classifier distinguishes them based on magic bytes. DICOMs go
into the studies pool, PDFs into documents.

### CD without DICOMDIR
Supported: each `.dcm` is read individually and `Study` / `Series`
/ `Instance` are reconstructed from the internal DICOM UIDs.

---

## 8. Limitations

- No resumable multipart: if the connection drops at 99%, you must
  start from scratch.
- No chunked upload for single files > 500 MB.
- DICOMweb STOW-RS is separate (`/api/dicom/stow-rs`), more standard but
  it does not support auto-classify. See
  [`dicom-upload.md`](./dicom-upload.md).
