# API reference

Every HTTP route exposed by the FastAPI backend. Routes are grouped
by resource and each entry links to the handler module under
`backend/src/bvphoenix/api/`.

**Base URL** (dev): `http://localhost:8000`.
**All routes are mounted under `/api`** except the infra / discovery
endpoints in §1.

**Auth.** Routes that say "auth: optional" use the `optional_user`
dependency and still work for anonymous callers (filtered to public
resources). Routes that say "auth: required" use `require_user` and
return 401 without a Bearer token. Owner-only actions also require
`study.owner_subject_id == user.subject_id` (or `user.is_admin`).

**Live API docs.** The backend also serves `/docs` (Swagger UI) and
`/openapi.json` — always the source of truth if this reference falls
out of date.

---

## 1. Infra / discovery

Source: `backend/src/bvphoenix/main.py`.

| Method | Path | Auth | Response |
|--------|------|------|----------|
| `GET` | `/` | public | `{name, docs, health}` |
| `GET` | `/health` | public | `{status, version, env}` |
| `GET` | `/docs` | public | Swagger UI |
| `GET` | `/openapi.json` | public | OpenAPI schema |
| `GET` | `/.well-known/agent-card.json` | public | A2A Agent Card |

---

## 2. Auth — local password + JWT

Source: `backend/src/bvphoenix/api/auth.py`.

| Method | Path | Auth | Body / params | Response |
|--------|------|------|---------------|----------|
| `POST` | `/api/auth/register` | public | `{email, password, display_name}` | `201 {access_token, token_type}` |
| `POST` | `/api/auth/login` | public | `{email, password}` | `200 {access_token, token_type}` |
| `GET` | `/api/auth/me` | required | — | `200 {subject_id, email, display_name, is_admin}` |
| `GET` | `/api/me/scopes` | required | — | `200 {subject_id, is_admin, scopes: [...], agent_token_id?, is_agent}`. Caller introspection: GUI uses it to gate buttons (e.g. hide "Modifica fase" if `phases:write` missing); MCP agents use it to decide whether to attempt a write. Source: `backend/src/bvphoenix/api/me.py`. |

Errors: `409` on duplicate email, `401` on bad credentials.

Password hashing: bcrypt via `backend/src/bvphoenix/auth/passwords.py`.
Tokens are HS256 JWTs with expiry from `BVP_JWT_EXPIRES_SECONDS`
(default 24h).

---

## 3. Studies, series, instances, derivatives

