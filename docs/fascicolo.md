# Health Record (Fascicolo)

Bitvision phoenix includes a per-patient radiology record (Fascicolo),
inspired by the Italian Fascicolo Sanitario Elettronico 2.0 (FSE 2.0),
adapted for a radiology imaging platform.

> For the user experience (Drive UX) see
> [fascicolo-drive-ux.md](./fascicolo-drive-ux.md). For an end-to-end
> tutorial, see [onboarding-tutorial.md](./onboarding-tutorial.md).

## Fascicolo index

The Fascicolo is organized into **6 sections**, with a transversal timeline:

| # | Section | Description | FSE 2.0 ref. |
|---|---------|-------------|--------------|
| 1 | **Patient Profile** | Extended demographics (name, date of birth, sex, tax ID, phone, email, address, blood type, allergies, clinical notes) | Profilo Sanitario Sintetico |
| 2 | **Diagnostic Studies** | DICOM studies grouped by date and modality (CT, MR, CR, US...) | Referti di Radiologia |
| 3 | **Reports** | Radiology reports linked to studies, text + attached PDF, versioned | Referti di Specialistica |
| 4 | **Clinical Documents** | Consents, discharge letters, prescriptions, referrals, lab results, ER reports, clinical notes, other | Documento dei Consensi, Lettera Dimissione, etc. |
| 5 | **Annotations** | Human and LLM annotations on studies, with source badge | Osservazioni Cliniche |
| 6 | **Personal Notebook** | Documents uploaded by the patient (visible only if `self_user_subject_id` is set) | Taccuino Personale |

The **Timeline** is a chronological view across all sections.

## Data model

### Patient (extended)

`patients` table with additional columns:

- `birth_date` (date), date of birth
- `sex` (char 1), M / F / O
- `tax_id` (varchar 32), tax ID, unique partial index (WHERE NOT NULL)
- `phone`, `email`, `address`, contacts
- `blood_type` (varchar 8), blood type (e.g. A+, O-)
- `allergies`, `notes` (text), free text

### PatientDocument

`patient_documents` table, standalone documents attached to the patient:

- `patient_id` (FK patients, CASCADE)
- `uploaded_by_subject_id` (FK subjects)
- `document_type`, CHECK constraint: `consent`, `discharge_letter`, `prescription`, `referral`, `lab_result`, `er_report`, `clinical_note`, `personal_notebook`, `other`
- `title` (varchar 255)
- `text` (text, optional)
- `file_s3_key`, `file_content_type`, attached file in S3 (derivatives bucket, path `patient-docs/{patient_id}/{doc_id}.{ext}`)
- `document_date` (date, optional)

## API

### Patients CRUD

```
GET    /api/patients                    # paginated list, search by name/tax_id/external_id
POST   /api/patients                    # create with full demographics
GET    /api/patients/{id}               # detail with demographics
PATCH  /api/patients/{id}               # update demographics
DELETE /api/patients/{id}               # owner/admin only
```

### Fascicolo index

```
GET    /api/patients/{id}/index         # FascicoloIndex: patient + sections[] with count, last_date, breakdown
```

Returns per-section counts so the UI can display numeric badges in the sidebar.

### Timeline

```
GET    /api/patients/{id}/timeline      # chronological merge of all items
                                        # ?section= filter (studies|reports|documents|annotations)
                                        # paginated with limit/offset
```

### Documents

```
GET    /api/patients/{id}/documents                 # list, ?type= filter; each row carries
                                                    # folder_count + is_in_root_only
POST   /api/patients/{id}/documents                 # multipart upload (title, type, text, file)
GET    /api/patients/{id}/documents/{doc_id}        # single document, folder_count populated
DELETE /api/patients/{id}/documents/{doc_id}        # soft-delete (git-like, restorable);
                                                    # 409 with blocking_references[] if
                                                    # clinical references are still active
POST   /api/patients/{id}/documents/{doc_id}/restore       # undo soft-delete; auto-reattaches
                                                            # to the patient root if the
                                                            # folder_items rows were lost
GET    /api/patients/{id}/documents/{doc_id}/references    # reverse-direction inventory:
                                                            # studies, report_contents,
                                                            # citations and folders that
                                                            # reference the document
POST   /api/patients/{id}/documents/{doc_id}/links         # link to a study, link_kind ∈
                                                            # {primary_report, addendum,
                                                            #  second_opinion, extracted_from,
                                                            #  cites, mentions}; partial unique
                                                            # index enforces ≤ 1 primary per
                                                            # study (409 primary_report_already_set)
DELETE /api/patients/{id}/documents/{doc_id}/links/{study_id}/{link_kind}
```

#### Hardlinks (a document in N folders)

`folder_items` is a polymorphic N:M bridge with primary key
`(folder_id, resource_kind, resource_id)`, so the same document UUID
can appear in multiple `folder_items` rows. Each row is one hardlink.
Hardlinks are created via `POST /api/folders/{id}/items` and dropped
via `DELETE /api/folders/{id}/items/document/{doc_id}`. The card
soft-delete path is *smart*: dropping the last hardlink only triggers
a document soft-delete; intermediate unlinks just remove the row.

