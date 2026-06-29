# PHR-Bundle — the patient's portable health record

The **PHR-Bundle** is an open, versioned container that holds everything
the platform knows about one account, in a form the patient can take
elsewhere and re-import. It is the concrete answer to the project's first
principle — *the patient owns the data* — and it doubles as the GDPR
Art. 20 (data-portability) export.

It is deliberately the opposite of an irreversible institutional data
lake: the bundle flows **out** to the patient, on demand, as a plain
file with a documented schema.

## What's in it

The bundle is a ZIP with two members:

| Member | Purpose |
| --- | --- |
| `manifest.json` | The canonical payload — the full structured record. |
| `README.txt` | Human-readable scope note + format/version banner. |

`manifest.json` conforms to
[`schemas/phr-bundle.v1.schema.json`](schemas/phr-bundle.v1.schema.json)
(JSON Schema, draft 2020-12). A representative instance lives at
[`schemas/phr-bundle.v1.example.json`](schemas/phr-bundle.v1.example.json).

### Self-identifying envelope

Every manifest starts with two fields so any reader can detect the
format and pick the right parser without guessing the shape:

```json
{ "format": "bitvision.phr-bundle", "schema_version": 1, "exported_at": "…", "user": { … } }
```

### Sections

All structured records about the account, grouped by domain:

- `user` — the account the bundle belongs to.
- `consents` — consent history (granted/revoked timeline).
- `erasure_requests` — GDPR Art. 17 requests and their status.
- `studies` — imaging studies the user owns (metadata; no pixels).
- `reports` — narrative reports the user authored. `author_kind`
  (`human` / `agent`) keeps AI-drafted content visibly distinct, with
  `model_id` / `provider` when an assistant wrote it.
- `markers` — in-viewer measurements / annotations the user authored.
- `patients` — every patient the user manages or self-owns.
- `patient_documents` — uploaded documents (text + metadata; the binary
  blob is excluded — see below).
- `audit_log` — the user's own audit-trail entries.

### What's intentionally NOT included

- **DICOM pixel data.** Raw images are large and are served by the
  per-study download endpoint or the Fascicolo export (`tree`/`flat`
  ZIP layout). The PHR-Bundle is the *structured record*, not the
  imaging archive.
- **Document binaries.** `patient_documents[].text` carries the
  extracted text; the original PDF/scan is fetched separately
  (`content_sha256` lets a consumer match it).

Storage isolation holds: the manifest never leaks a bucket name or a
presigned S3 URL.

## How to get one

### In the app (one click)

*Settings → Privacy & data → Health Record export (PHR-Bundle)* →
**Download my data**. The export runs as an async Job; the download
starts automatically when the bundle is ready.

### Over MCP (parity with the GUI)

```
export_health_record_bundle()      # → Job descriptor
get_job(job_id)                    # poll until status = "succeeded"
issue_download_token(job_id)       # → a single-use curl command
```

The tool rides the `health_record:export` scope (account-wide, no
DICOM) — distinct from `fascicolo:export` (one patient, images
included). Both stream the bytes through the backend, so a multi-MB
archive never passes through the agent's context.

### Over REST

```
POST /api/gdpr/export              # 202 + Job descriptor
GET  /api/jobs/{id}                # poll for result_download_url
```

## Versioning policy

`schema_version` is an integer. Additive changes (new optional fields,
new sections) keep the same version; readers MUST ignore unknown fields.
A breaking change (a removed or renamed field, a changed type) bumps the
version and ships a new `phr-bundle.vN.schema.json`. The version and the
format tag are pinned to the code by `tests/test_phr_bundle_conformance.py`
so the published spec and the builder can never drift silently.
