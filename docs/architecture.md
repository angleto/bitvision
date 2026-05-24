# Architecture

Concrete view of how bitvision phoenix is wired today. For the design
rationale and roadmap see [`./DESIGN.md`](./DESIGN.md). For the
authoritative permission semantics see
[`authorization.md`](./authorization.md). For agent protocols see
[`agent-protocols.md`](./agent-protocols.md).

All file paths below are relative to the repository root.

---

## 1. Overall system

```
                     ┌──────────────────────────────────┐
                     │  Browser                         │
                     │  Next.js 15 (App Router) + React │
                     │  frontend/src/app                │
                     │  VolumeViewer / MPRLayout / etc. │
                     └────────────────┬─────────────────┘
                                      │ HTTPS (JSON + Float32 volumes)
                                      │
┌─────────────────────────────────────▼──────────────────────────────────┐
│  FastAPI backend                                                        │
│  backend/src/bvphoenix/main.py                                          │
│    ├── api/  (auth, studies, search, annotations, llm,                  │
│    │         sharing, folders, reports, patients, a2a)                  │
│    ├── auth/ (jwt, passwords, deps)                                     │
│    ├── services/ (permissions, access_levels, llm, thumbnails,          │
│    │              volumes, a2a_intent, a2a_store)                       │
│    ├── storage/ (s3.py — boto3 wrapper)                                 │
│    └── db/ (async SQLAlchemy + models/)                                 │
│                                                                         │
│  Exposes:  /api/*  (REST)                                               │
│            /api/a2a  (JSON-RPC 2.0, A2A protocol)                       │
│            /.well-known/agent-card.json  (A2A discovery)                │
│            /docs  (Swagger UI), /openapi.json  (OpenAPI schema)         │
└──┬────────────┬──────────────┬────────────────┬────────────────────────┘
   │            │              │                │
   │ SQL        │ Redis (arq)  │ S3 (boto3)     │ HTTP (LLM)
   │            │              │                │
   │            │              │                │
┌──▼───────────┐├─────────────┐├───────────────┐│ ┌─────────────────┐
│ PostgreSQL   ││ Redis 7     ││ MinIO / S3    ││ │ LLM provider     │
│  + pgvector  ││             ││  bucket_raw   ││ │  (stub | Anthro. │
│  schema via  ││  Arq        ││  bucket_deriv.││ │   via svc/llm.py)│
│  Alembic     ││  job queue  ││               ││ └─────────────────┘
└──────────────┘└─────────────┘└───────────────┘│
                ▲                                │
                │                                │
                │ enqueue_job("pack_volume"),    │
                │ enqueue_job("embed_series")    │
                │                                │
┌───────────────┴──────────────┐                 │
│ Arq worker                   │                 │
│ workers/src/bvworkers/main.py│─────────────────┘
│  tasks/pack_volume.py        │ (downloads DICOMs, stacks, uploads to S3,
│  tasks/embed_series.py       │  writes Derivative or Embedding rows)
│  tasks/ping.py               │
└──────────────────────────────┘

┌──────────────────────────────┐      ┌──────────────────────────────┐
│ MCP server                   │      │ External A2A agents          │
│ mcp/src/bvmcp/               │─────▶│ (doctor agents, etc.)        │
│  server.py     (stdio)       │ HTTP │ speak JSON-RPC 2.0 at /api/a2a│
│  server_http.py (HTTP, ADR   │      │ discover via Agent Card      │
│    0019; per-assistant bearer│      └──────────────────────────────┘
│    via /api/internal/agent-  │
│    bearer/resolve)           │
│  tools/ (read + write tool   │
│    families: patients,       │
│    studies, search, sharing, │
│    documents, care_phases,   │
│    segmentations, ...; see   │
│    docs/agent-protocols.md)  │
│  → calls backend REST        │
└──────────────────────────────┘

┌──────────────────────────────┐
│ Admin CLI                    │
│ crawler/src/bvcrawler        │  Pluggable connectors for public
│   cli.py + connectors/       │  archives (TCIA, OpenNeuro). Reuses
│                              │  the backend ingestion API.
└──────────────────────────────┘
```