`PatientDocumentOut` and `TreeNode` (drive listing) carry
`folder_count` and `is_in_root_only` so the FE can render the
chain-link badge when the count reaches 2.

#### No-orphan invariant

Every patient owns one *materialised* root folder (`folders.is_root = TRUE`,
unique partial index per `patient_id`). The schema lives in
`0001_initial_schema.py` after the OSS-release rebase (the migration
historically called `0088_patient_root_folder` is rolled into the
baseline; see `data-model.md §9`). The root row exists only to
anchor the trigger family `enforce_document_in_folder()`:

| Trigger | Fires when | Effect |
|---|---|---|
| `trg_folder_items_no_orphan_doc` | `DELETE` on `folder_items` (`resource_kind = 'document'`) | At COMMIT raises `check_violation` if the document is still live (`deleted_at IS NULL`) and has zero folder containment. |
| `trg_documents_restore_no_orphan` | `UPDATE OF deleted_at` on `documents` (NOT NULL → NULL) | At COMMIT raises if the restored document has no folder containment. |
| `trg_documents_insert_no_orphan` | `INSERT` on `documents` | At COMMIT raises if the freshly-inserted live document has no folder containment. |

All three are `DEFERRABLE INITIALLY DEFERRED` so a service-layer
transaction can stage *delete folder_items + set deleted_at* in a
single unit of work without the trigger firing in the middle. The
canonical patterns (ingest a document + add to root, restore + reattach
to root if needed, smart-delete unlink + soft-delete) all commit cleanly.

The materialised root is invisible in the UI: `_resolve_path("/")` opens
the root's contents directly, `_breadcrumb_for_folder` skips the root
row, and `_child_folders_with_counts` filters `is_root = false`. From
the user's POV the root is just "the patient", with their own folders
visible at the top level.

#### Git-like deletion

`delete_document` is always a soft-delete (`deleted_at = now()`,
`purge_after = NULL` by default — no automatic hard-delete window).
`restore_document` rehydrates the document and reattaches it to the
root folder if the folder containment was lost during the unlink path.
The `?force=true` admin escape-hatch (GDPR right-to-erasure) is gated
on the new scope `documents:purge`; routine work never hits it.

### Patient-level sharing

```
POST   /api/patients/{id}/share         # same ShareCreateIn structure as sharing.py
GET    /api/patients/{id}/shares        # list of active links
```

Grants with `resource_kind='patient'` automatically cascade to the patient's
studies: `effective_permissions_on_study()` also checks patient-level grants
via `study.patient_id`.

## Permissions

- `effective_permissions_on_patient()`, admin -> ALL, managed_by -> ALL, self_user -> ALL, otherwise grants
- `can_patient()`, boolean wrapper
- `visible_patients_filter()`, patients are NEVER visible to anonymous users; authenticated users see: managed_by, self_user, or with a grant

## Care Timeline & Clinical Phases

The flat per-patient timeline (§ Timeline) is complemented by a
**semantic grouping** of the same clinical events into **care phases**.
A care phase is a clinically meaningful chapter of a patient journey
(staging imaging, primary surgery, adjuvant follow-up, ...) onto which
individual `clinical_event` rows are assigned via a composite foreign
key that prevents cross-patient assignment at the database level.

Each phase has:

- a slug unique per patient, a localized name (`name_i18n`), an `ordinal`,
  optional `start_date` / `end_date` and a free-form `narrative_md`
- a `kind` from a fixed vocabulary of 7 values:
  `imaging`, `surgery`, `followup`, `surveillance`, `visit`,
  `reassessment`, `other`
- a `color_hex` rendered in the chip and in the SVG
- provenance fields `author_kind` (`human` / `agent`),
  `proposed_by_agent_id`, `confirmed_by_user_id` so AI authorship
  remains visible after human confirmation
- an `etag` for optimistic concurrency on PATCH

### Classifier and human review

A bounded LLM classifier processes one patient at a time, fetches the
clinical events plus associated study / report / consultation
abstracts, and proposes a `care_phase_proposal` (set of phases plus
event-to-phase assignments). The proposal is cached by `input_hash`
(7 days) and applied atomically through `care-phases:apply-proposal`
guarded by `Idempotency-Key`. The same scope catalog used elsewhere
(MCP, REST) splits read, propose and write so a clinician can let an
agent draft phases without granting it write access. Humans then
edit, reorder, restore previous revisions, or delete phases via the
phase editor; revision history is persisted in `care_phase_revision`.

### Frontend tab Events

The patient Fascicolo tab Events now mounts the vertical care
timeline `<CareTimeline />`: phases are rendered as colored chips
on the left of a dashed vertical axis, events as dots on the right,
with the same layout as the reference visual
the local reference SVG layout (not committed in the repo).
Click on a chip opens the per-phase page
`/patients/{id}/care-phases/{slug}` (header, narrative, sub-tabs for
studies, documents, reports / consultations, annotations); click on
a dot navigates to the natural event target (study, report, document
or consultation).