Source: `backend/src/bvphoenix/api/studies.py`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/studies` | optional | Paginated list. Params: `limit` (1-200), `offset`, `modality`, `body_part`. Filtered by `visible_studies_filter`. |
| `GET` | `/api/studies/{study_id}` | optional | Study detail with inlined series. 404 if not visible. |
| `GET` | `/api/series/{series_id}` | optional | Single series. |
| `GET` | `/api/series/{series_id}/instances` | optional | Ordered list of instances in the series. |
| `GET` | `/api/series/{series_id}/thumbnail` | optional | JPEG of one slice. Params: `index` (0-based; default = middle), `wc_delta`, `ww_delta`. Requires `read:pixels`. |
| `GET` | `/api/series/{series_id}/volume.raw` | optional | Packed Float32 volume. Lazy-builds and caches a `Derivative` on first call. Requires `read:pixels`. |
| `POST` | `/api/series/{series_id}/pack-volume` | required | Enqueue an Arq job to pre-pack the volume. `202 {status, series_id}`. |
| `POST` | `/api/series/{series_id}/embed` | required | Enqueue an Arq job to compute the BiomedCLIP embedding. `202 {status, series_id}`. |
| `GET` | `/api/instances/{instance_id}/file` | optional | 307 redirect to a presigned S3 URL. Requires `download:dicom`. |
| `PATCH` | `/api/studies/{study_id}/tier` | required | Change contribution tier (T1-T4). Owner / admin only. Upgrades to T3/T4 auto-materialise a `TrainingConsent` row and enqueue `deidentify_reindex_study`; downgrades to T1/T2 close the active consent row with a `revoked_at` stamp. Body: `{tier}`. |
| `DELETE` | `/api/studies/{study_id}/training-consent` | required | Granular opt-out for a single study's training contribution. Downgrades T3/T4 → T2, clears `is_public`, and revokes the active `TrainingConsent` row (append-only: row stays, gets `revoked_at`). Optional body: `{reason}`. No-op when already private. Owner / admin only. |

**Volume byte layout (`volume.raw`):** 32-byte header followed by
Float32 voxels. Header: `uint32 nx, ny, nz; float32 sx, sy, sz;
float32 range_min, range_max`. Viewer parses this in one pass — see
`frontend/src/app/viewer/series/[id]/page.tsx`.

Response schemas: `StudyOut`, `StudyDetailOut`, `SeriesOut`,
`InstanceOut`, `PaginatedStudies` in
`backend/src/bvphoenix/api/_schemas.py`.

---

## 4. Search

Source: `backend/src/bvphoenix/api/search.py`,
`search_semantic.py`, `search_hybrid.py`, `search_chunks.py`. Full
architectural context in
[`search-and-embeddings.md`](./search-and-embeddings.md).

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/search` | optional | Full-text + structured filters. Params: `q` (max 128 chars), `modality`, `body_part`, `date_from`, `date_to`, `tag` (repeatable `namespace:value`), `limit` (1-200), `offset`, `sort` (`relevance|date_desc|date_asc|modality`). Uses tsvector GIN index + B-tree on `tags(namespace, value)`. `SET LOCAL statement_timeout='3s'`. |
| `GET` | `/api/similar-to/{target_id}` | optional | BiomedCLIP image-to-image similarity. `target_id` may be a series or study UUID. Params: `k` (1-100, default 10), `modality` optional filter. Returns `[{study, score, matched_series_id}]`. 404 if no embedding exists yet for the target. |
| `GET` | `/api/search/semantic` | optional | Text-to-anything via BiomedCLIP text encoder + pgvector HNSW. Params: `q`, `target_kind` (`series|report|consultation|document|...`), `k` (1-100), `modality`, `tag` (repeatable). Uses Redis query embedding cache (TTL 24h). Returns `[{target_kind, target_id, score, snippet?}]`. |
| `GET` | `/api/search/hybrid` | optional | Reciprocal Rank Fusion (k=60 default) of 3 rankers: tsvector `ts_rank`, BiomedCLIP text→image, sentence-transformer text→text. Params: same as `/api/search` + `weights=<t>,<c>,<s>` override. |
| `GET` | `/api/patients/{patient_id}/search` | required | Patient-scoped hybrid search across studies + reports + consultations + documents + annotations of the fascicolo. Params: mirror `/api/search/hybrid` minus patient filter. Source: `search_chunks.py`. |

**Planned updates to `/api/search` (S1):**

- `sort` param: `relevance|date_desc|date_asc|modality` (default
  `relevance` when `q` present, else `date_desc`).
- `facets` block in response, e.g.:
  ```json
  {
    "items": [...],
    "total": 123,
    "limit": 50,
    "offset": 0,
    "facets": {
      "modality": {"CT": 80, "MR": 43},
      "body_part": {"CHEST": 55, "ABDOMEN": 28},
      "tags": {"anatomy:lung": 40, "protocol:CT-chest": 30}
    }
  }
  ```
- `ts_rank` applied so results are ranked by textual relevance, not
  only `created_at desc`.

Response: `PaginatedStudies` (landed) or the shapes described above
for planned variants.

Example:

```bash
curl "http://localhost:8000/api/search?q=ground-glass&tag=anatomy:lung&modality=CT&limit=20"
```

---

## 5. Markers and clinical notes

> Section numbering skips §6 on purpose: the former §6 covered the
> legacy `Annotation` model and the `/api/annotations` +
> `/api/series/{id}/llm/describe` endpoints, which were retired when
> `Annotation` was split into `Marker` (in-viewer geometry) and
> `ClinicalNote` (human prose). Free-text clinical prose lives in
> `ClinicalNote`; geometric / in-viewer ephemera live in the unified
> `Marker` table; measurement persistence is a thin layer on top of
> `Marker` (kind=`measurement.<tool>`).

- Markers — `backend/src/bvphoenix/api/markers.py`. `GET / POST /
  PATCH / DELETE /api/patients/{id}/markers`. Filterable by
  `target_kind`, `target_id`, `kind`. JSON / DICOM SR import-export
  endpoints alongside.
- Measurements — `backend/src/bvphoenix/api/measurements.py`.
  `GET / POST /api/series/{id}/measurements`, `DELETE
  /api/measurements/{id}`, `GET /api/series/{id}/measurements.sr`.
  The wire ``payload`` shape is preserved for the viewer; storage is
  in `markers` rows (`target_kind='series'`, `kind='measurement.<tool>'`).