**Composition.** Everything below Postgres/Redis/MinIO (the backend,
workers, and migrator) runs as images defined in
`infra/dockerfiles/`, orchestrated by `infra/docker-compose.yml`. The
frontend, MCP, and crawler typically run on the host in dev for
faster iteration — the Makefile has `make {backend,workers,frontend,
crawler,mcp}.dev` targets.

**Auth.** Local password + JWT (PyJWT 2.10+, claims
`iss`/`aud`/`iat`/`nbf`/`exp`/`jti`) is the default path
(`backend/src/bvphoenix/auth/tokens.py`); external OIDC SSO is
opt-in (`auth/oidc.py`, disabled when `BVP_OIDC_ISSUER` is empty).
AI assistants authenticate to MCP HTTP via per-assistant
`client_id`/`client_secret` pairs (see ADR 0019). Share-link tokens
go through `backend/src/bvphoenix/api/sharing.py` and mint short-
lived JWTs scoped to a grant. Browser sessions ride a
HttpOnly + Secure + SameSite=Lax `bvp_session` cookie minted by
`/api/auth/login*` and cleared by `/api/auth/logout` (post the
security pass 2026-05-21, see `docs/security-2026-05-21-assessment.md`).

**Anonymous-first.** The root FastAPI app and every listing endpoint
use `optional_user` — unauthenticated traffic still works, filtered
to `is_public = true` rows via
`services.permissions.visible_studies_filter`.

---

## 2. DICOM ingestion flow

Admin bootstrap / bulk import path (the user-driven upload path with
presigned URLs is on the roadmap; the CLI is what exists today).

```
┌──────────────────┐      ┌──────────────────────────────┐
│  folder of .dcm  │      │  bvphoenix-import CLI        │
│  files on host   │──▶──▶│  backend/src/bvphoenix/      │
│                  │      │    cli/import_dicom.py       │
└──────────────────┘      └──────────┬───────────────────┘
                                     │
                   ┌─────────────────┼─────────────────┐
                   │                 │                 │
                   ▼                 ▼                 ▼
        ┌──────────────────┐  ┌────────────┐  ┌────────────────┐
        │ pydicom parse    │  │ S3 upload  │  │ Postgres rows  │
        │ Study / Series / │  │ bucket:    │  │ Study, Series, │
        │ Instance UIDs    │  │ bvphoenix- │  │ Instance       │
        │ modality,        │  │ raw        │  │ (owner, tier,  │
        │ body_part, etc.  │  │            │  │  SHA256)       │
        └──────────────────┘  └────────────┘  └────────────────┘

After insert, on-demand derivatives are created lazily on first
request:

  GET /api/series/{id}/volume.raw
          │
          │  if no Derivative row yet:
          ▼
      services/volumes.py:pack_series()
          │
          ▼
      download all instances from S3 → stack to Float32 →
      upload to bucket_derivatives under volumes/<uid>.f32 →
      insert Derivative row → stream bytes back to client.

Or enqueue it eagerly:

  POST /api/series/{id}/pack-volume
          │
          ▼
      arq enqueue_job("pack_volume", series_id)
          │
          ▼
      worker downloads, packs, uploads, updates DB.
```

Key files:

- `backend/src/bvphoenix/cli/import_dicom.py` — bulk import.
- `backend/src/bvphoenix/services/volumes.py` — on-demand packing.
- `backend/src/bvphoenix/api/studies.py` — `GET /series/{id}/volume.raw`
  and `POST /series/{id}/pack-volume`.
- `workers/src/bvworkers/tasks/pack_volume.py` — worker-side packer.
- `backend/alembic/versions/0001_initial_schema.py` — creates
  `studies`, `series`, `instances`, `derivatives` tables.

---

## 3. Upload pipeline

The bulk upload endpoint `POST /api/upload/bulk` accepts arbitrary
file trees (a dragged CD folder, a ZIP, or a mix of DICOM + PDF +
images) and fans them out into the right pools. The web drag-drop and
STOW-RS endpoints in [`dicom-upload.md`](./dicom-upload.md) are a
DICOM-only subset of this pipeline; the user-facing guide lives in
[`cd-upload-guide.md`](./cd-upload-guide.md).

