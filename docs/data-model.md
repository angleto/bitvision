# Data model

Walkthrough of the PostgreSQL schema that backs bitvision phoenix.
The authoritative source is the SQLAlchemy ORM under
`backend/src/bvphoenix/db/models/` + Alembic migrations under
`backend/alembic/versions/`. For the permission semantics see
[`authorization.md`](./authorization.md); for the Health Record see
[`fascicolo.md`](./fascicolo.md).

---

## 0. Conceptual overview: the three-axis model

A patient's Health Record (Fascicolo) sits on **three orthogonal axes** plus one cross-cutting dimension. Knowing which axis a question lives on tells you which table to look at and which write tool to reach for; conflating axes is the single most common source of confusion (for both humans and LLM agents).

### 0.1 The three axes plus cross-cutting

- **Storage axis (`Document`)**: the held artefact. PDF, scanned page, label exported by a DVD burner, inline note. Answers "what arrived, in what form?".
- **Temporal axis**: the patient's clinical history on a line. Two granularities:
  - atomic level: `ClinicalEvent`, one row per timeline item (visit, surgery, imaging study, lab batch, ...);
  - grouping level: `CarePhase`, a semantic wrapper over one or more events ("Diagnosi", "Chirurgia", "Sorveglianza").
- **Organisational axis (`Folder`)**: a Drive-style container the user (or an agent on their behalf) builds to navigate the record. Cosmetic. No clinical semantics. Sub-folders, polymorphic items.
- **Cross-cutting (`Tag`)**: flat `namespace:value` labels for search and filtering. Today imaging-scoped by schema (study / series / instance / dataset).

**Canonical relationship**: `CarePhase ⊃ ClinicalEvent`; `Folder` and `Document` are orthogonal to both; `Tag` labels imaging targets and is orthogonal to all three.

### 0.2 Axes at a glance

| Axis label | Backing tables | What it is | What it is not | Created by | Cross-cutting? |
|---|---|---|---|---|---|
| **Storage axis (`Document`)** | `documents` | The held artefact attached to a Health Record (PDF / scan / note / label). | Not a timeline event. Not a folder. Not searchable via `Tag` (today). | Upload pipeline; manual via `ingest_document`; agent-extracted attachments. | No |
| **Temporal axis, atomic level (`ClinicalEvent`)** | `clinical_events` | A single event in the patient timeline (kind ∈ {`imaging_study`, `surgical_procedure`, `outpatient_visit`, `inpatient_admission`, `lab_batch`, `consultation_event`, `pathology_review`, `mdt_meeting`, `cardio_diagnostic`, `endoscopy`, `other`}). | Not a folder. Not a phase wrapper. Not the document that describes it. | DICOM ingest (`imaging_study`), or `create_clinical_event` (everything else). | No |
| **Temporal axis, grouping level (`CarePhase`)** | `care_phase` | Semantic wrapper over one or more `ClinicalEvent` rows ("Diagnosi", "Chirurgia", "Follow-up"). | Not a folder. Not an event. Does not own documents directly. | LLM classifier via `propose_care_phases` + `apply_phase_proposal`, or manual `create_care_phase`. | No |
| **Organisational axis (`Folder`)** | `folders`, `folder_items` | Drive-style container for documents and links, no clinical semantics, no place on the timeline. | Not a phase. Not an event. Not a tag. | User or agent via `create_folder`. Grants cascade to items. | No |
| **Cross-cutting (`Tag`)** | `tags`, `tag_aliases` | Flat `namespace:value` labels (`anatomy:liver`, `modality:CT`, `finding:nodule`). | Not a folder. Not an event. Not on documents today. | `add_tag_to_study`, `replace_study_tags`, autotag worker. Schema-bound by `ck_tags_target_kind`. | Yes |

### 0.3 Orthogonality and containment