- Clinical notes — `backend/src/bvphoenix/api/clinical_notes.py`.
  `GET / POST / PATCH / DELETE /api/patients/{id}/notes`. Now carries
  an optional `anchor` JSONB (`{x, y, z}`) for viewer-pinned notes.

---

## 7. Reports

Source: `backend/src/bvphoenix/api/reports.py`. Reports are versioned
per study; version is auto-incremented.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/studies/{study_id}/reports` | optional | List reports ordered by version desc. Requires `read:annotations`. |
| `POST` | `/api/studies/{study_id}/reports` | required | Create a new report version. Multipart form: `text` and/or `file`. File is stored under `reports/{study_id}/{version}.{ext}` in `bucket_derivatives`. Requires `write:report`. |

---

## 8. Sharing — study share links and publish flags

Source: `backend/src/bvphoenix/api/sharing.py`. Model details in
[`sharing.md`](./sharing.md).

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/api/studies/{study_id}/share` | required (owner) | Create a share. Body: `{access_level (viewer|editor|manager), download, target (link_public|link_org|email|org), expires_in_hours, password, label, max_uses}`. Returns `ShareLinkOut`. |
| `GET` | `/api/studies/{study_id}/shares` | required (owner) | List shares for a study. |
| `DELETE` | `/api/share-links/{link_id}` | required (grantor) | Revoke the underlying grant. |
| `GET` | `/api/shared/{token}/info` | public | Preview info for a share link. Refuses expired / revoked / exhausted links. |
| `POST` | `/api/shared/{token}/verify` | public | Validate password if any, bump `use_count`, return a short-lived JWT. |
| `POST` | `/api/studies/{study_id}/publish` | required (owner) | Flip `is_public = true`. |
| `POST` | `/api/studies/{study_id}/unpublish` | required (owner) | Flip `is_public = false`. |

Access-level → verb mapping:
`backend/src/bvphoenix/services/access_levels.py`.

---

## 9. Folders

Source: `backend/src/bvphoenix/api/folders.py`. All folder routes
are under `/api/folders`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/api/folders` | required | Create folder. Body: `{name, parent_folder_id?}`. |
| `GET` | `/api/folders` | required | List folders owned by the caller. |
| `GET` | `/api/folders/{folder_id}` | required (owner) | Folder detail with items. |
| `POST` | `/api/folders/{folder_id}/items` | required (owner) | Add item. Body: `{resource_kind (study|series|folder), resource_id}`. |
| `DELETE` | `/api/folders/{folder_id}/items/{resource_kind}/{resource_id}` | required (owner) | Remove item. |
| `DELETE` | `/api/folders/{folder_id}` | required (owner) | Delete folder. |

Cascading grants on folders are described in the design docs; the
server does not yet expand folder grants to contained resources in
permission checks.

---

## 10. Patients — fascicolo

Source: `backend/src/bvphoenix/api/patients.py`. Full conceptual
model in [`fascicolo.md`](./fascicolo.md).

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/patients` | optional | Paginated list filtered by `visible_patients_filter`. Params: `limit`, `offset`, `q` (matches name / tax_id / external_id). Anonymous callers see nothing (patients are never public). |
| `POST` | `/api/patients` | required | Create a Health Record managed by the caller. Body: demographics + clinical fields. |
| `GET` | `/api/patients/{patient_id}` | optional | Patient detail. 404 if not visible. |
| `PATCH` | `/api/patients/{patient_id}` | required | Partial update. Requires manage rights on the patient. |
| `DELETE` | `/api/patients/{patient_id}` | required | Delete. Requires `delete` permission. |
| `GET` | `/api/patients/{patient_id}/index` | optional | Fascicolo section counts + last-updated dates + modality / document-type breakdowns. |
| `GET` | `/api/patients/{patient_id}/timeline` | optional | Unified timeline (studies + reports + annotations + documents). Params: `section` (optional filter), `limit`, `offset`. |
| `GET` | `/api/patients/{patient_id}/documents` | optional | List patient documents. Param: `type` optional. |
| `POST` | `/api/patients/{patient_id}/documents` | required | Upload a clinical document. Multipart: `title`, `document_type`, `text`, `document_date`, `file`. Requires `write:report` or `delete`. |
| `DELETE` | `/api/patients/{patient_id}/documents/{doc_id}` | required (owner) | Delete a patient document. |
| `POST` | `/api/patients/{patient_id}/share` | required (owner) | Create a patient-level share. Same shape as study share. |
| `GET` | `/api/patients/{patient_id}/shares` | required (owner) | List patient shares. |

