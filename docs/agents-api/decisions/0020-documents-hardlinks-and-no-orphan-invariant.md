# ADR 0020: Document hardlinks, materialised patient root, no-orphan invariant, multi-referto

**Status**: Accepted
**Date**: 2026-05-06
**Migrations**: `0088_patient_root_folder`, `0089_document_study_links_multi_referto`
**Deciders**: Angelo Leto

## Context

Three orthogonal weaknesses surfaced in the v3 documents model after a
few weeks of real-patient usage:

1. **No way to file the same document under multiple folders.** A
   discharge letter is clinically relevant for the surgical phase
   *and* for the follow-up phase; users want it visible in both
   without uploading two copies. The `folder_items` table already
   had a polymorphic primary key
   `(folder_id, resource_kind, resource_id)` that supports this
   (the same document id can appear in N rows), but neither the
   API nor the UI exposed the gesture. Users were uploading
   duplicates, which polluted dedupe + storage quota and made the
   "Riferito da" view unreliable.
2. **The "patient root" was a documented fiction.** The
   `folders.py` module said *"every patient has an implicit root.
   Any item attached to a patient but not explicitly listed in a
   folder is considered to live in that patient's root folder"*,
   but no row backed that claim. `ingest_document` could create a
   `documents` row without any `folder_items` companion; the FE
   tree assumed those rows live "at /". As soon as we wanted to
   enforce "every live document is reachable through at least one
   folder" — the natural invariant for any Drive-style UX — there
   was nothing to FK against.
3. **`document_study_links.link_kind` allowed multiple
   `report_of` rows per study.** The unique constraint was on the
   triple `(document_id, study_id, link_kind)`; nothing prevented
   two distinct documents from claiming `report_of` on the same
   study. In production this happened naturally (multi-referto:
   a primary report + an addendum + occasionally a second
   opinion), but the model called every one of them `report_of`,
   which lost semantic information and prevented the FE from
   surfacing the *primary* report distinctly.

## Decision

### A. Hardlinks: one document, N folder containments

We use the existing `folder_items` polymorphism as the hardlink
primitive. No schema change to support it; what's added is the
*UX gesture* and the *count surfaced on the document DTO*.