```
                                         ┌──────────────────────┐
       Storage axis                       │                      │
       ┌─────────────┐                    │   Cross-cutting      │
       │  Document   │                    │       Tag            │
       │ (PDF, DCM,  │                    │  namespace:value     │
       │  label, …)  │                    │  on study / series / │
       └──────┬──────┘                    │  instance / dataset  │
              │                           │                      │
   describes  │ (via                      └──────────┬───────────┘
              │ ContentDocumentLink)                 │ labels
              │                                      │
              ▼                                      ▼
   Temporal axis                            DICOM imaging chain
   ┌─────────────────────────────────┐      (study → series → instance)
   │           CarePhase             │
   │   ┌──────────────┐ ┌────────┐   │      Organisational axis
   │   │ ClinicalEvent│ │ Clin.  │   │      ┌────────────────┐
   │   │   (atomic)   │ │ Event  │   │      │     Folder     │
   │   └──────────────┘ └────────┘   │      │ ┌────┐ ┌────┐  │
   │                                 │      │ │item│ │sub │  │
   │   CarePhase ⊃ ClinicalEvent     │      │ │    │ │fold│  │
   └─────────────────────────────────┘      │ └────┘ └────┘  │
                                            │ (heterogeneous │
                                            │  membership)   │
                                            └────────────────┘
```

The four boxes share patients but no FK among themselves on the orthogonal arrows. `ContentDocumentLink` is the only join across axes (Storage ↔ Temporal); `FolderItem` references all kinds polymorphically without claiming clinical meaning.

### 0.4 Decision rules for agents and contributors

When a fact about a patient arrives, ask which axis it lands on:

- **A 2024 study** the user wants to find later → put it on the `Folder` "2024" (organisational axis).
- **The surgery on 2024-07-29** → record a `ClinicalEvent` with `kind=surgical_procedure` (temporal axis, atomic).
- **A whole "post-op surveillance" stretch from October 2024 to March 2026** → wrap the relevant events in a `CarePhase` (temporal axis, grouping).
- **`anatomy:liver` so the liver studies show up in a search** → add a `Tag` (cross-cutting) on the imaging targets.
- **The PDF you got from the radiologist** → that is a `Document` (storage). Link it to the matching `ClinicalEvent` via `ContentDocumentLink` so the temporal axis can show it; drop it into a `Folder` if the user wants a curated view.

A request that says "organise the record into phases" is a temporal-axis task and should not create folders. A request that says "put all 2024 studies into one folder" is an organisational-axis task and should not touch `CarePhase`.

### 0.5 Important non-properties (negative space)

