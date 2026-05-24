# workers — async jobs

Arq-based worker process. Handles long-running jobs dispatched by the
backend: DICOM ingestion (parsing, de-identification, derivative
generation, embeddings) and LLM annotation runs.

## Stack

- [`arq`](https://arq-docs.helpmanual.io/) — asyncio-native Redis queue
- `pydicom`, `highdicom`, `SimpleITK`, `nibabel` — DICOM/volume I/O
- `dcm2niix` (CLI, wrapped via subprocess) — series-to-NIfTI packing
- `pydicom-deid` — PHI removal for T3 uploads
- `boto3` — S3-compatible storage
- Optional AI deps (`torch`, `monai`, `torchio`, `open-clip-torch`) for
  embedding generation, behind the `[ai]` extra

## Run

```sh
make up              # infra
make workers.install # uv sync
make workers.dev     # runs `arq bvworkers.main.WorkerSettings`
```

For AI-heavy tasks:

```sh
cd workers && uv sync --extra ai
```

## Task registry

All tasks are listed in `src/bvworkers/tasks/registry.py`. Adding a new
task means:

1. Write the coroutine in a new module under `tasks/`
2. Append it to `FUNCTIONS` in `registry.py`

## Pipeline

The ingestion pipeline (`pipeline/`) is the meat: it takes an upload
manifest, parses the DICOM files on S3, generates derivatives, runs
de-identification if the contribution tier requires it, computes
embeddings, and writes the results back to Postgres + pgvector.

See `../docs/DESIGN.md` §5 (tier rules).

## What's here

The worker registry currently covers the following families (see
`src/bvworkers/tasks/` for the live list):

- **Ingestion + derivatives**: `ingest_bulk`, `pack_volume`,
  `prefetch_series`, `embed_series`, `embed_text`,
  `embed_text_multilingual`, `autotag_target`, `chunk_and_embed`,
  `reindex_batch`, `deidentify_reindex`.
- **Documents**: `bulk_document_update`, `purge_documents`, `ocr`,
  `entity_extraction`.
- **Long ops + exports**: `cleanup_jobs`, `generate_summary`,
  `export_gdpr`, `export_patient`, `export_study`.
- **Segmentation + registration**: `segment_auto`,
  `segment_interactive`, `registration`.
- **Care phases**: `care_phase_propose`.
- **Versioning F12**: `pack_entity_objects` (pack-on-GC delta worker;
  the F12.8 cold-tier service is invoked from the backend).
- **Notifications**: `dispatch_notification` (Arq cron every 5 min).
- **Smoke**: `ping`.

Most jobs ride the unified `Job` row pattern documented in
`../docs/DESIGN.md` §11 (idempotency key, per-user cap, progress
poll on `/api/jobs/{id}`); a handful of older consumers (`pack_volume`,
`reindex`) keep their own state tables and are kept that way per the
DESIGN doc §11.9 step 5.
