# bitvision phoenix — Documentation

Master index for every documentation page in this folder. Start here.

bitvision phoenix is a free, open-source medical imaging platform
(FastAPI + Next.js 15 + Arq workers + MCP + A2A) for uploading,
searching, viewing and annotating DICOM studies — with LLMs as
first-class citizens. See [`../README.md`](../README.md) for the
project overview and [`./DESIGN.md`](./DESIGN.md) for the design
rationale, roadmap, and open questions.

> **Reading note — migration numbering.** Several documents in this
> folder cite Alembic migration filenames like `0009_rls_policies`,
> `0035_versioning_schema`, `0042_marker_clinical_note_split`,
> `0068_assistant_credentials`, `0080_care_phase`, or `0088_patient_root_folder`.
> Those names are the **historical narrative** of how the schema
> grew. The OSS release of 2026-05-19 squashed the pre-release
> history into a single `0001_initial_schema.py` that materialises
> every table, index, RLS policy and CHECK constraint accumulated up
> to that point; post-rebase migrations restart from `0002`. The live
> file list is under `backend/alembic/versions/`; see
> [`data-model.md §9`](./data-model.md#9-migrations) for the policy
> on how to read those references. Cross-references to historical
> filenames are kept as provenance, not as pointers to live files.

---

## 1. Getting started

For a user running the stack for the first time.

- [`../README.md`](../README.md) — project overview, quick links,
  top-level `make` targets, service map.
- [`development-guide.md`](./development-guide.md) — prereqs,
  first-time setup, running the backend / frontend / workers / MCP in
  dev, tests, code style, adding migrations, where to look for what.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — contribution workflow,
  CLA pointer, commit conventions, mission statement.

---

## 2. Architecture

How the pieces fit together.

- [`architecture.md`](./architecture.md) — system architecture with
  ASCII diagrams: overall system, DICOM ingestion flow, similarity
  search flow, A2A task lifecycle, permission system. References
  actual file paths in the repo.
- [`./DESIGN.md`](./DESIGN.md) — the original design document:
  capabilities, principles, modalities, roadmap, open questions.

---

## 3. API reference

Every HTTP route the backend exposes.

- [`api-reference.md`](./api-reference.md) — full REST API reference
  (auth, studies, series, instances, search, measurements, sharing,
  folders, reports, patients, AI assistants, A2A). Each route links to
  its implementation in `backend/src/bvphoenix/api/`.
- [`search-and-embeddings.md`](./search-and-embeddings.md) — search
  architecture: tag-first cascade, embedding models (BiomedCLIP +
  planned sentence-transformers), RRF fusion, HNSW tuning,
  troubleshooting.
- OpenAPI JSON (live): `GET /openapi.json` on the running backend.
- Swagger UI (live): `/docs` on the running backend.

---

## 4. Viewer features

The browser viewer at `frontend/src/app/viewer/series/[id]/page.tsx`
plus the components in `frontend/src/components/`. The feature areas
below are implemented; see the viewer page for the full tool set.

- **3D volume rendering** — `components/VolumeViewer.tsx`. Multiple
  color presets (bone, grayscale, skin, tissue, muscle, ct-soft-tissue,
  ct-lung, ct-bone, mri-t1, mri-t2, cool-warm, hot, ice, spectral,
  jet, viridis, magma), blend modes (composite, MIP, MinIP, average),
  opacity shift, ray quality (sample distance), shading toggle,
  camera presets (anterior/posterior/left/right/superior/inferior),
  reset camera.
- **2D slice viewer** — `components/SliceViewer.tsx`. Interactive
  W/L (window center / window width) via mouse drag, W/L presets (CT
  Abdomen, CT Lung, CT Bone, CT Brain, CT Mediastinum, MRI Default),
  auto W/L, invert.
- **MPR (multi-planar reconstruction)** — `components/MPRLayout.tsx`
  + `components/MPRViewport.tsx`. Axial, sagittal, coronal viewports
  with synchronized crosshair.
- **Measurements** — `components/MeasurementOverlay.tsx`. Distance,
  angle, area, bounding-box, text. Persisted on the unified `Marker`
  table (kind=`measurement.<tool>`); the sidebar `MarkerListPanel`
  groups them by slice, scopes the visible rows to the active series
  + study (no leakage from sibling series), and offers per-row
  delete, jump-to, and edit/pin for clinical notes.
- **Hotkeys** — keyboard shortcuts for tools and navigation; see
  viewer page for the current mapping.
- **Hanging protocols** — viewport toggles for axial / sagittal /
  coronal / 3D with saved layouts.
- **Segmentations** — `components/SegmentationControls.tsx` offers
  segmentation overlay controls in the viewer sidebar.
- **Oblique / cinematic / fusion / fiducials** — called out as
  planned features in [`./DESIGN.md`](./DESIGN.md) but not yet in
  the current viewer code. No UI surface today.
- **Cine mode** — iterates through slices at a configurable speed.
- **Similar cases panel** — `components/SimilarCasesPanelViewer.tsx`
  calls `/api/similar-to/{target_id}` and lists nearest BiomedCLIP
  matches inline.
- **Marker list / clinical notes** — `components/MarkerListPanel.tsx`
  is the single sidebar surface for everything pinned to the study /
  active series: measurements, fiducials, text overlays, and clinical
  notes. Plain notes can be created inline; the legacy
  `AnnotationsPanel` (free-text descriptors + LLM `describe_series`)
  has been retired in favour of `ClinicalNote` for prose and `Marker`
  for geometry.

Viewer data loads via the packed Float32 volume endpoint
`GET /api/series/{id}/volume.raw` (see
[`api-reference.md`](./api-reference.md) for the header layout).

---

## 5. Agent protocols

How LLMs and external agents talk to bitvision.

- [`agent-protocols.md`](./agent-protocols.md) — MCP (Model Context
  Protocol, stdio + HTTP transports, read + write tool families
  registered in `mcp/src/bvmcp/tools/`) + A2A (Agent-to-Agent Protocol,
  Agent Card + JSON-RPC 2.0 task lifecycle). Includes tool catalogue,
  skills, and the fallback intent-routing logic.
- [`ai-assistants.md`](./ai-assistants.md) — per-user AI assistant
  model: identity (label, provider, model_id, permissions) ↔ active
  bearer token ↔ N–N patient share list. Powers the
  `/settings/ai-assistants` page and the "Share with AI" modal on
  the Health Record.

---

## 6. Upload and web access

- [`./DESIGN.md`](./DESIGN.md) §5 — ingestion pipeline (presigned
  S3 multipart → Arq worker → DB).
- [`dicom-upload.md`](./dicom-upload.md) — web drag-drop and
  DICOMweb STOW-RS for pure DICOM payloads.
- [`cd-upload-guide.md`](./cd-upload-guide.md) — CD/DVD ingest with
  DICOMDIR + ZIP + folder + mixed DICOM/PDF/image classification.
- [`owner-scoped-uids.md`](./owner-scoped-uids.md) — DICOM UIDs
  (StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID / DICOM
  PatientID) are scoped to the *owning subject* in the database and
  in S3 keys, because real-world UIDs are not globally unique.
- ISO 9660 / Joliet support in the upload widget: drop an ``.iso``
  file (radiology DVD) on `UniversalUploader` and it is parsed
  client-side via `frontend/src/lib/iso9660.ts`; the member files
  flow through the regular bulk-upload pipeline (DICOMDIR + magic
  byte detection + PDF report classification all apply).
- `backend/src/bvphoenix/cli/import_dicom.py` — the
  `bvphoenix-import` CLI used by `make import DIR=... OWNER=...`. The
  same code runs in dev bootstrap and in production bulk import.
- Web access: `frontend/src/app/` with routes under `login`,
  `register`, `studies`, `patients`, `search/visual`, `shared/[token]`,
  `viewer/series/[id]`. Auth state in `frontend/src/lib/auth-context.tsx`.
- [`sharing.md`](./sharing.md) — link-based sharing UX and data model.

---

## 7. Security

- [`authorization.md`](./authorization.md) — ownership,
  capability-based ACL (grants), organization scoping, marketplace,
  RLS policies. Canonical reference for the permission model.
- [`security-rls.md`](./security-rls.md) — PostgreSQL Row-Level
  Security: helper functions, bypass strategy, per-table policies,
  and the **production hardening note** (owner-bypass requires either
  FORCE RLS or a non-owner app role).
- [`security-audit-log.md`](./security-audit-log.md) — append-only
  audit log: action taxonomy (auth, sharing, studies, patients,
  reports, A2A, MFA, **versioning proposals + consultations + GDPR**),
  the source-pinned coverage test that prevents silent drift.
- [`security-gdpr.md`](./security-gdpr.md) — GDPR posture, erasure
  scopes, **versioned-content tombstoning** (entity_objects scrub
  with cross-patient dedup safety, F12.7).
- [`security-encryption-deidentify-cors.md`](./security-encryption-deidentify-cors.md)
  — S3 SSE, DICOM Basic-Profile de-identification on share links,
  **Italian free-text de-identification** (CF / email / phone /
  date / address regex matrix used by OpenData publish), CORS / HSTS
  / TrustedHost.
- [`sharing.md`](./sharing.md) — share link UX, short-lived JWTs
  scoped to a grant, audit trail. Auth chain validates revoked /
  expired / not-yet-valid grants.
- `backend/src/bvphoenix/services/permissions.py` — application-side
  permission resolution (grants + memberships + public flag).
- `backend/src/bvphoenix/db/models/audit.py` — append-only audit log.
- [`./DESIGN.md`](./DESIGN.md) §2 — GDPR posture (sanitari as
  special category, k-anonymity ≥ 5 for T3 aggregates, right to
  erasure, EU residency optional).
- Encryption at rest: Postgres + S3 rely on the underlying provider
  (e.g. Cloudflare R2 SSE-S3); in-transit TLS is enforced at the
  ingress layer (out of scope for this repo).
- Severe-test catalogue: see
  [`development-guide.md §"Severe-test suites"`](./development-guide.md)
  for the four backend test files that pin security invariants.

---

## 8. Data model

- [`data-model.md`](./data-model.md) — DB schema walkthrough with an
  ER-style ASCII diagram. Links to each SQLAlchemy model file under
  `backend/src/bvphoenix/db/models/`.
- [`fascicolo.md`](./fascicolo.md) — Health Record
  (fascicolo, FSE 2.0 inspired) — data model, sections, timeline.
- Embeddings: `backend/src/bvphoenix/db/models/embeddings.py` —
  BiomedCLIP vectors (`pgvector`, 512 dims).
- Alembic migrations: `backend/alembic/versions/`.

---

## 9. Development guide

- [`development-guide.md`](./development-guide.md) — the contributor
  quick reference: prereqs, setup, running each service, tests, code
  style, commit convention, adding migrations, subfolder map.
- [`i18n.md`](./i18n.md) — bilingual UI (EN / IT) contract: stack
  (`next-intl`), catalogue conventions, migration pattern per file,
  the per-batch TODO list for the long tail of untranslated
  components.

---

## Directory map

Where each subsystem lives in the repo. Full details in
[`architecture.md`](./architecture.md) and
[`development-guide.md`](./development-guide.md).

| Path | Role |
|------|------|
| `backend/` | FastAPI REST API |
| `workers/` | Arq async workers (pack volume, embed series) |
| `mcp/` | Native MCP server (stdio + HTTP, ADR 0019; registry under `mcp/src/bvmcp/tools/`) |
| `crawler/` | Admin CLI for public DICOM archives |
| `frontend/` | Next.js 15 + React viewer |
| `infra/` | docker-compose + Dockerfiles |
| `docs/` | This documentation set |