The full data model, REST contract, classifier algorithm, SVG
renderer and frontend wiring are documented in
[care-timeline-phases.md](./care-timeline-phases.md). REST endpoints
are listed in [api-reference.md §10b](./api-reference.md#10b-care-phases-semantic-timeline)
(17 endpoints including SVG / iCal / PDF exports and the `?async=true`
arq enqueue path), MCP tools and scopes in
[agent-protocols.md](./agent-protocols.md) (care-phases tool family
under `mcp/src/bvmcp/tools/care_phases.py`, a strict superset of the
GUI: every button has an MCP equivalent plus introspection helpers). Caller introspection is at
`GET /api/me/scopes` so both the GUI and agents can gate writes by
the scopes the current token actually holds.

## Frontend

- `/patients`, patient list with search by name or tax ID
- `/patients/{id}`, Fascicolo page (see [§ Patient page layout](#patient-page-layout) below for the current vertical structure and deep-link contract)
- `/patients/{id}/care-phases/{slug}`, per-phase detail (header, narrative, sub-tabs for studies, documents, reports / consultations, annotations)
- `PatientShareDialog`, component for patient-level sharing

### Patient page layout

`/patients/{id}` is composed top-to-bottom of three regions, each with a
distinct semantic role:

1. **Patient header** (`PatientHeader` in
   `frontend/src/app/patients/[id]/page.tsx`): name, demographics line, action
   buttons (history, share, share-with-AI, edit). The full demographics block
   (`Anagrafica completa`) and the contacts list are collapsible. An `Allergies`
   warning chip surfaces here when present.

2. **Clinical notes (sticky)** (`ClinicalNotesSticky`): a sticky compact card
   that previews `patient.notes` (free-form markdown). Roughly five lines tall
   in collapsed state, with a soft `mask-image` fade-out and an `Espandi /
   Comprimi` toggle for the full text. When `isOwner`, an inline `Modifica`
   button opens an `EvidenceEditor` that saves via `PATCH /api/patients/{id}`
   with the standard `If-Match` ETag flow. The card stays visible (CSS `position:
   sticky`) while the user navigates the Health Record below; it is reset to
   static positioning under `@media print` and on viewports shorter than 600 px.

3. **Health Record** (`FascicoloViewToggle` + `FascicoloDriveLayout`): the
   primary work surface. Always rendered (no longer a collapsible panel).
   Tabs:
   - `Drive` (default): hierarchical folder tree + content pane with grid /
     list / timeline view modes. **Default sort is creation date** (newest
     first); switchable to `name` or `updated`. When the user descends into
     a folder that carries `description` / `narrative_md` / `clinical_date`,
     a `CurrentFolderHeader` strip surfaces those fields between the
     breadcrumb and the grid (the same context that the hover tooltip shows
     on the parent listing's folder card, kept visible once the user is
     inside).
   - `Eventi`: vertical care timeline with phase chips (see
     [care-timeline-phases.md](./care-timeline-phases.md)).
   - `Documenti`: flat list with the v3 taxonomy.
   - `Sintesi & Evidenze`: clinical synthesis (`SynthesisPanel`) on top, plus
     all `ClinicalNote` rows (`NotesAggregateView`) aggregated chronologically
     with AI/human filters. Mounting this tab is also the way the
     `?from=notes&note=<id>` back-link from any sub-page lands on the right
     `#note-<id>` anchor.
   - `Provenance`: append-only audit lineage of patient changes.

The `GET /api/patients/{id}/tree` response (`TreeOut`) carries a
`current_folder: TreeNode | null` field populated whenever the listing
target is a real folder (not the patient root, not for shared-link or
anonymous callers). It allows the FE to render the folder header strip
without a second round-trip to fetch the folder's own metadata.

### Deep-link contract

The patient page reads two query parameters and writes them back via
`router.replace` (no extra history entry):

- `?path=<folder-path>` is owned by `FascicoloDriveLayout` and tracks the
  active folder inside the Drive tab. `path=/` collapses the parameter away.
- `?view=<tab>` is owned by `FascicoloViewToggle` and tracks the active tab.
  Values: `drive | events | documents | evidence | provenance`. `view=drive`
  collapses the parameter away.

Both parameters survive refresh, browser-back, and external deep-links. On
mount, if either is present, the page scrolls the Health Record into view so
the user lands directly in the work surface instead of above the sticky
clinical notes.

The semantic distinction between the three places that talk about "notes"
deserves emphasis (they are *not* synonyms):

| Surface | Backed by | Cardinality | Role |
|---|---|---|---|
| Clinical notes (sticky) | `patient.notes` column | one per patient | static reference about the patient (allergies-of-the-mind, recurring concerns) |
| Evidence | `clinical_notes` table, target_kind ∈ {study, series, document, consultation, folder, patient} | many | scattered field notes attached to specific artefacts, written from each artefact's own page |
| Synthesis | `consultations` row in `draft` status, well-known title | one active draft per author | the clinician's coherent narrative, signable into a formal consultation |