> **Status**: live. Endpoint, magic-byte classifier, DICOMDIR parser,
> `document_type` heuristic, and ISO 9660 walker are implemented and
> exercised by production imports (see CHANGELOG `3.0.0-beta.50` and
> the post-launch security hardening C6 / ZIP slip in
> `docs/security-2026-05-21-assessment.md`). DICOM-only uploads also
> have the endpoints in [`dicom-upload.md`](./dicom-upload.md).

```
┌──────────────────────────────────────────────────────────────────┐
│ Browser: UniversalUploader                                       │
│   drag CD_ROOT / folder / zip / loose files                      │
│   POST /api/upload/bulk   (multipart, one part per file          │
│                            + relative_paths + manual_override[]) │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ backend/src/bvphoenix/api/upload.py :: bulk_upload                │
│   1. Unzip any *.zip (recursive, in-memory)                       │
│   2. Detect DICOMDIR in the file list                             │
│   3. For each file → services.file_classifier.detect(bytes)       │
└───────────────────────────┬──────────────────────────────────────┘
                            │
          ┌─────────────────┼──────────────────────────┐
          │                 │                          │
          ▼                 ▼                          ▼
 ┌──────────────────┐ ┌────────────────────┐ ┌────────────────────┐
 │ classifier       │ │ dicomdir_parser    │ │ document_type_     │
 │ (magic bytes)    │ │ parses DICOMDIR    │ │ heuristic          │
 │                  │ │ → Patient/Study/   │ │ (filename regex →  │
 │ DICM  @128 → dcm │ │   Series/Image     │ │  consent / lab /   │
 │ %PDF  → pdf      │ │ maps each Image    │ │  discharge / …)    │
 │ JPEG/PNG/…       │ │ to a file in       │ │                    │
 │ ID3/ftyp/RIFF    │ │ relative_paths     │ │                    │
 │ PK    → zip      │ │                    │ │                    │
 └────────┬─────────┘ └─────────┬──────────┘ └─────────┬──────────┘
          │                     │                      │
          │   kind              │   DICOM bytes        │   doc_type
          ▼                     ▼                      ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ router (api/upload.py)                                            │
 │   DICOM   → services.dicom_ingest.DicomIngestor                   │
 │              → S3 bucket_raw + Study/Series/Instance rows         │
 │   PDF/img → services.documents → S3 bucket_docs +                 │
 │              PatientDocument row (document_type from heuristic    │
 │              or manual_override)                                  │
 │   text    → PatientDocument (kind=text, clinical_note)            │
 │   audio/  → PatientDocument (skipped today if >500 MB)            │
 │   video                                                           │
 │   unknown → skipped[] in the response                             │
 └───────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │ storage                                                           │
 │   Postgres: Study, Series, Instance, PatientDocument, FolderItem  │
 │   S3: bucket_raw (DICOM), bucket_docs (non-DICOM)                 │
 │   Folder "Studies" inside fascicolo (or target_folder_id)         │
 └──────────────────────────────────────────────────────────────────┘
```

Magic-byte reference (used by `services.file_classifier`):

| Format | Signature | Offset |
|--------|-----------|--------|
| DICOM Part-10 | `DICM` | 128 |
| PDF | `%PDF` | 0 |
| JPEG | `FF D8 FF` | 0 |
| PNG | `89 50 4E 47 0D 0A 1A 0A` | 0 |
| GIF | `GIF87a` / `GIF89a` | 0 |
| TIFF | `II*\0` / `MM\0*` | 0 |
| ZIP | `PK\x03\x04` | 0 |
| MP3 (ID3) | `ID3` | 0 |
| MP4 | `ftyp` | 4 |
| WAV | `RIFF` … `WAVE` | 0, 8 |

Key files:

- `backend/src/bvphoenix/api/upload.py` — `POST /api/upload/bulk`.
- `backend/src/bvphoenix/services/file_classifier.py` — magic-byte
  detection.
- `backend/src/bvphoenix/services/dicomdir_parser.py` — DICOMDIR
  Patient/Study/Series/Image walker; raises `DicomdirParseError`.
