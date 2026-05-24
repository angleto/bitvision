# Fascicolo as Google Drive: UX paradigm

This document describes the rethinking of the Health Record (Fascicolo) user
experience as a hierarchical container in the Google Drive style. For the
underlying data model and API specification, see
[fascicolo.md](./fascicolo.md).

## 1. Motivation

**Where this fits in the data model.** The `Folder` lives on the *organisational axis* of the three-axis model (see [data-model.md §0](./data-model.md#0-conceptual-overview-the-three-axis-model)). Folders are user-driven, cosmetic, and have no place on the patient timeline; events live in `ClinicalEvent`, semantic groupings live in `CarePhase` (see [care-timeline-phases.md](./care-timeline-phases.md)), search labels live in `Tag` (see [tag-taxonomy.md](./tag-taxonomy.md)). This document covers only the organisational axis.

The historical bitvision phoenix Fascicolo is organized into flat sections
(studies, reports, documents, annotations, notebook): the user navigates with
a vertical sidebar and each section has its own UI. This model has two
limitations:

- Fragmentation: radiology studies and clinical documents live in separate
  views with different interactions (cards vs lists, different filters,
  different uploads).
- Too many clicks to group materials: it is not possible to create a
  "Pre-op 2024" containing CT study + reports + clinical photos.

The Drive paradigm solves both:

- **A single navigable container** with arbitrary folders and an implicit
  root per patient.
- **Uniform interaction** (drag-drop, right-click, multi-select) for any
  item type.
- **Inline preview** (PDF, images, text) without changing page.
- **Fewer clicks** for bulk operations: select, move, share, download ZIP.

The result: the user sees a single "drawer" per patient, managed the way
they would manage a personal Drive.

## 2. UX architecture

The patient page is structured as three vertical regions: **header**, **sticky
clinical notes preamble**, and **Health Record** (the Drive container itself).
The Health Record is always rendered first-class (not a collapsible panel) so
the user lands directly in the work surface after a deep-link or a refresh.

```
+--------------------------------------------------------------------------+
| Patient header (name, demographics, actions, allergies, contacts)        |
+--------------------------------------------------------------------------+
| Clinical notes (sticky, ~5-line preview, fade-out, expand/edit inline)   |
+--------------------------------------------------------------------------+
| Health Record                                                            |
| [Drive] [Eventi] [Documenti] [Sintesi & Evidenze] [Provenance]           |
| +--------------------+--------------------------------------------------+|
| | Folder tree        | Breadcrumb: Patient / Pre-op 2024 / Imaging      ||
| | (left sidebar)     +--------------------------------------------------+|
| |                    | Content pane                                     ||
| |  - root/           |                                                  ||
| |    - Pre-op 2024/  |  [file1.dcm]  [study_ct.card]  [report.pdf]      ||
| |    - Post-op/      |  [consent.pdf]  [photo.jpg]                      ||
| |    - Lab/          |                                                  ||
| +--------------------+--------------------------------------------------+|
+--------------------------------------------------------------------------+
```

**Sticky clinical notes preamble**, between header and Health Record. Renders
`patient.notes` (free-form markdown) clamped to ~5 lines with a soft fade-out
mask. `Espandi / Comprimi` toggles the full text in place; `Modifica` opens an
inline `EvidenceEditor` that PATCHes `/api/patients/{id}.notes` under
`If-Match`. The card uses CSS `position: sticky` so it stays under the eye
while the user scrolls the Health Record below; print mode and short
viewports reset it to static.

**Health Record tabs** (in `FascicoloViewToggle`):

- **Drive** (default), hierarchical view with folder tree + content pane.
- **Eventi**, vertical care timeline (see
  [care-timeline-phases.md](./care-timeline-phases.md)).
- **Documenti**, flat list with the v3 taxonomy.
- **Sintesi & Evidenze**, clinical synthesis draft on top + aggregated
  `ClinicalNote` evidence below, with AI/human filters.
- **Provenance**, append-only audit lineage.

**Drive default sort**: items are ordered by **creation date** (newest first).
For a clinical record the natural axis is "when did this enter the fascicolo",
not the alphabetical filename. Folders carry `clinical_date` so their position
reflects the underlying episode date, not just the metadata-edit timestamp.
The user can switch to `name` or `updated` ordering via the toolbar.

**Current folder header** (`CurrentFolderHeader`): when the user navigates
*into* a folder that has its own clinical context, a header strip appears
between the breadcrumb and the content pane showing `description` (short
markdown), `clinical_date`, and `narrative_md` (extended commentary,
collapsed behind an "Espandi" toggle). The same fields are surfaced inside
the hover tooltip on the folder card; this header keeps the context visible
once the card is gone. Backed by `TreeOut.current_folder` on the tree
endpoint, populated whenever the listing target is a real folder (not the
patient root).

The legacy "monthly timeline" view sketched in earlier revisions of this
document never shipped as a tab; chronological browsing is covered by
**Eventi** (semantic care-phase grouping) for clinical use and by the
content pane's "timeline" view mode within Drive for cosmetic chronology.

**Preview pane** on demand: right-click, "Preview", opens a right-side panel
(or modal on mobile) without closing the current view.

### Deep-link contract

Two query parameters survive refresh and browser-back:

- `?path=<folder-path>` tracks the active folder inside the Drive tab.
- `?view=<drive|events|documents|evidence|provenance>` tracks the active
  Health Record tab.

The `?from=notes&note=<id>` flow used by `EvidenceWorkspace.NoteRow` to mark a
side-trip into a study or document forwards the user back via
`/patients/{id}?view=evidence#note-<id>` so the `#note-<id>` anchor lands on
the right row inside the Sintesi & Evidenze tab.

When the page mounts with any of these query parameters present, the viewport
auto-scrolls the Health Record into view so the user lands directly in the
work surface, not above the sticky clinical notes.

## 3. Folders

The structure is **polymorphic**: a folder can contain any item type, and
items themselves can be folders (subfolders) or leaves (study, series,
report, annotation, document, consultation).

Supported types (`kind`):

- `subfolder`, user folder
- `study`, DICOM study
- `series`, DICOM series (optional, typically nested under a study)
- `report`, report
- `annotation`, human or LLM annotation
- `document`, PatientDocument (consents, letters, prescriptions, photos)
- `consultation`, saved LLM consultation

**Materialised but invisible root per patient**: every patient owns
exactly one folder marked `is_root = TRUE`, anchored at patient
creation in the same transaction. The schema lives in
`0001_initial_schema.py` after the OSS-release rebase (the historical
`0088_patient_root_folder` migration is rolled into the baseline;
see [`data-model.md §9`](./data-model.md#9-migrations)). The row is FK-able from
`folder_items`, so the no-orphan invariant (every live document has at
least one folder containment) has a real target — but the row is never
rendered as a folder card or as a breadcrumb segment. The user-facing
path `/` opens the root's contents directly; folders the user creates
"at the top level" actually land under the root via
`parent_folder_id = root.id`. See ADR
[0020](./agents-api/decisions/0020-documents-hardlinks-and-no-orphan-invariant.md)
for the rationale.

The root cannot be renamed, reparented, or deleted: `PATCH` /
`DELETE /api/folders/{root_id}` returns 409 `root_folder_protected`.

**Hardlinks (a document in N folders simultaneously)**: `folder_items`
already had a polymorphic primary key
`(folder_id, resource_kind, resource_id)`, so the same document can sit
in multiple folder rows. The UI exposes this in two ways:

- **`📎 Add to another folder`** button on the document card toolbar:
  opens a folder picker (modal `HardlinkPickerModal`) listing every
  user-visible folder of the patient, then issues
  `POST /api/folders/{id}/items` to add a hardlink. The original
  containment is preserved.
- **Cmd / Alt + drag** (Finder-style copy): drop the card onto a
  folder card, the `..` parent shortcut, or a breadcrumb segment
  while holding Cmd (mac) or Alt (linux/windows) to add a hardlink
  instead of moving.

A document with `folder_count >= 2` shows a chain-link badge in the
card foot tooltipped *"Linked from N folders (same document)"*. The
badge is the explicit signal that two cards in different folders are
the **same** file — not a duplicate.

When the last hardlink is removed (card delete from the only
containing folder), the backend transparently turns the operation
into a soft-delete of the document (the document never goes
`folder_count = 0` while live: trigger
`trg_folder_items_no_orphan_doc` enforces this). If the document has
active clinical references (study link with `link_kind` other than
`mentions`, or a `content_document_links` row, or a
`report_content_citations` row pointing at the document) the unlink
is rejected 409 with a structured `blocking_references` payload so
the FE can surface a "remove these first" guided flow.

**Permission cascade**: grants on a folder cascade to all its items
(including subfolders). Example: share "Pre-op 2024" with the surgeon, the
surgeon sees study, reports and photos inside the folder without explicit
grants on each.

## 4. Drag-drop

Four modes:

- **Item -> folder** (move): drag a card over a folder in the tree or
  in the content pane, the item changes `parent_folder_id`. No clone:
  the item is the same, only the position changes. For documents this
  is implemented as remove from the source folder + add to the target,
  preserving any other hardlinks the document may already have.
- **Item -> folder + Cmd / Alt** (hardlink): same gesture with the
  modifier held, fires `POST /api/folders/{target_id}/items` instead
  of `POST /api/patients/{id}/tree/move`. The document stays in the
  source folder; the target folder gets an additional containment
  row. Visible afterwards as a chain-link badge on the card.
- **Item -> breadcrumb crumb** (move up): drag a card onto any clickable
  crumb (the home icon or any parent segment) and drop. Resolves to
  `tree/move` with `target_folder_id = crumb.id` (or the patient root
  on the home crumb). Cmd / Alt held = hardlink to the segment instead
  of move.
- **OS file -> folder** (upload): drag one or more files from the
  desktop over the content pane, upload to the current folder starts.
  Type inferred from content-type / extension (DICOM -> study, report
  PDF -> report, other -> document).
- **Multi-select**: Shift+click for range, Cmd/Ctrl+click for toggle.
  The selection is visible and dragging one of the selected elements
  moves them all.

Visual feedback: drop target highlight, dashed outline, "N items"
badge during multi-selection drag, chain-link badge on the card foot
when `folder_count >= 2`.

## 5. Context menu (right-click)

The menu is **polymorphic** based on the target item's `kind`:

| Kind | Available actions |
|------|-------------------|
| subfolder | Open, Rename, Share, Move, Download ZIP, Delete |
| study | Open in viewer, Preview, Add report, Share, Move, Download, Delete |
| series | Open in viewer, Preview, Move |
| report | Preview, Download, Edit, Share, Move, Delete |
| annotation | View, Edit, Share, Move, Delete |
| document | Preview, Download, Reclassify, Share, Move, Delete |
| consultation | Open, Export, Share, Move, Delete |

On multi-select the menu shows only actions **common** to the subset.

## 6. Batch actions

When the selection is >= 2 items, a floating **action bar** appears at the
bottom:

- **Download ZIP**, bundle of all selected files (recursive on folders).
- **Move**, opens a "Choose destination folder" dialog with folder picker.
- **Share**, creates a single public link exposing the entire selection
  (internally: temporary folder or multiple grants).
- **Delete**, soft delete with confirmation, cascades on folders.

## 7. Inline preview

The preview pane opens content without navigating away from the page:

- **PDF**, `<iframe>` with the file served by the backend (presigned URL).
- **Image** (JPG, PNG, WebP), `<img>` with fit contain.
- **Text / Markdown**, server-side markdown rendering, plain text in
  `<pre>`.
- **DICOM study**, key image thumbnail + "Open in viewer" button (the full
  viewer remains a dedicated page for performance).
- **Other types** (DOCX, XLSX, DICOMDIR, unknown), fallback to a
  "Download" button.

The preview is lazy: it loads only on opening, not eagerly in the listing.

## 8. Search + Timeline

**Search bar** in the Fascicolo page top header, integrated with
`GET /api/patients/{id}/search` (see
[search-and-embeddings.md](./search-and-embeddings.md)):

- Primary ranking: `ts_rank` on full-text.
- Fallback: semantic search over embeddings (BiomedCLIP for images, text
  embeddings for documents) when FTS returns 0 results or low score.
- 250ms debounce, results grouped by kind and by folder.

**Timeline view** is an alternative content pane view: instead of the
folder tree, items are grouped by month (header "April 2026", "March
2026", ...) in reverse chronological order. Useful for questions like
"what happened in 2024?".

## 9. Mobile

Below 768px width:

- **Collapsible** folder tree: drawer hidden on the left, opened via
  hamburger button.
- **Full-width** content pane, larger cards optimized for touch.
- Drag-drop disabled (or with long-press to activate selection), replaced
  by context menu with "Move to..." action.
- Preview pane opens as full-screen modal instead of side panel.

## 10. Comparison with Google Drive

**What is there**:

- Hierarchical folder tree
- Drag-drop + multi-select
- Inline preview
- Full-text + semantic search
- Granular sharing (public links, user grants)
- Polymorphic context menu

**What is intentionally missing** (roadmap, not MVP):

- **Star / Favorites**, mark items as favorites for quick access.
- **Trash**, delete is currently soft but there is no UI for restore;
  roadmap: trash view with 30-day restore.
- **Usage quota**, no visible space limit per patient; current limits are
  at user/tenant level.
- **Revision history**, reports are versioned in DB but there is no
  Drive-style UI for diffs between versions.
- **Inline comments**, no comment threading on an item (use of
  consultations/annotations as a surrogate).
- **Offline sync**, no desktop client, no filesystem synchronization.

## See also

- [fascicolo.md](./fascicolo.md), Fascicolo data model and API
- [onboarding-tutorial.md](./onboarding-tutorial.md), end-to-end tutorial
- [search-and-embeddings.md](./search-and-embeddings.md), hybrid search
- [sharing.md](./sharing.md), sharing model
- [patient-fascicolo-export.md](./patient-fascicolo-export.md), ZIP export