- **`Tag` is imaging-only by design, not by accident.** The CHECK constraint `ck_tags_target_kind` (in `backend/src/bvphoenix/db/models/annotations.py:91`) limits `target_kind` to `study`, `series`, `instance`, `dataset`. Documents, ClinicalEvents, CarePhases, ReportContent rows, Markers, ClinicalNotes, and Folders are not Tag targets. Tags fit imaging because the vocabulary is open and the source of truth is the artefact itself; on every other entity a tag tends to duplicate information already held more rigorously elsewhere (enum column, FK, structured ReportContent), creating a second source of truth that drifts. Before proposing to widen the constraint, model the need as a structured field or a dedicated entity. See [`tag-taxonomy.md` §"Before proposing to widen the constraint"](./tag-taxonomy.md#before-proposing-to-widen-the-constraint-model-the-need-first) for the checklist and the canonical alternatives (`incident_anchor`, `LegalHold`, `Trial` / `TrialEnrollment`, `ReviewRequest`).
- **`CarePhase` does not own documents directly.** Documents attach to events (via `ContentDocumentLink`); the phase wraps the events.
- **`Folder` grants cascade to items; `ClinicalEvent` does not.** Sharing a folder shares its members; sharing a phase does not share its events. Permissions are an axis-aware concept.
- **`Report` and `Annotation` are retired entity names.** `Report` was replaced by `ReportContent` in v3; `Annotation` was split into `Marker` (in-viewer ephemera) and `ClinicalNote` (human prose) in migration 0042. Cross-references in older specs may still mention the retired names; treat them as shorthand for the v3 successors.
- **Storage isolation is axis-respecting too.** No bucket names, presigned URLs, or S3/SQL errors leak into responses regardless of axis (REST, MCP, share links).

### 0.6 Where to read more

- Temporal axis details: [`care-timeline-phases.md`](./care-timeline-phases.md).
- Organisational axis UX: [`fascicolo-drive-ux.md`](./fascicolo-drive-ux.md).
- Cross-cutting tags and namespaces: [`tag-taxonomy.md`](./tag-taxonomy.md).
- Health Record sectioning, permissions and timeline assembly: [`fascicolo.md`](./fascicolo.md).

The remainder of this document drills into each table individually; the §1 ER-style overview shows the joins between them.

---

## 1. ER-style overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          subjects                                   │
│  (kind ∈ {user, org, group, public})                                │
└────┬──────────────┬────────────────┬───────────────┬────────────────┘
     │              │                │               │
     │ 1:1          │ 1:1            │ 1:1           │ edges in
     ▼              ▼                ▼               │ memberships
┌─────────┐    ┌─────────┐     ┌─────────┐           │
│ users   │    │   orgs  │     │ groups  │◀──────────┘
└───┬─────┘    └─────────┘     └─────────┘
    │                                ▲
    │ 1:N owned / managed            │ parent_org_subject_id
    ▼                                │
┌──────────┐                         │
│ patients │◀─────────────┐          │
└────┬─────┘              │          │
     │ 1:N                │          │
     ▼                    │          │
┌──────────┐              │ grants on patient/study
│ studies  │──────────────┤          │
│          │              │          │
└────┬─────┘           ┌──┴────┐     │
     │ 1:N             │ grants│─────┘  (resource_kind,resource_id,grantee)
     ▼                 └──┬────┘
┌──────────┐              │
│ series   │              │ 1:1?
└────┬─────┘              ▼
     │ 1:N         ┌──────────────┐
     ▼             │ share_links  │
┌──────────┐       │ (grant_id)   │
│instances │       └──────────────┘
└──────────┘
     │ 1:N
     ├──▶ ┌──────────────┐
     │    │ derivatives  │   (per series: packed volume, thumbnails, …)
     │    └──────────────┘
     │
     ├──▶ ┌──────────────┐
     │    │ embeddings   │   (pgvector(512), target_kind+target_id+model_id)
     │    └──────────────┘
     │
     ├──▶ ┌──────────────┐
     │    │ annotations  │   (target_kind+target_id, source ∈ {human,llm})
     │    └──────────────┘
     │
     └──▶ ┌──────────────┐
          │ tags         │   (namespace:value, on study/series/instance/dataset)
          └──────────────┘

Study-scoped:
    reports (study_id, version)        — versioned text + file report
    folders / folder_items             — user-owned hierarchies

Patient-scoped:
    patient_documents (patient_id, document_type, text|file)

Cross-cutting:
    audit_log (actor_subject_id, action, resource_kind, resource_id, …)
```

---

## 2. Principals — Subject, User, Organization, Group, Membership

Source: `backend/src/bvphoenix/db/models/principals.py`.

### `subjects`

Base table. Every entity that can hold a permission has a row here.
Keeps foreign keys in `grants`, `memberships`, `audit_log` uniform.

Columns: `id UUID PK`, `kind ENUM('user','org','group','public')`,
`display_name`, `created_at`, `updated_at`.

Special row: `PUBLIC_SUBJECT_ID = 00000000-0000-0000-0000-000000000001`
represents unauthenticated visitors (used as the grantee for
link-based shares). See `db/models/sharing.py`.

### `users`

Columns: `subject_id UUID PK FK→subjects`, `email UNIQUE`,
`oidc_subject UNIQUE NULL`, `password_hash TEXT NULL` (bcrypt, null
for OIDC-only accounts), `is_admin BOOLEAN`.

### `organizations`

`subject_id PK FK→subjects`, `slug UNIQUE`, `kind`, `homepage_url`.

### `groups`

`subject_id PK FK→subjects`, `parent_org_subject_id FK→subjects`,
`slug UNIQUE per parent`.

### `memberships`

Edge table: `(subject_id, parent_subject_id, role)` where role is one
of `admin | member | viewer | nested` (see check constraint). Unique
per edge.

Used by `services.permissions.principal_set()` to compute the set of
subject ids a user effectively acts as.

---

## 3. Patients and documents

Source: `backend/src/bvphoenix/db/models/patients.py`.

### `patients`

A patient is not necessarily a user. Most patients are "managed" by
a doctor or org; some are self-owned (a user managing their own
record).

Columns:

- `id UUID PK`
- `managed_by_subject_id UUID NULL FK→subjects`
- `self_user_subject_id UUID NULL FK→users.subject_id UNIQUE`
- `display_name`, `external_id`
- Extended demographics: `birth_date`, `sex (char 1)`, `tax_id`,
  `phone`, `email`, `address`, `blood_type`, `allergies`, `notes`
- Unique partial index on `tax_id WHERE tax_id IS NOT NULL`

### `patient_documents`

Standalone clinical documents attached to a patient (consents,
prescriptions, discharge letters, etc.). FSE 2.0-inspired types.

Columns: `id`, `patient_id FK→patients (CASCADE)`,
`uploaded_by_subject_id`, `document_type` (CHECK constraint — see
`PATIENT_DOCUMENT_TYPES`), `title`, `text`, `file_s3_key`,
`file_content_type`, `document_date`.

Types: `consent, discharge_letter, prescription, referral, lab_result,
er_report, clinical_note, personal_notebook, other`.

Details: [`fascicolo.md`](./fascicolo.md).

---

## 4. DICOM — Study, Series, Instance, Derivative

Source: `backend/src/bvphoenix/db/models/dicom.py`.

### `studies`

The primary ownership unit.

Columns of note:

- `study_instance_uid UNIQUE` (DICOM UID)
- `patient_id NULL FK→patients (SET NULL)`
- `owner_subject_id FK→subjects (RESTRICT)` — individual owner
- `owner_org_subject_id NULL FK→subjects (SET NULL)` — optional co-owning org
- `contribution_tier ENUM('t1','t2','t3','t4')` (default `t1`)
- `is_public BOOLEAN`
- `is_listed_for_sale BOOLEAN`
- `ingestion_complete BOOLEAN`
- `study_description TEXT`, `study_date DATE`
- `modalities TEXT[]`
- Indexes on `is_public`, `contribution_tier`

### `series`

The logical unit of sharing, annotation, embedding, LLM jobs.

Columns: `id`, `study_id FK→studies (CASCADE)`,
`series_instance_uid UNIQUE`, `series_number`, `modality`,
`body_part_examined`, `series_description`,
`expected_instance_count`, `received_instance_count`,
`ingestion_complete`.

### `instances`

Raw `.dcm` blobs in S3.

Columns: `id`, `series_id FK→series (CASCADE)`,
`sop_instance_uid UNIQUE`, `sop_class_uid`, `instance_number`,
`s3_bucket`, `s3_key`, `size_bytes`, `content_sha256`.

### `derivatives`

Generated artefacts — packed Float32 volumes, thumbnails, MPR cache,
tile pyramids.

Columns: `id`, `series_id FK (CASCADE)`, `kind` (free-form string),
`format`, `s3_bucket`, `s3_key`, `size_bytes`, `generator_version`.
Unique on `(series_id, kind, format)`.

Kinds in use today:

- `kind='volume_f32'`, `format='raw'` — see `services/volumes.py`.
- Thumbnails are currently generated on-demand and not cached as
  Derivative rows (see `services/thumbnails.py`).

---

## 5. Annotations, Reports, Tags

Source: `backend/src/bvphoenix/db/models/annotations.py`.

### `annotations`

Columns:

- `id`, `target_kind` (study|series|instance), `target_id`
- `author_subject_id NULL FK→subjects`
- `source` (human|llm) — CHECK: `source='human'` iff author is
  non-null; `source='llm'` iff `model_id` is non-null
- `model_id`, `confidence`, `reviewed_by_human`
- `kind` (free-form, e.g. `description`, `bbox`, `segmentation`)
- `payload JSONB`
- `created_at`, `updated_at`
- Indexes on `(target_kind, target_id)` and `source`

### `reports`

Versioned text (+ optional uploaded file) per study.

Columns: `id`, `study_id FK (CASCADE)`, `author_subject_id`,
`version INT`, `text`, `file_s3_key`, `file_content_type`.
Unique on `(study_id, version)`.

### `tags`

Flat `namespace:value` tags on study / series / instance / dataset.

Unique on `(target_kind, target_id, namespace, value)`.
GIN/btree indexes on `(namespace, value)` and `(target_kind, target_id)`.

---

## 6. Embeddings (pgvector)

Source: `backend/src/bvphoenix/db/models/embeddings.py`.

### `embeddings`

Columns:

- `id`, `target_kind` (study|series|instance), `target_id`
- `model_id VARCHAR(128)` — e.g. `biomedclip-pubmedbert-256`
- `vector Vector(512)` — pgvector column
- `created_at`
- Unique on `(target_kind, target_id, model_id)`
- Index on `(target_kind, target_id)`

`EMBEDDING_DIM = 512` is fixed — pgvector requires a fixed dim per
column. Different-dim models live in their own table/column when
introduced.

Similarity search uses `vector.cosine_distance(q) = vector <=> q` via
the SQLAlchemy helper — see `api/search.py::similar_to`.

---

## 7. Sharing — Grant, ShareLink, Folder

### `grants`

Source: `backend/src/bvphoenix/db/models/grants.py`.

Atomic capability-based ACL record.

Columns:

- `id`, `resource_kind` (study|series|instance|annotation|dataset|patient)
- `resource_id`
- `grantor_subject_id FK (RESTRICT)`
- `grantee_subject_id FK (CASCADE)`
- `parent_grant_id FK→grants (CASCADE)` — for delegated grants
- `permissions TEXT[]` — list of verb strings (`read:metadata`, `write:annotations`, …)
- `conditions JSONB` — e.g. `{"scope": "link_public"}`, `{"purpose": "consultation"}`
- `valid_from`, `valid_until`
- `revoked_at`, `revoked_by_subject_id`
- `is_commercial BOOLEAN`
- `purpose TEXT`
- Indexes on `(grantee, resource_kind, resource_id)` and `(resource_kind, resource_id)`

### `share_links`

Source: `backend/src/bvphoenix/db/models/sharing.py`.

Public entry point to a grant — a URL-safe token plus optional
password and usage counter.

Columns: `id`, `grant_id FK→grants (CASCADE)`, `token VARCHAR(64) UNIQUE`,
`password_hash TEXT NULL`, `label TEXT`, `max_uses INT NULL`,
`use_count INT`, `created_at`.

### `folders` / `folder_items`

Source: `backend/src/bvphoenix/db/models/folders.py`.

Google Drive-style hierarchical grouping.

`folders`: `id`, `name`, `owner_subject_id FK→subjects (CASCADE)`,
`parent_folder_id NULL FK→folders (CASCADE)`.

`folder_items`: composite PK `(folder_id, resource_kind, resource_id)`;
`resource_kind` CHECK in `('study','series','folder')`.

Cascade of grants from folder to contents is designed but not
implemented in the permission check (see
`services.permissions.visible_studies_filter`).

### Access levels

Source: `backend/src/bvphoenix/services/access_levels.py`.

Named bundles mapped to permission verbs. Used by the share endpoints
to avoid picking verbs one by one:

- `viewer` → `{read:metadata, read:pixels, read:annotations}`
- `editor` → viewer + `{write:annotations, write:report, run:llm}`
- `manager` → editor + `{share, share:delegate, transfer:ownership}`
- `download` toggle → +`{download:dicom, download:derivative}`

---

## 7.5. Versioning git-like (F12)

Source: `backend/src/bvphoenix/db/models/versioning.py`. Full doc at
[`versioning.md`](./versioning.md). Eight tables that replicate git's
internals in PostgreSQL so every save on a fascicolo is a tracked
commit, branches isolate consultations, and pull-request review wires
into the existing `consultations` lifecycle.

### `entity_objects`

Content-addressed payload. PK is the 32-byte BYTEA `object_hash =
sha256(canonical_json_bytes)` (RFC 8785 / JCS). Columns:
`entity_kind` (one of patient/study/series/report/annotation/tag/
clinical_note/patient_document/consultation/summary/measurement/
segmentation/_tree_), `schema_version`, `payload JSONB`,
`payload_size`, plus pack lifecycle (`storage_kind ∈ full|delta`,
`delta_parent_hash`, `delta_bytes`) and GDPR tombstoning
(`is_tombstoned`, `tombstoned_at`, `tombstoned_reason`). Initial writes
are always `storage_kind='full'`; the F12.6 pack worker rewrites long
chains as deltas.

### `commits`

Anchor of the DAG. PK is 32-byte BYTEA `commit_hash`. Columns:
`patient_id` (the repo), `tree_hash`, `parent_hashes BYTEA[]` (0-2),
author + AI provenance (`author_subject_id`, `author_kind ∈
human|agent|system`, `model_id`, `provider`, `agent_token_id`),
`message`, `db_txid` for forensic correlation, `branch_at_creation`
debug hint. GIN index on `parent_hashes` for ancestor walks.

### `manifest_entries`

Exploded manifest, one row per `(commit_hash, entity_kind, entity_id)
→ object_hash`. HASH-partitioned on `commit_hash` over 16 buckets.
Read O(1) for "state at a commit" without recursive walks.

### `refs`

Branch heads `(patient_id, ref_name) → commit_hash`. Two valid
ref_name patterns: `main` (always) and `consultation/<consultation_id>`
(materialised on consultation open, locked on reject, archived on
merge). Visibility ∈ private/shared/public.

### `ref_log`

Append-only history of every ref movement: `from_commit`, `to_commit`,
`op_kind ∈ init|commit|merge|reset|revert|rebase|delete`,
`actor_subject_id`, `reason`. Equivalent to `git reflog`.

### `proposals`

Pull request between two refs of the same patient. 1:1 with the
consultation that produced it (`consultation_id` FK SET NULL).
Columns: `source_ref_name`, `target_ref_name`, `source_head_commit`,
`target_head_commit`, `base_commit` (LCA at open time),
`proposer_subject_id`, `status ∈ open|approved|rejected|merged|
withdrawn|superseded`, `conflict_count`, `merge_commit`, review fields.

### `merge_conflicts`

Per-proposal cache of conflict detection: `(proposal_id, entity_kind,
entity_id)` → `(base/source/target object_hash, conflict_kind ∈
add_add|edit_edit|edit_delete|delete_edit, resolution ∈
take_source|take_target|manual, resolved_object_hash)`.

### `binary_blobs`

Indirection for S3-backed payloads too big for `entity_objects.payload`
(NIfTI segmentation masks, multi-page PDFs). PK is 32-byte BYTEA
`content_hash = sha256(blob_bytes)`. Columns: `s3_bucket`, `s3_key`,
`size_bytes`, `content_type`, refcount for GC, tombstone fields. The
JSON payload of a `segmentation` entity carries only
`{content_hash, size_bytes, format}` and resolves the actual bytes
via this table.

### Service entry point

`backend/src/bvphoenix/services/versioning.py` exposes
`commit_change`, `read_at_commit`, `diff_commits`, `read_object`,
plus the consultation helpers `open_consultation_branch`,
`resolve_branch_for_write`, `submit_consultation_proposal`,
`fast_forward_merge`. The pilot endpoint refactor lives in
`api/clinical_notes.py` (dual-write atomic on POST/PATCH/DELETE);
`api/consultations.py` wires the lifecycle hooks.

### PLATFORM_OWNER (OpenData)

`subjects.id = '00000000-0000-0000-0000-000000000099'` (default,
configurable via `BVP_PLATFORM_OWNER_SUBJECT_ID`, seeded by migration
`0036_platform_owner_subject`). Owns every OpenData fascicolo;
visibility filters surface its patients and studies to all
authenticated users with read-only enforcement.

---

## 8. Audit

Source: `backend/src/bvphoenix/db/models/audit.py`.

### `audit_log`

Append-only. Columns: `id`, `actor_subject_id NULL`, `action`,
`resource_kind NULL`, `resource_id NULL`, `metadata JSONB`,
`ip_address INET`, `user_agent TEXT`, `created_at`.

Indexes on `(actor_subject_id, created_at)` and
`(resource_kind, resource_id, created_at)` for audit queries.

Today the log is a target for writes but not systematically populated
on every touch; populating it on sharing, viewer access, and LLM
runs is part of the hardening phase (DESIGN.md F11).

---

## 9. Migrations

Source: `backend/alembic/versions/`.

> **Note on numbering.** The OSS release of 2026-05-19 squashed the
> pre-release migration history into a single `0001_initial_schema.py`
> that materialises every table, RLS policy, partial index and
> CHECK constraint accumulated up to that point. Per-feature
> migrations carry the historical narrative name (`0009_rls_policies`,
> `0035_versioning_schema`, `0042_marker_clinical_note_split`,
> `0080_care_phase`, ...) inside CHANGELOG entries, design docs, and
> ADR text, but those filenames **do not exist on disk anymore**;
> their effect is rolled into `0001`. Post-rebase, new migrations
> restart from `0002` with a new numbering line.

Files currently on disk (run `ls backend/alembic/versions/` for the
authoritative list):

| File | Highlights |
|------|-----------|
| `0001_initial_schema.py` | Squashed baseline: every pre-OSS-release schema (subjects / users / orgs / groups / memberships / patients / studies / series / instances / derivatives / tags / embeddings / pgvector indexes / grants / annotations / reports / audit_log / share_links / folders / patient_documents / care phases / clinical events / documents / consents / consultations / proposals / refs / commits / manifest_entries / entity_objects / merge_conflicts / binary_blobs / agent_assistants / jobs / OCR / entities / labs / ...) + RLS policies + CHECK constraints. |
| `0002_*.py` and onwards | Post-rebase deltas: see the file listing under `backend/alembic/versions/`. Recent additions cover calendar subscriptions, imaging studies provenance, pathology slides, agent-assistant revocation (3.7.9 H2), and OpenData / pathology CHECK constraints (3.7.9 H6 / H7). |

Run with `make db.migrate`. Create new revisions with
`make db.revision m="message"`. See
[`development-guide.md`](./development-guide.md) §7.

---

## 10. What's planned but not yet in the schema

For reference; these are called out in
[`./DESIGN.md`](./DESIGN.md) and will be introduced as follow-up
migrations.

- `datasets` — curated collections of studies/series.
- `sale_listings`, `purchases`, `contributor_ledger` —
  P2P marketplace (F9; the credits / payouts / sponsorships tables
  already exist for the LLM-credits and grant-sponsorship paths).
- `user_quotas`, `organization_plans` — storage / plan billing tied
  to F9 / enterprise tier.
- `legal_holds` — GDPR-Art-17 hold table; the predicate stub already
  lives in `services/erasure._user_has_legal_hold` and returns
  `False` until the table lands.

Already landed (was on this list pre-OSS-release):

- `training_consents` — implemented; surfaces as
  `PATCH /api/studies/{id}/tier` (auto-materialise on T3/T4) and
  `DELETE /api/studies/{id}/training-consent` (granular opt-out).
- Per-resource RLS policies mirroring
  `services/permissions.py::visible_studies_filter` /
  `visible_patients_filter` are in place on `studies`, `patients`,
  `reports`, `annotations`, `patient_documents`, `grants`, and the
  eight F12 versioning tables. The owner-bypass gap is tracked as
  open work in [`security-rls.md`](./security-rls.md) §"Production
  hardening required" and pinned by
  `tests/test_versioning_security.py::TestRlsStructuralEnforcement`.