Document types: `consent, discharge_letter, prescription, referral,
lab_result, er_report, clinical_note, personal_notebook, other` — see
`backend/src/bvphoenix/db/models/patients.py`.

---

## 10b. Care phases (semantic timeline)

Source: `backend/src/bvphoenix/api/care_phases.py`. Conceptual model
in [`fascicolo.md`](./fascicolo.md), full spec in
[`care-timeline-phases.md`](./care-timeline-phases.md).

All routes are nested under `/api/patients/{patient_id}/...`. Cross-
patient invariant: any `phase_id` or `event_id` that does not belong
to `{patient_id}` resolves to `404` (composite FK + scoped queries).
PATCH requires `If-Match` (`428` when missing, `412` on mismatch).
`apply-proposal` requires `Idempotency-Key` (`428` when missing).

### Read

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/patients/{patient_id}/care-phases` | required | List phases ordered by `ordinal`, with counts (`n_events`, `n_studies`, `n_documents`, `n_reports`). |
| `GET` | `/api/patients/{patient_id}/care-phases/{phase_id}` | required | Phase detail with assigned events. `404` if `phase_id` belongs to another patient. |
| `GET` | `/api/patients/{patient_id}/care-phases/{phase_id}/material` | required | Grouped material (studies, documents, reports, consultations, annotations). |
| `GET` | `/api/patients/{patient_id}/care-phases/{phase_id}/revisions` | required | Revision history (audit + restore points). |
| `GET` | `/api/patients/{patient_id}/care-timeline` | required | Bundle in one call. Params: `lang` (`it|en`, default `it`), `format` (`json|svg|markdown|ics|pdf`, default `json`), plus `theme` (`light|dark`) and `width` for SVG. SVG matches the local reference SVG (not committed). iCal (RFC 5545) emits one `VEVENT` per clinical event with deterministic `UID` and phase slug as `CATEGORIES`. **`format=pdf` returns `501`** until the `weasyprint` runtime dependency is bundled (the endpoint exists so the GUI / MCP have a stable name to call). |
| `GET` | `/api/patients/{patient_id}/care-timeline/health` | required | Diagnostics: `% events assigned`, `n_phases`, `last_classifier_run`, `last_proposal_id`. |

Response codes: `200` on success, `404` when the patient is not
visible or the id belongs to another patient.

### Mutations

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/api/patients/{patient_id}/care-phases` | required | Create a manual phase. Body: `{slug, name_i18n, kind, color_hex?, ordinal?, start_date?, end_date?, narrative_md?}`. `201` on create, `409` on duplicate `slug`. |
| `PATCH` | `/api/patients/{patient_id}/care-phases/{phase_id}` | required | Partial update. **`If-Match` mandatory.** Body: any subset of the create payload. `200` on success, `412` ETag mismatch, `428` missing `If-Match`. |
| `DELETE` | `/api/patients/{patient_id}/care-phases/{phase_id}` | required | Delete phase. Events keep their rows with `phase_id = NULL` (composite FK ON DELETE SET NULL). `204` on success. |
| `PUT` | `/api/patients/{patient_id}/care-phases/{phase_id}/events/{event_id}` | required | Assign an event to the phase. Body: `{confidence?: 0..1}`. `200` on success, `404` if phase or event is from another patient. |
| `DELETE` | `/api/patients/{patient_id}/care-phases/{phase_id}/events/{event_id}` | required | Unassign the event (sets `event.phase_id = NULL`). `204` on success. |
| `POST` | `/api/patients/{patient_id}/care-phases:propose` | required | Kick the LLM classifier. Query: `lang?: it|en`, `async?: bool` (default `false`). When `async=false` (default) the classifier runs in-request and returns the materialised `ProposePhasesOut`. When `async=true` an arq job (`propose_care_phases` in `workers/src/bvworkers/tasks/care_phase_propose.py`) is enqueued; the response carries `status="queued"` and a `job_id` to poll `/api/jobs/{job_id}`. |
| `POST` | `/api/patients/{patient_id}/care-phases:apply-proposal` | required | Atomic apply. **`Idempotency-Key` mandatory.** Body: `{proposal_id, accept_phases: [slug,...], accept_assignments: [{event_id, phase_slug},...]}`. `200` on apply, `409` on stale proposal, `428` missing `Idempotency-Key`. |
| `POST` | `/api/patients/{patient_id}/care-phases:reorder` | required | Batch ordinal update. Body: `{ordinals: [{phase_id, ordinal},...]}`. `200` on success. |
| `POST` | `/api/patients/{patient_id}/care-phases/{phase_id}/restore` | required | Restore a previous revision. Body: `{revision_no}`. `200` on success, `404` if revision belongs to another phase / patient. |