- `POST /api/folders/{id}/items` (existing) creates a hardlink
  when the resource already lives in another folder. The unique
  PK on the triple naturally prevents duplicate hardlinks to the
  same folder (returns 409, idempotent semantics from the
  client's POV).
- `PatientDocumentOut`, `TreeNode` (drive listing) and the new
  reverse-direction endpoint `GET /documents/{id}/references`
  carry `folder_count` and `is_in_root_only` so the FE can render
  a chain-link badge on cards with `folder_count >= 2`.
- The frontend exposes two ways to create a hardlink: a `📎` button
  on the card toolbar (opens `HardlinkPickerModal`, a folder picker
  that lists every patient-visible folder excluding the root + the
  current containment) and `Cmd / Alt + drag` (Finder-style copy
  modifier, reuses the existing drop targets: folder cards, `..`
  parent shortcut, breadcrumb crumbs).

### B. Materialised patient root, invisible to the user

`folders.is_root` (boolean, default false) plus a partial unique
index `uq_folders_root_per_patient ON folders(patient_id)
WHERE is_root` give every patient exactly one row that
*represents* the root. The row is created in the same transaction
as the patient (`get_or_create_root_folder` service, idempotent
under the partial unique). It is **never** rendered as a folder
card or as a breadcrumb segment:

- `_resolve_path("/")` translates to "inside the root".
- `_breadcrumb_for_folder` skips the row when walking up.
- `_child_folders_with_counts` filters `is_root = false`.

User folders previously created at "the top level"
(`parent_folder_id IS NULL` + `patient_id IS NOT NULL`) are
re-parented under the root by the migration backfill. From the
user's POV nothing changes: the same folders appear at `/`,
they just have a real parent in the schema.

The root cannot be renamed, reparented or deleted: PATCH
`/api/folders/{root_id}` and DELETE `/api/folders/{root_id}`
return 409 `root_folder_protected` if `is_root` is true.

### C. No-orphan invariant enforced by the database

Three `CREATE CONSTRAINT TRIGGER` rows, all
`DEFERRABLE INITIALLY DEFERRED`, share a single function
`enforce_document_in_folder()`:

- `trg_folder_items_no_orphan_doc` (AFTER DELETE on `folder_items`
  WHERE `resource_kind = 'document'`).
- `trg_documents_restore_no_orphan` (AFTER UPDATE OF `deleted_at`
  on `documents` WHEN `NEW.deleted_at IS NULL AND OLD.deleted_at IS NOT NULL`).
- `trg_documents_insert_no_orphan` (AFTER INSERT on `documents`
  WHEN `NEW.deleted_at IS NULL`).

At COMMIT the function raises `check_violation` with code
`document_orphan_forbidden` if any live document has zero
`folder_items` rows. `DEFERRABLE INITIALLY DEFERRED` is the key:
service flows that stage *delete the last folder_items + set
deleted_at* in a single transaction commit cleanly because both
mutations are visible at COMMIT time, where the trigger reads
`deleted_at IS NOT NULL` and exits early.

The invariant is *belt and suspenders* — the service layer
already takes the same care, but the trigger catches any future
code path that forgets, and any direct DBA query.

### D. Git-like deletion

`delete_document`:

- Default flow: soft-delete (`deleted_at = now()`,
  `purge_after = NULL` — no automatic purge window).
- Rejects 409 with structured `blocking_references[]` if the
  document still has clinical references active
  (`document_study_links` non-`mentions`, `content_document_links`
  any role, `report_content_citations` targeting the document).
  The payload carries `kind`, `id`, `label`, `detail_url` and
  `extra` so the FE can route the user to the source and ask them
  to cleanup, then retry.
- `?force=true` is admin-only and gated on a new scope
  `documents:purge` (intended for GDPR right-to-erasure, never
  for routine work).

`restore_document` clears `deleted_at` and reattaches the document
to the patient root if the unlink path lost its `folder_items`
rows along the way. The trigger
`trg_documents_restore_no_orphan` makes the reattach mandatory:
restoring a document without a folder fails at COMMIT.

The card-level delete (`DELETE /folders/{f}/items/document/{d}`)
implements the smart-delete:

- If `folder_count >= 2` after the unlink, the document
  survives — only the `folder_items` row is removed.
- If `folder_count = 0` after the unlink, the route runs
  `collect_blocking_references` and either soft-deletes the
  document in the same transaction or rolls back with 409 and
  the structured payload.

### E. Multi-referto: primary + addendum + second_opinion

`document_study_links.link_kind` enum is expanded:

| Old | New | Semantics |
|---|---|---|
| `report_of` | `primary_report` | The canonical report for the study. ≤ 1 per study (partial unique index). |
| (n/a) | `addendum` | A correction or extension added after the primary. Many per study. |
| (n/a) | `second_opinion` | A separate read by another radiologist. Many per study. |
| `extracted_from` | `extracted_from` | Provenance — content was OCR'd / parsed out of the document. |
| `cites` | `cites` | Soft citation. |
| `mentions` | `mentions` | Transient mention; non-blocking on document delete. |

A partial unique index
`uq_document_study_links_primary_per_study ON document_study_links(study_id) WHERE link_kind = 'primary_report'`
enforces the cardinality. Production data was reconciled by the
migration: studies that historically had multiple `report_of`
rows kept the earliest (smallest `created_at`) as
`primary_report` and demoted the rest to `addendum`. This
preserves every existing document-study association and assigns
each a clinically reasonable role (the original report stays
primary; the later additions become addenda).

The MCP `link_document_to_study` tool exposes the new enum and
auto-translates legacy `report_of` input to `primary_report`
through `services.documents.link_kind.coerce_link_kind` for one
release, with a deprecation warning.

## Consequences

### Positive

- **Same document, multiple folders.** Real workflow case
  (discharge letter visible in surgery + follow-up) is
  expressible without duplicates.
- **No-orphan is a database fact.** Past code paths that left
  documents without folder containment are caught by the
  trigger; future code paths cannot drift either. The invariant
  is impossible to violate by accident at the SQL layer.
- **Restore is lossless.** Because folder containment is the
  *anchor* of the document's lifecycle, undoing a delete is
  always meaningful: the document goes back to where it was, or
  to the root if it lost containment.
- **Multi-referto matches reality.** Radiology workflow has
  always supported addenda; the schema now records that without
  losing the *primary* role distinction.
- **MCP / FE / API parity.** The same vocabulary
  (`primary_report`, `addendum`, `second_opinion`) flows through
  every layer. The shim guarantees a smooth deprecation for
  external clients still on `report_of`.

### Negative / costs

- **Migration data reconciliation.** The 0089 migration mutates
  data: it demotes duplicate `primary_report` rows. We document
  this clearly and the reconciliation rule is deterministic
  (oldest wins as primary), but it is irreversible without a
  pre-migration snapshot. PostgreSQL DDL transactionality keeps
  the migration atomic — partial state is impossible — but a
  rollback after a successful upgrade has to handle the demoted
  rows out-of-band.
- **Trigger-only enforcement on document INSERT.** The
  `trg_documents_insert_no_orphan` trigger fails any code path
  that creates a document without staging the matching
  `folder_items` row in the same transaction. This is the
  *intended* outcome — the canonical
  `ingest_document` route attaches to the root by default — but
  any new bulk-import path has to remember the same pattern or
  the trigger will block it.
- **Materialised root is implementation detail leaking into
  ops.** SQL queries that filter `parent_folder_id IS NULL` to
  find "patient-level folders" stop working post-migration:
  user folders now have `parent_folder_id = root.id`. The
  application code is updated; ad-hoc DBA scripts have to be
  reviewed.

## Alternatives considered

- **Synthetic root path with no DB row.** Keep `/` as a virtual
  concept, allow `folder_items.folder_id` to be NULL for "in the
  root". Rejected: NULL on a primary-key column requires a
  schema redesign of `folder_items`, and the FK-able real row
  is what makes the no-orphan trigger trivial.
- **Soft-link table separate from `folder_items`.** Two distinct
  tables for "document is in this folder" vs "document is also
  hardlinked here". Rejected: same data, two write paths, two
  query patterns. The polymorphic `folder_items` already
  supports both naturally (a hardlink is just a second row).
- **Refcount column on `documents`.** Materialise `folder_count`
  on the documents row, maintained by triggers. Rejected for
  now: write amplification on every folder mutation, and the
  `GROUP BY` subquery on `folder_items` is cheap enough at our
  scale (the index `ix_folder_items_resource` covers it).

## Backout

Both migrations are atomic via PostgreSQL DDL transactionality;
a failure during upgrade leaves the database at the previous
revision. The 0089 downgrade reverses the rename and drops the
new enum members; rows that were demoted to `addendum` from
`primary_report` are renamed back to `report_of` (the legacy
vocabulary), so no data is lost.

The 0088 downgrade drops the trigger family + the column +
indices but does not delete the materialised root rows or revert
the re-parent of user folders. Production downgrade is therefore
a one-way door for the *visual layout* (folders stay nested
under the now-vestigial root row), even if the schema is
restored. This is acceptable because production downgrade is an
emergency operation; for normal recovery from a buggy migration
the path is *forward-fix in 0090+*, not downgrade.