- `backend/src/bvphoenix/services/document_type_heuristic.py` —
  filename regex → `document_type`.
- `backend/src/bvphoenix/services/dicom_ingest.py` — DICOM-side
  ingestion (shared with `/api/dicom/studies` and `/api/dicom/stow-rs`).

---

## 4. Similarity search flow

```
┌──────────────────────────────────────────────────────────────────┐
│  UI: SimilarCasesPanelViewer.tsx (in viewer) or /search/visual    │
│      Calls GET /api/similar-to/{target_id}?k=10                   │
└──────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  backend/src/bvphoenix/api/search.py :: similar_to                │
│    1. Resolve source embedding row (series_id → Embedding)        │
│    2. Visibility check (permissions.can on source study)          │
│    3. pgvector cosine distance query (ORDER BY vector <=> ?)      │
│    4. Visibility-filter candidates (visible_studies_filter)       │
│    5. Return top-k with score = 1 - distance                      │
└──────────────────────────────────────────────────────────────────┘
                                 ▲
                                 │ reads Embedding rows
                                 │
┌──────────────────────────────────────────────────────────────────┐
│  Postgres: embeddings table (pgvector, dim=512)                   │
│    backend/src/bvphoenix/db/models/embeddings.py                  │
└──────────────────────────────────────────────────────────────────┘
                                 ▲
                                 │ Embedding rows inserted by worker
                                 │
┌──────────────────────────────────────────────────────────────────┐
│  Arq worker: workers/src/bvworkers/tasks/embed_series.py          │
│    Trigger: POST /api/series/{id}/embed → enqueue_job             │
│    Uses BiomedCLIP to embed the middle slice(s)                   │
│    Writes vector to embeddings table                              │
└──────────────────────────────────────────────────────────────────┘
```

Notes:

- Default embedding dim is 512 (BiomedCLIP). Changing dims requires
  either a new column/table or a schema migration — pgvector requires
  a fixed dim per column (see
  `backend/src/bvphoenix/db/models/embeddings.py`).
- The visibility filter runs *after* the distance query to keep the
  inner nearest-neighbor scan fast; the API fetches `k * 3` candidates
  and filters them down.

---

## 5. A2A task lifecycle

See [`agent-protocols.md`](./agent-protocols.md) for the full skill
catalogue. Runtime path:

```
┌───────────────────────────┐     Agent Card     ┌───────────────────────┐
│  External agent           │◀───────────────────│ GET /.well-known/     │
│  (doctor agent, etc.)     │                    │  agent-card.json      │
└────────────┬──────────────┘                    └───────────────────────┘
             │ JSON-RPC 2.0 over HTTPS
             ▼
┌────────────────────────────────────────────────────────────────────────┐
│  POST /api/a2a                                                         │
│  backend/src/bvphoenix/api/a2a.py :: a2a_endpoint                      │
│                                                                        │
│  method = agent/sendMessage                                            │
│    ▼                                                                   │
│  create-or-fetch Task in services.a2a_store (in-memory today)          │
│  mark status = "working"                                               │
│    ▼                                                                   │
│  services.a2a_intent.parse_intent(text)                                │
│    → LLM-based when configured, keyword fallback otherwise             │
│    → yields (skill_id, params)                                         │
│    ▼                                                                   │
│  _execute_skill(task, intent, …)                                       │
│    │                                                                   │
│    ├─ "dicom-search"          → /api/search pipeline                   │
│    ├─ "similarity-search"     → /api/similar-to/{id}                   │
│    ├─ "image-analysis"        → LLMProvider.describe_series (annotation)│
│    ├─ "patient-fascicolo"     → fascicolo index + timeline             │
│    └─ "radiology-consultation"→ similarity + description               │
│    ▼                                                                   │
│  mark status = "completed" | "failed" | "input-required"               │
│  append artifacts (text + data parts) to task                          │
│  save task, return to caller                                           │
└────────────────────────────────────────────────────────────────────────┘
```

Task states: `submitted → working → (input-required)* → completed | failed | canceled`.
See `TaskState` in `backend/src/bvphoenix/api/a2a.py`.

Methods supported today:

| Method | Handler | Notes |
|--------|---------|-------|
| `agent/sendMessage` | `_handle_send_message` | Start or continue a task |
| `agent/getTask` | inline | Fetch current task state |
| `agent/listTasks` | inline | List tasks (optionally by contextId) |
| `agent/cancelTask` | inline | Mark a task canceled |
| `agent/getAgentCard` | inline | Returns `AGENT_CARD` dict |

Task storage is Redis-backed
(`services/a2a_store.py`, key prefix `a2a:task:<id>`, 7-day TTL,
`BVP_REDIS_URL`), with a transparent in-process fallback when Redis is
unreachable so tests and single-process dev setups keep working.

---

## 6. Permission system

Source: `backend/src/bvphoenix/services/permissions.py`. Model:
`docs/authorization.md`. RLS will mirror this in a future migration.

```
            ┌────────────────────────────────┐
            │  subjects (id, kind, name)     │
            │  kind ∈ {user, org, group, …}  │
            └─────┬─────────┬──────────┬─────┘
                  │         │          │
         ┌────────▼─┐  ┌────▼──┐  ┌────▼────┐
         │  users   │  │ orgs  │  │ groups  │
         └──────────┘  └───┬───┘  └────┬────┘
                           │           │
                           └────┬──────┘
                                │ memberships (edges)
                                │
         ┌──────────────────────▼──────────────────────┐
         │  principal_set(user) = {user.subject_id}    │
         │      ∪ {parent_subject_id                   │
         │         FROM memberships                    │
         │         WHERE subject_id = user.subject_id} │
         └──────────────────────┬──────────────────────┘
                                │
                                ▼
                   ┌─────────────────────────┐
                   │  grants                 │
                   │  (resource_kind,        │
                   │   resource_id,          │
                   │   grantee_subject_id,   │
                   │   permissions[],        │
                   │   valid_from/until,     │
                   │   revoked_at)           │
                   └─────────────┬───────────┘
                                 │
         effective_permissions_on_study(db, user, study):
           = ALL_PERMS                       if user is owner/admin
           = PUBLIC_READ_PERMS               if study.is_public
           ∪ grants on (study, user ∈ principals, active)
           ∪ grants on (patient_id, user ∈ principals, active)

         can(db, user, action, study) = action ∈ effective_permissions
         visible_studies_filter(db, user) = Select(Study) WHERE (
           is_public OR owner OR in-grant OR patient-grant
         )
```

Roles / access levels: `backend/src/bvphoenix/services/access_levels.py`
maps Drive-style `viewer | editor | manager` names (and a `download`
toggle) to the permission verbs above — useful when the UI wants a
short bundle rather than picking verbs one by one.

Patient-level permissions: same machinery with its own `can_patient`
and `visible_patients_filter`. Grants on a patient cascade to the
patient's studies.

Share links: a share link is a public entry point to a grant. Token
verify (password optional) mints a short JWT with
`subject_id = PUBLIC_SUBJECT_ID`; the API sees it as an anonymous
caller, and the grant attached to the link (resolved via the link's
`grant_id`) is what actually enables access. See
`backend/src/bvphoenix/api/sharing.py` and
[`sharing.md`](./sharing.md).

---

## 7. Where things run

| Process | Command | Port |
|---------|---------|------|
| Postgres + pgvector | `docker compose up postgres` | 5432 |
| Redis | `docker compose up redis` | 6379 |
| MinIO (S3) | `docker compose up minio` | 9000 (S3), 9001 (console) |
| Alembic (one-shot) | `make db.migrate` | — |
| Backend API | `make backend.dev` | 8000 |
| Worker | `make workers.dev` | — (consumes Redis) |
| Frontend | `make frontend.dev` | 3000 |
| MCP server (stdio) | `make mcp.dev` | stdio |
| MCP server (HTTP, ADR 0019) | container `mcp-http` (prod) or `uv run --project mcp python -m bvmcp.server_http` (dev) | configurable, prod behind Ingress at `mcp.<host>/mcp` |
| Crawler CLI | `make crawler.dev` | — |

See `infra/docker-compose.yml` for the full orchestration and
[`development-guide.md`](./development-guide.md) for the per-service
setup.