Schemas: `CarePhaseOut`, `CarePhaseDetailOut`, `CarePhaseMaterialOut`,
`CareTimelineOut`, `CarePhaseCreateIn`, `CarePhaseUpdateIn`,
`AssignPhaseIn`, `ProposePhasesOut`, `PhaseProposal`, `EventAssignment`,
`CarePhaseRevisionOut`, `EventTarget` (discriminated union) in
`backend/src/bvphoenix/api/_schemas_care_phase.py`.

---

## 11. A2A — Agent-to-Agent protocol

Source: `backend/src/bvphoenix/api/a2a.py`. Full protocol notes in
[`agent-protocols.md`](./agent-protocols.md).

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/.well-known/agent-card.json` | public | Agent Card (5 skills, security schemes, interface bindings). |
| `POST` | `/api/a2a` | optional | JSON-RPC 2.0 endpoint. Methods: `agent/sendMessage`, `agent/getTask`, `agent/listTasks`, `agent/cancelTask`, `agent/getAgentCard`. |

Skills dispatched from `agent/sendMessage`:

| Skill id | Backs onto |
|----------|------------|
| `dicom-search` | `visible_studies_filter` + full-text search |
| `similarity-search` | `api.search.similar_to` |
| `image-analysis` | `services.llm.describe_series` |
| `patient-fascicolo` | `api.patients.get_fascicolo_index` + timeline |
| `radiology-consultation` | similarity + description combined |

Task lifecycle: `submitted → working → (input-required)* → completed | failed | canceled`.
Task store: in-memory (`services/a2a_store.py`).

---

## 12. Tags

CRUD + aggregation for the `tags` table (source: `tags.py`). Also see
the v3.7.9 race fix on `PATCH /api/studies/{id}/tags` (H9: study-row
`SELECT ... FOR UPDATE` before tag set read).

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/tags` | optional | List tags. Params: `target_kind` (`study|series|instance|dataset`), `target_id`, `namespace` optional. Returns `[{id, namespace, value, target_kind, target_id, created_at}]`. |
| `POST` | `/api/tags` | required | Body: `{target_kind, target_id, namespace, value}`. Requires `write:annotations` on the parent study (if `target_kind != 'dataset'`). Conflict on `(target_kind, target_id, namespace, value)` unique. |
| `DELETE` | `/api/tags/{tag_id}` | required | Author or anyone with `write:annotations`. |
| `PATCH` | `/api/studies/{id}/tags` | required | Bulk tag set: `mode = add|replace|remove`, dry-run + `Idempotency-Key` supported. Preserves `auto` / `imported` rows; only touches `manual`. |
| `GET` | `/api/tags/tree` | optional | Aggregation by `namespace` with per-value counts, scoped to visible studies. Response: `{namespace: {value: count, ...}, ...}`. |
| `GET` | `/api/tags/autocomplete` | optional | Params: `q` (prefix, min 1 char), `namespace` optional, `limit` (1-50, default 10). Case-insensitive prefix match on `value`. Returns `[{namespace, value, count}]`. |

Example:

```bash
curl "http://localhost:8000/api/tags/autocomplete?q=lun&namespace=anatomy&limit=5"
```

---

## 13. Summaries

LLM-generated summaries at different scopes — study-level,
patient-level. Persisted for cacheing + audit. Source:
`backend/src/bvphoenix/api/summaries.py`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/summaries` | optional | List summaries. Params: `target_kind` (`study|patient|series`), `target_id`. Returns stored summaries with `model_id`, `created_at`. |
| `POST` | `/api/studies/{study_id}/summaries` | required | Generate a study summary via LLM. Body: `{style?, max_words?}`. Requires `run:llm`. Response: `{id, text, model_id, confidence, created_at}`. |
| `POST` | `/api/patients/{patient_id}/summaries` | required | Generate a patient-fascicolo summary. Body: `{style?, sections?}`. Requires `run:llm` on all included studies. |
| `DELETE` | `/api/summaries/{summary_id}` | required | Author or manager. |

---

## 14. LLM streaming

Server-sent events channel for token-by-token LLM output. Used by
the chat/describe UI for low-latency feedback. Source:
`backend/src/bvphoenix/api/llm_stream.py`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/api/llm/stream` | required | Body: `{prompt, target_kind?, target_id?, model_id?}`. Response: `text/event-stream` with `data: {delta}` frames and a final `data: {done: true, annotation_id?}`. Requires `run:llm` on the target. Closes on client disconnect. |

No non-streaming counterpart: the descriptor channel
(`/api/series/{id}/llm/describe` and `/api/annotations`) was retired
in favour of `ClinicalNote` for prose and `Marker` for geometry.

---

## 15. Embeddings admin

Ops endpoints to monitor and backfill the embedding pipeline. Source:
`backend/src/bvphoenix/api/embeddings_admin.py`.

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `GET` | `/api/embeddings/coverage` | required (admin) | Response: `{total_series, embedded, pending, failed, coverage_pct, per_model: {model_id: {embedded, coverage_pct}}}`. |
| `POST` | `/api/embeddings/retry-failed` | required (admin) | Re-enqueue failed embedding jobs. Params: `model_id` optional. Response: `{enqueued: n}`. |
| `POST` | `/api/embeddings/embed-missing` | required (admin) | Enqueue `embed_series` for every series without an embedding for the given model. Params: `model_id` optional, `limit` (1-10000). Response: `{enqueued: n}`. |

Already-landed per-series trigger: `POST /api/series/{id}/embed` (§3).

See [`search-and-embeddings.md`](./search-and-embeddings.md) §3-§8
for the model registry, HNSW tuning, and troubleshooting.

---

## 16. Data Use Committee + training licenses (admin)

Source: `backend/src/bvphoenix/api/duc.py`,
`backend/src/bvphoenix/services/training_licenses.py`.

The Data Use Committee (DUC) has a veto on every commercial training
licence deal. The review workflow and the signing action live under
``/api/admin/duc/*`` and are admin-only (``require_admin``).

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| `POST` | `/api/admin/duc/requests` | required (admin) | Open a review on a training licence. Body: `{license_id, summary}`. Flips the licence to `pending_duc`. 409 if a pending review already exists. |
| `GET` | `/api/admin/duc/requests` | required (admin) | List reviews. Param: `status` (defaults to all; commonly `pending`). |
| `POST` | `/api/admin/duc/requests/{request_id}/vote` | required (admin + DUC member) | Cast or replace a vote. Body: `{decision: approve\|reject\|abstain, rationale?}`. Triggers majority re-evaluation; flips the linked licence to `approved` / back to `draft` (on reject). |
| `POST` | `/api/admin/duc/licenses/{license_id}/sign` | required (admin) | Transition licence `approved` → `signed`. Refuses with 409 when the licence is not approved, has no `duc_request_id`, or the linked `DUCRequest.status` is not `approved`. Stamps `signed_at = now()`. |

Response: `RequestOut` for DUC request endpoints, `LicenseOut` for the
sign endpoint. Error body is standard FastAPI `{detail}`.

---

## 17. Rate limits, CORS, errors

- **CORS**: origins from `BVP_CORS_ORIGINS` (comma-separated). Empty
  in `BVP_ENV=development` = allow all (dev convenience); empty in
  `production` = deny (loud failure). See `main.py` and
  [`security-encryption-deidentify-cors.md`](./security-encryption-deidentify-cors.md).
- **Rate limiting**: live. Redis-backed `slowapi` limiter with
  per-endpoint budgets (login 5/min, register 3/min, share verify
  10/min, bulk upload 3/min, search 60/min, semantic 30/min,
  LLM 20/min, A2A 30/min) plus per-email progressive lockout after 5
  failed logins. See
  [`security-rate-limiting.md`](./security-rate-limiting.md) for the
  full table.
- **MCP HTTP rate limiting**: 50 req/s per token, 200 req/s per IP
  on `mcp.<host>/mcp` (env `BVP_MCP_RATE_LIMIT_PER_TOKEN` /
  `BVP_MCP_RATE_LIMIT_PER_IP`). See ADR 0019 and
  [`docs/agents-api/onboarding-mcp.md`](./agents-api/onboarding-mcp.md).
- **Errors**: standard FastAPI `HTTPException` with JSON `{detail}`,
  with RFC 9457 Problem Details on the write endpoints touched by
  the agents-API sprint (see ROADMAP Sprint 1). Read endpoints return
  `404` on permission failures to avoid leaking the existence of
  non-visible resources (see `studies.py` and friends). Write /
  download endpoints return `403` when the resource is visible but
  the action is not permitted. `412` on ETag mismatch, `428` when
  `If-Match` / `Idempotency-Key` is required and missing.
