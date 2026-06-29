# Changelog

All notable changes to bitvision phoenix are listed here. The
project follows semantic versioning; pre-release suffixes (`alpha`,
`beta`) gate Kubernetes deployments via the GHCR image tag (without
the leading `v`, see deployment guide).

## 4.4.91 (2026-06-29)

### Standards conformance: FHIR CapabilityStatement + DICOMweb conformance gate

* `GET /api/fhir/metadata` now serves a FHIR R4 `CapabilityStatement` (the
  standard capabilities interaction). Public and PHI-free. It is honest by
  construction: it declares the resource types the export Bundle emits
  (`Patient` / `ImagingStudy` / `DiagnosticReport` / `DocumentReference`) and
  advertises **no** FHIR REST interactions bitvision doesn't implement —
  bitvision *produces* FHIR Bundles and serves imaging over DICOMweb; it is
  not a general FHIR REST server.
* `docs/conformance.md` is the DICOMweb (QIDO-RS / WADO-RS) + FHIR conformance
  statement. The claim is made **falsifiable in CI**
  (`tests/test_fhir_capability.py`): the CapabilityStatement round-trips
  through the FHIR R4B model, advertises exactly the exported resource types
  (shared single source, so it can't drift), and every DICOMweb transaction
  the statement claims is asserted to be a registered route — an over-claimed
  conformance fails the gate. The proof that a patient-owned platform can hand
  the data back, which a closed lake structurally cannot.

## 4.4.90 (2026-06-29)

### DICOMweb: WADO-RS frames/bulkdata + QIDO relational roots

* The DICOMweb read surface now answers WADO-RS **frames**
  (`GET …/instances/{sop}/frames/{framelist}`), so OHIF's default
  pixel-streaming path works (not just full-instance retrieve). Each frame
  is the stored codec bitstream — native data is sliced by stride,
  encapsulated data is read back per fragment — with no transcoding; the
  part `Content-Type` carries the codec media type and a `transfer-syntax`
  parameter (`application/octet-stream`, `image/jp2`, `image/jls`,
  `image/jpeg`, `image/dicom-rle`).
* WADO-RS **bulkdata** (`GET …/instances/{sop}/bulkdata/{tag}`) serves a
  top-level binary element, and the metadata endpoint now wires
  `BulkDataURI` (PixelData → the frames resource, other bulk elements →
  their bulkdata resource) instead of emitting none.
* QIDO-RS **relational roots** `GET /series` and `GET /instances` search the
  caller's whole visible set (the hierarchical forms already existed).
* Fix: `RetrieveURL` / `Content-Location` now honour `X-Forwarded-Proto`, so
  the emitted URLs are `https` behind the TLS-terminating Traefik proxy
  (where `request.base_url` is `http`) — no mixed-content for a client
  following a RetrieveURL from an https context.
* Patient-scoping (cross-patient inexpressible), storage isolation, and
  PS3.15 de-identification-on-egress carry over unchanged. Rendered
  (JPEG/PNG) and transfer-syntax transcoding remain tracked follow-ups.
  `docs/dicomweb.md` updated.

## 4.4.89 (2026-06-29)

### FHIR R4 Bundle export (Health Record + GDPR)

* Both the Health Record (Fascicolo) and GDPR/PHR exports now ship a
  `fhir-bundle.json` next to the canonical `manifest.json` — the same
  record as an HL7 FHIR R4 `Bundle` (Patient / ImagingStudy /
  DiagnosticReport / DocumentReference), so any FHIR-aware EHR can ingest
  a bitvision export with no bespoke parser. Additive: the PHR-Bundle
  manifest stays the lossless, round-trippable payload.
* **Patient-safety guard-rail.** `DiagnosticReport.status = final` is
  reserved for *human-attested* reports — a `canonical_synthesis` a human
  **signed** (a hard human-only gate) or an `original`/`derived` report a
  human **authored and endorsed**. AI-authored (`author_kind=agent`) or
  not-yet-attested content is `preliminary` at most and can never present
  as a clinician-final report downstream; an `author-kind` extension (and
  `ai-model`/`ai-provider` when an agent drafted it) preserves provenance.
* Images are referenced via WADO-RS (`ImagingStudy.endpoint` →
  `dicom-wado-rs` Endpoint), never inline pixels or storage URLs.
* The exporter emits plain dicts; conformance is gated in CI by
  `tests/test_fhir_export.py`, which round-trips every emitted resource
  through `fhir.resources` R4B models (a test-only dependency, so the
  production API/worker images stay free of the FHIR ORM).
  `docs/fhir-export.md` documents the format and the guard-rail.

## 4.4.88 (2026-06-29)

### DICOMweb: anonymous browse of the public OpenData library

* The DICOMweb endpoints now use `public_user`, so an anonymous client can
  run QIDO query and WADO metadata against **public (OpenData) studies**
  with no credentials — the open-standard, point-OHIF-at-it browse path the
  4.4.87 surface intended but blocked (the global auth gate 401'd anonymous
  under `optional_user`). WADO **retrieve** stays gated on `download:dicom`
  (public studies grant read/view but not file download), so anonymous
  retrieve of a public study is a `403`, consistent with the rest of the
  platform's `.dcm` export policy. `docs/dicomweb.md` documents the access
  model.

## 4.4.87 (2026-06-29)

### DICOMweb read surface: QIDO-RS + WADO-RS under /api/dicom

* bitvision now answers DICOMweb queries and retrievals, not just STOW-RS
  stores. Point OHIF / 3D Slicer / a PACS at `https://<host>/api/dicom` and
  it queries (QIDO-RS, `application/dicom+json`) and pulls instances (WADO-RS
  `multipart/related`) drop-in. A capability a closed, inbound-only,
  irreversibly-anonymizing biobank structurally cannot offer — and the
  foundation the FHIR bundle, the conformance statement, and the
  bitvision↔bitvision pull connector build on.
* **Patient scoping is the query base.** Every list is built on
  `visible_studies_filter` and every UID resolves inside that filtered set,
  so an out-of-scope study UID is a `404` — cross-patient access is
  inexpressible, not merely refused. Bytes stream through the backend
  (`iter_object`); no bucket/key/presigned URL crosses the boundary.
  De-identification is honoured on egress (PS3.15) for share recipients and
  T3 studies. Anonymous callers see only public OpenData studies, so the
  public library is browsable with no credentials.
* QIDO: `/studies`, `/studies/{s}/series`,
  `/studies/{s}/series/{se}/instances`, `/studies/{s}/instances` with
  matching (StudyInstanceUID / PatientID / ModalitiesInStudy / Modality /
  StudyDate range / StudyDescription wildcard) + `limit`/`offset`. WADO
  retrieve: study / series / instance as `multipart/related`. WADO metadata:
  study / series / instance DICOM-JSON (pixel data excluded, no dangling
  `BulkDataURI`). `docs/dicomweb.md` documents the surface; WADO
  frames/bulkdata/rendered + transcoding are tracked follow-ups.

## 4.4.86 (2026-06-29)

### Consent: patient-visible append-only consent ledger + MCP tool + FE

* New `GET /api/gdpr/consent-ledger`: the caller's append-only grant/revoke
  history for account-level GDPR consents and per-study training opt-ins
  (tiers T3 / T4), most recent first, plus the current state and the
  currently-active study consents. Pass `as_of` (ISO-8601) for point-in-time
  proof of what was in effect at that instant (GDPR Art. 7(1)). Reversible,
  patient-mediated, provable consent — the governance an irreversible data
  lake precludes by construction.
* Derived from the authoritative consent rows, not a parallel table: the
  `Consent` and `TrainingConsent` tables are already append-only episode
  ledgers (a grant inserts a fresh row, a revoke stamps `revoked_at` once),
  and the ledger reads the very rows that gate cohort selection
  (`training_cohort.select_cohort` filters `revoked_at IS NULL`), so it
  cannot drift from what actually governs data use. No migration; works
  retroactively over all existing consent history.
* New MCP tool `get_consent_ledger` on a grantable `consent:read` scope
  (read-only, self-scoped) — MCP stays a superset of the GUI.
* New `ConsentLedgerPanel` on the privacy settings page beside the consent
  toggles and the PHR-Bundle export; en/it i18n parity.
* `list_consents` refactored onto the shared collapse helper (single source
  of the current-state logic, no duplication).

## 4.4.85 (2026-06-29)

### Governance: public applied-policy endpoint + data-governance dossier

* New public, versioned endpoint `GET /api/governance` exposing the
  *applied* data-governance policy as a machine-readable descriptor: the
  de-identification passes, the k-anonymity threshold, the contribution
  tiers, the licences, and the patient rights. Values are sourced from the
  runtime constants the code actually enforces
  (`k_anonymity.DEFAULT_K_MIN`, `deid_text._KIND_TO_PATTERN`) so the
  published policy cannot drift from the code; a DB-free conformance test
  pins it. The endpoint is the auditable, open counterpart to a closed
  irreversible black-box.
* Load-bearing honesty: the policy frames bitvision as pseudonymization +
  tiering + k-anonymity + auditable redaction, and states explicitly it is
  NOT a claim of irreversible-anonymization parity (a test asserts the
  framing).
* New `docs/data-governance.md` dossier mapping every de-id / k-anon /
  audit / erasure / portability module to its file, plus a reproducible
  deploy quickstart.

## 4.4.84 (2026-06-29)

### OpenData: de-identification provenance panel on the study page

* The per-study text de-identification record (`GET
  /studies/{id}/deidentification-provenance`, live since 4.4.78) now has a
  frontend: a collapsible, read-only panel on the study page (rendered
  only when the study is public) showing the redaction counts per category
  (Italian tax code / email / phone / precise date / address / LLM scrub),
  the LLM model + provider when an LLM scrub ran, the totals, and the
  load-bearing scope disclaimer — TEXT de-identification only, NOT DICOM
  PS3.15. It is the auditable, transparent counterpart to an irreversible
  black-box. Aggregate + storage-isolated (counts only); fetches lazily on
  first open. en/it i18n at parity.

## 4.4.83 (2026-06-29)

### PHR-Bundle: the patient's portable health record (+ export builder fix)

* **Fixed a broken export.** The GDPR Art. 20 / data-portability export
  silently failed for every user: the builder still imported the v3-dead
  `Report` symbol (ImportError the moment the worker ran the job) and
  serialized a non-existent `Document.document_type` column. The builder
  now reads `ReportContent` (with the `author_kind` human/agent trail)
  and the real `kind_id` / `file_content_type` / `content_sha256`
  columns, and lists soft-deleted-but-not-purged documents honestly.
* **Formalised the format.** The export is now a self-identifying,
  versioned open container — `bitvision.phr-bundle` — documented at
  `docs/phr-bundle.md` with a published JSON Schema
  (`docs/schemas/phr-bundle.v1.schema.json`) and example. A new
  conformance test (`test_phr_bundle_conformance.py`) pins the schema's
  `format` / `schema_version` to the code so the spec and builder cannot
  drift, and validates the real builder output end-to-end.
* **MCP = GUI parity.** New `export_health_record_bundle` MCP tool
  (account-wide, no DICOM) on its own grantable `health_record:export`
  scope, distinct from per-patient `fascicolo:export`. The
  `POST /api/gdpr/export` route now enforces that scope for agent tokens
  (no-op for human sessions).
* **Discoverable.** The Settings → *Privacy & data* card and the export
  section now name the PHR-Bundle and describe it as a portable,
  re-importable open format.

## 4.4.82 (2026-06-28)

### Visual Search: scope + tier on the search step

* The scope selector and tier badges (4.4.80) were on Step 2 (similar
  cases, shown only after picking a reference); the actual search happens
  in Step 1 (the reference picker), so the search looked unchanged. The
  scope selector (All visible / Mine / Shared with me / Public) and the
  tier badge now sit on the Step 1 picker — where you type the query and
  see the results — wired to `/api/search`'s `scope`. Step 2 keeps them too.

## 4.4.81 (2026-06-28)

### Visual Search: full i18n (en/it)

* The `/search/visual` page was hardcoded in English (including the scope
  selector + tier badges added in 4.4.80), so it ignored the selected
  language. Every user-facing string now lives in a new `visualSearch`
  next-intl namespace and is translated for English and Italian — the scope
  options (All visible / Mine / Shared with me / Public → Tutti visibili /
  Miei / Condivisi con me / Pubblici), the tier badges, the step headings,
  the reference picker, and the not-indexed cards. en/it key sets are at
  parity.

## 4.4.80 (2026-06-28)

### Visual Search: scope selector + tier badges

* The `/search/visual` neighbours view gains a scope selector — All visible
  / Mine / Shared with me / Public — wired to the `/similar-to` `scope`
  param shipped in 4.4.79. It only narrows what you can already see (the
  backend auth filter is the ceiling); 'shared' = studies visible only via
  a grant.
* Each neighbour result card now shows the study's contribution tier (and a
  'public' badge for OpenData), so the tier of every visual-search result
  is visible at a glance.

## 4.4.79 (2026-06-28)

### Search: scope filter + visual-search privacy regression test

* Confirmed and regression-locked that the visual / similarity search
  cannot be used to discover another patient's PRIVATE study. Every
  search surface (`/search`, `/search/hybrid`, `/search/semantic`,
  `/similar-to`) already filters the embedding KNN through
  `visible_studies_filter` (anonymous → only `is_public`; authenticated →
  own + public + OpenData + grants). A new test gives user A's private
  study the vector NEAREST to user B's source and asserts B still does not
  receive it, and cannot even anchor a search on A's private series id
  (404). The owner seeing his own managed patient's exam is correct.
* `scope = all | public | mine | shared` on `/search`, `/search/hybrid`
  and `/similar-to` (and the MCP `similar_to` tool). New `shared` scope =
  studies visible only via a grant (not owned, not public), built from the
  same grant subqueries as the auth filter so the two never drift. Scope
  can only narrow the auth-allowed set, never widen it. No migration.
  (The tier badges + scope selector on the Visual Search page are a
  tracked frontend follow-up; `contribution_tier` is already in every
  result.)

## 4.4.78 (2026-06-28)

### De-identification provenance for OpenData studies

* `GET /studies/{id}/deidentification-provenance` (and the MCP
  `get_deidentification_provenance` tool) return the per-study text
  de-identification record: counts per redaction category (Italian tax
  code, phone, email, precise dates, addresses, LLM scrub) with the LLM
  model / provider when an LLM scrub ran, plus the contribution tier. It
  turns the redaction ledger into the auditable artifact an irreversible
  black-box cannot offer.
* Aggregate + storage-isolated: category counts only, never an excerpt /
  prompt hash, actor, note id, or storage location. A public OpenData
  study's record is public (same gate as the study detail); the read runs
  with service privilege after the boundary check so the table's
  authenticated-only RLS does not silently zero the anonymous record.
* Honest scope: it records TEXT de-identification of clinical notes only.
  DICOM PS3.15 header / pixel handling is applied separately at ingest and
  is explicitly NOT claimed by this ledger. No migration. (Flow 0f706c4a)

## 4.4.77 (2026-06-28)

### Promote live PET-VOI measurements onto a finding

* `POST /findings/{id}/promote-measurement` (and the MCP
  `promote_finding_measurement` tool) recompute a PET Volume-of-Interest
  server-side and materialise its radiomic numbers — SUVmax / SUVpeak /
  SUVmean and MTV (as `volume_ml`) — onto the finding's typed measurement
  columns, so the corpus becomes quantitatively queryable ("confirmed
  nodules with SUVmax > 4"). The number is measured from the pixels, never
  asserted by the caller.
* SUV columns are filled only when the series carries a decay-corrected
  dose; a raw-units VOI yields the volume alone (raw PET counts must not
  masquerade as SUV). The finding's `status` is left untouched (a
  promoted measurement stays `candidate` until a human confirms it); an
  agent token cannot promote onto a human-authored finding; the measured
  series must belong to the finding's study; the placed VOI marker can be
  linked as the `measurement` geometry (idempotently). Audited via a
  finding revision; `Idempotency-Key` + optional `If-Match`. No migration.
* Scope: the VOI sources (the SUVmax / MTV headline) land here; ROI-stats
  (HU) and a hot-spots → multi-finding creation flow are a tracked
  follow-up. (Flow 2e09b6d9)

## 4.4.76 (2026-06-28)

### Ingest reliability

* Resumable uploads are now idempotent on retry. A repeated "start
  upload" with the same owner, target and file manifest maps back to
  the still-active session instead of opening a second one — and, since
  each session has its own staging prefix, a duplicate ingest job at
  commit. The partial-unique index that guards this already existed but
  was never fed: `create_session` now derives the key from the full
  request shape and resolves a retry (race-safe, before the per-owner
  session cap) to the live session. No migration. (Flow ac9731ed)
* A blob uploaded to object storage before its database row is committed
  is no longer orphaned when the ingest transaction rolls back. The
  non-DICOM document upload paths (`bulk_ingest`, `ingest_document_blob`)
  now stage the object against the transaction via a shared helper: a
  rollback best-effort deletes the key, a commit keeps it. The cleanup
  is anchored to the session's transaction outcome, so it covers both
  the caller that owns its commit and the one whose caller commits
  later. Keys never leave the backend. (Flow 4c4c6a7a)

## 4.4.75 (2026-06-28)

### Event ↔ Document reconciliation

* From a clinical event you can now point at the curated documents in
  the Drive instead of keeping isolated copies. Two ways to attach a
  file: upload from the PC (auto-reconciled against the Drive by
  content hash — identical bytes already curated are linked, not
  duplicated) or "attach from Drive" (reference an existing document,
  no re-upload).
* The old promote-to-document stub (which minted a dangling UUID and
  never created a Document) is replaced by a real reconcile-or-ingest
  flow. New `clinical_event_documents` n:m link table (migration 0038)
  replaces the 1:1 `promoted_to_document_id` column; cross-patient
  links stay unrepresentable via the composite FK.
* MCP gains `link_event_document` / `list_event_documents` /
  `unlink_event_document` / `find_documents_by_content_hash`; the
  document references payload gains the back-reference to events.
* `scripts/backfill_event_attachment_hashes.py` hashes + auto-links the
  pre-0038 attachments. New DB-backed CI gate runs the reconciliation
  tests against a real Postgres before a release builds images.

## 3.10.0 (2026-05-22)

### MCP token efficiency + scope-aligned tool listing

* MCP HTTP transport: `list_tools` now returns only the tools the
  calling assistant's bearer scopes can actually invoke, instead of
  the full ~160-tool catalogue. Cuts the schema payload (the dominant
  MCP token cost) for narrowly-scoped assistants and keeps
  `list_tools` and `call_tool` in lockstep (no "visible but 403"
  tools). Logic in the pure helper `_tools_for_principal`.
* Refreshed the source-pinned MCP tool-set tests to cover the
  patient_tasks (v3.4), notifications (v3.5) and `export_event_ics`
  families. Registry verified clean: 162 tools, 162 unique names, no
  duplicates.

### Project status

* Status promoted to **beta**; CI gates + README badges tracked to
  the `v3.10` line (now the default branch).

## 3.9.1 (2026-05-22)

### Slim ARM images: CPU-only torch

* `torch` / `torchvision` are pulled from the PyTorch CPU index in the
  workspace `pyproject.toml`. The default PyPI wheels declared the
  (aarch64-published) NVIDIA CUDA 13 stack as hard deps — multiple GB
  of cuDNN / NCCL / cuBLAS never loaded on the GPU-less ARM cluster.
  Backend image 7.2 → 1.9 GB, workers 7.7 → 2.4 GB: faster pulls and
  lower DiskPressure risk on the Kapsule nodes. Runtime behaviour
  unchanged (inference was already CPU).

## 3.8.0 (2026-05-21)

### God-file splits + isolated sub-component extracts

Closes the two B-tier items deferred from 3.7.12 (the
``api/patients.py`` and ``api/studies.py`` god-files) and lifts the
fully-local sub-components out of the largest frontend file.

* **``api/patients.py`` → ``api/patients/`` package** (5818 LOC →
  one shared module + seven domain children).
  * ``_shared.py`` (162 exported symbols): imports, 38 Pydantic
    schemas, 75 helpers, the canonical ``router`` placeholder.
  * ``core.py`` (6 routes, ~560 LOC): patient list / create / get /
    patch / delete / decode_cf.
  * ``contacts.py`` (6 routes, ~240 LOC): contacts CRUD +
    delegation + telegram link sub-flow.
  * ``fascicolo.py`` (2 routes, ~285 LOC): index + timeline.
  * ``documents.py`` (23 routes, ~2625 LOC): document upload, CRUD,
    versioning, merge, restore, bulk update, link, files, content,
    thumbnail, binary, OCR / entities.
  * ``sharing.py`` (1 route, ~120 LOC): patient-scoped share-link
    mint (cross-patient guard added in 3.7.9 stays in place).
  * ``search.py`` (3 routes, ~455 LOC): patient-scoped search +
    hybrid + chunks.
  * ``publish.py`` (1 route, ~70 LOC): publish to OpenData (F12.4).
  * ``__init__.py`` aggregates every child router via
    ``include_router`` and re-exports every public name from
    ``_shared`` plus each child module so ``from
    bvphoenix.api.patients import update_patient`` keeps resolving.
  * Cross-patient guard test
    (``tests/security/test_patients_cross_patient_guard.py``)
    rewritten to walk every ``*.py`` under the package and assert
    each ``{patient_id}`` write route reaches one of
    ``enforce_agent_patient_scope`` / ``_get_patient_or_404`` /
    ``_resolve_patient_for_write``. CI now keeps the post-split
    safety property as a hard invariant.
* **``api/studies.py`` → ``api/studies/`` package** (3636 LOC → one
  shared module + seven domain children: core, metadata, pet,
  segmentations, registrations, roi_stats, bulk).
* **``viewer/series/[id]/page.tsx``**: 4365 → 3930 LOC. The four
  fully-local sub-components (``WLPresetBar``,
  ``Vr3DRangeControl``, ``Vr3DColorEditor``, ``Vr3DCropBox``) and
  the shared ``WL_BTN_STYLE`` are now their own files
  (``viewerStyles.ts`` + four ``.tsx``). The main
  ``SeriesViewerPage`` component stays put — its remaining state is
  React-intrinsic and would need an architecture rewrite to split
  meaningfully.
* **``CornerstoneMPRLayout.tsx`` (3994 LOC)**: inspected, no
  isolatable sub-components found. The file is a single
  ``forwardRef`` whose state is shared across every render
  callback. Deferred until the underlying state model can be
  refactored.

### Tests

906 backend tests + frontend tsc / vitest, all green. The 21 fail +
24 errors that remain locally are integration tests that need a live
Postgres (``asyncpg.exceptions.InvalidPasswordError``) — pre-existing
per memory ``backend_test_isolation_pre_existing``.

## 3.7.12 (2026-05-21)

### Architectural cleanup (post-audit refactor pass)

Follow-up to the modularity / duplication / layering audit run earlier
the same day. Closes every "quick win" the audit flagged; the two
god-file splits (``api/patients.py`` 5818 LOC and ``api/studies.py``
3636 LOC) stay open as dedicated PRs because clean separation needs
~4 h of review-driven work each.

* **Layer leakage closed**: services/ no longer reaches back into api/
  for helpers.
  * ``api/_etag.py`` → ``services/etag.py`` (5 callers updated)
  * ``api/_schemas_care_phase.py`` → ``services/care_phase_schemas.py``
    (5 callers updated)
  * ``api/patients._document_versioning_payload`` →
    ``services/document_mappers.document_versioning_payload``;
    ``services/document_bulk_update`` no longer needs the lazy
    ``# avoid circular imports`` import.
* **Cross-FSM duplication folded**: the identical ``_check_if_match``
  block in ``api/clinical_events.py`` and ``api/patient_tasks.py``
  now delegates to ``services/etag.enforce_if_match_value``. Wildcard
  ``*`` semantics + 412/428 contract preserved.
* **CLI helper consolidated**: the duplicate ``_resolve_owner`` in
  ``cli/import_dicom`` and ``cli/import_pathology`` is now a single
  ``cli/_common.resolve_owner_by_email``.
* **Frontend lib/api split**: the 4076-LOC monolith is replaced by
  ``lib/api/{core,index}.ts``. ``core.ts`` (305 LOC) holds the
  primitives every domain needs — ``request``, ``ApiError``,
  ``Paginated<T>``, ``qs()`` query-string builder, ``SearchParams``,
  the auth-token compatibility shims. ``index.ts`` (3813 LOC) keeps
  the domain endpoints and re-exports ``./core`` so every
  ``import { ... } from "@/lib/api"`` caller keeps working. A finer
  19-section split was attempted and reverted: domain types
  cross-reference one another (Patient ↔ Study ↔ ShareLink ↔ ...) and
  the per-domain bookkeeping costs more than the file-size win until
  the underlying types are refactored together.

### Deferred to dedicated PRs

* Splitting ``backend/src/bvphoenix/api/patients.py`` (5818 LOC,
  42 routes) into a package ``api/patients/{core,contacts,documents,
  export,publish}.py``. The CI cross-patient guard
  (``tests/security/test_patients_cross_patient_guard.py``) already
  walks the file's AST, so the split is a refactor, not a security
  blocker — but it touches every patient-flow test and needs review.
* Splitting ``backend/src/bvphoenix/api/studies.py`` (3636 LOC,
  30 routes) into a package ``api/studies/{core,segmentations,
  measurements,sharing,similarity}.py``. Same rationale.

### Tests

145 touched backend tests + 122 frontend vitest tests + tsc clean.

## 3.7.11 (2026-05-21)

### Fix: /search now reaches OpenData via filter-only browse mode

Setting ``scope=Public`` (or any sidebar filter) on /search without
typing a query now fires a metadata-mode request and lists the 152
OpenData studies straight away. Hybrid mode still needs a query;
the page falls back to metadata when ``q`` is empty.

CI workflows narrowed: ``ci.yml`` and ``openapi-check.yml`` no longer
trigger on every direct push to the dev branch; only annotated tags
(``v3.7.x``) build images, and the openapi-snapshot drift check runs
on PRs only. ``backend/openapi.json`` regenerated after 3.7.9 added
``POST /api/ai-assistants/{id}/revoke``.

## 3.7.10 (2026-05-21)

### Fix: CSP nonce middleware (hydration unblock)

3.7.9 shipped a CSP via ``next.config.mjs::headers()`` with
``script-src 'self' 'wasm-unsafe-eval'``. Allows the bundled chunks
(``'self'``) but blocks the inline
``<script>self.__next_f.push(...)</script>`` tags Next.js emits for
React streaming hydration (no nonce, not covered by ``'unsafe-inline'``).
Result: pages returned 200 with the right HTML but never hydrated.

Fix: new ``frontend/src/middleware.ts`` generates a fresh nonce per
request, sets ``Content-Security-Policy`` on both REQUEST headers
(so Next.js auto-attaches it to its inline scripts) and RESPONSE
headers (so the browser enforces it). The CSP keeps ``'strict-dynamic'``
+ ``'wasm-unsafe-eval'`` for Cornerstone/VTK. Static CSP removed
from next.config.mjs (would shadow the dynamic one).

## 3.7.9 (2026-05-21)

### Security hardening pass (post-assessment)

A full multi-domain security review on 2026-05-21 surfaced 6 critical
and 11 high findings. This release closes every one of them; full
audit lives in ``docs/security-2026-05-21-assessment.md``. Highlights:

* **C1 — Open redirect on /login**: ``router.push(search.get("next"))``
  is now gated by ``safeInternalPath`` (rejects absolute URLs,
  protocol-relative URLs, ``javascript:`` schemes, NUL bytes,
  backslash escapes). Tests in ``frontend/__tests__/safeRedirect.test.ts``.
* **C2 — Session token leaves localStorage**: backend ``/api/auth/login``,
  ``/login-mfa`` and ``/verify-email`` now set an HttpOnly + Secure +
  SameSite=Lax ``bvp_session`` cookie alongside the legacy JSON body.
  The SPA's ``request()`` helper sends ``credentials: "include"``;
  ``getStoredToken`` / ``setStoredToken`` are no-op compatibility
  shims. XSS-to-account-takeover no longer escalates a script
  execution into a stolen bearer token. CSRF is mitigated by the
  SameSite=Lax cookie + the API's JSON-only mutation contract.
* **C3 — Share-link recipient PII leaked publicly**: ``recipient_name``
  and ``recipient_email`` removed from the unauthenticated
  ``/shared/{token}/info`` response (``ShareInfoOut``). Regression
  test in ``test_share_recipient_pii_redaction.py``.
* **C4 — Pods ran as root**: every Deployment, Job and Dockerfile now
  enforces ``runAsNonRoot: true``, ``allowPrivilegeEscalation: false``,
  ``capabilities: drop ALL`` and ``seccompProfile: RuntimeDefault``.
  Frontend + mcp-http additionally enable ``readOnlyRootFilesystem``.
* **C5 — No CSP, HSTS or X-Frame-Options**: Next.js ``headers()`` emits
  a tight CSP (``script-src 'self' 'wasm-unsafe-eval'``,
  ``frame-ancestors 'none'``, ``connect-src 'self'``) plus HSTS,
  X-Frame-Options DENY, Referrer-Policy, Permissions-Policy. Same
  values re-asserted at the Traefik edge via a dedicated Middleware
  (``ingress/middleware-security-headers.yaml``).
* **C6 — ZIP slip in bulk_upload**: ``_unpack_zip`` and the ISO walker
  reject any member name containing ``..``, an absolute path, a NUL
  byte, or a Windows-style escape. Crafted-archive regression test in
  ``test_bulk_upload_zip_slip.py``.

### High-severity hardening

* **H1 — JWT library migrated to PyJWT**, ``python-jose`` removed
  (CVE-2022-29217 / no upstream maintenance). Every minted token now
  carries ``iss``, ``aud``, ``iat``, ``nbf``, ``exp``, ``jti``; the
  decoder rejects tokens with mismatched issuer / audience or future
  ``nbf``. Clock-skew leeway settable via ``BVP_JWT_LEEWAY_SECONDS``.
* **H2 — Per-assistant secret revocation**: new ``revoked_at`` column
  on ``agent_assistants`` + ``POST /api/ai-assistants/{id}/revoke``
  endpoint that zeros the secret hash, sets ``is_active=false`` and
  stamps the timestamp. The auth resolver rejects rows with either
  ``is_active=false`` OR ``revoked_at IS NOT NULL`` (single-flag
  bypass defeated). Migration ``0006_agent_assistants_revoked_at.py``.
* **H3 — MCP sharing tools accept ``idempotency_key``**:
  ``create_study_share_link`` and ``create_folder_share_link``
  propagate it as ``Idempotency-Key`` so a flaky retry can no longer
  mint two visible share-links.
* **H4+H5 — DB pool sizing**: explicit ``pool_size=10``,
  ``max_overflow=10``, ``pool_recycle=3600``,
  ``connect_args.statement_cache_size=250``. Default 5+10 was
  thrashing asyncpg's statement cache and starving the worker pool
  under multi-agent traffic.
* **H6+H7 — DB CHECK constraints**: ``pathology_slides`` now refuses
  a write that stores ``s3_label_key`` without
  ``label_redacted=true``; ``imaging_studies`` refuses
  ``is_public=true`` outside the T4 tier. Migration
  ``0007_opendata_pathology_constraints.py``.
* **H8 — NetworkPolicy**: explicit per-component NetworkPolicy bundle
  (``deploy/.../network-policies.yaml``) — workers get a hard
  ``deny-all-ingress``, the others limit ingress to a single port.
* **H9 — Tag write race**: ``PATCH /api/studies/{id}/tags`` now takes
  a ``SELECT ... FOR UPDATE`` on the study row so concurrent multi-
  agent tag bulk updates serialise (lost-update bug closed).
* **H10 — WSI file-size cap**: ``import_pathology_slide`` rejects
  files larger than ``BVP_WSI_MAX_BYTES`` (default 30 GiB) before
  OpenSlide memory-maps them.
* **H11 — patients.py cross-patient guard**: every write route under
  ``/patients/{patient_id}/...`` now calls
  ``enforce_agent_patient_scope``. ``create_patient_share`` and
  ``publish_patient`` were the two outstanding holes; a CI guard
  test (``tests/security/test_patients_cross_patient_guard.py``)
  walks the AST and fails the build if a future route slips by.

### Medium-severity batch

* **M1** — split httpx timeouts on OIDC (``connect=5s``, ``read=15s``,
  ``write=5s``, ``pool=5s``).
* **M5** — External Secrets Operator refresh interval lowered from
  ``1h`` to ``5m`` so credential rotations propagate to the cluster
  within the operator's incident window.
* **M8** — bcrypt cost factor pinned to 14 (was implicit 12).

### Internal

* Frontend ``localStorage`` token handling removed across 24 files;
  ``getStoredToken`` / ``setStoredToken`` retained as compatibility
  shims that return ``null`` / no-op. ``credentials: "include"``
  added to every direct ``fetch()`` that previously attached a
  Bearer header.
* ``frontend/next.config.mjs`` now declares a ``headers()`` route
  that ships the security headers above on every response.
* Auth context migrated from token tracking to ``/api/auth/me``
  session probing; ``logout`` is now async and POSTs to
  ``/api/auth/logout`` to clear the cookie server-side.

## 3.0.0-beta.53 (2026-05-04)

### Fixed: patient-scoped folder visibility ignores creator owner

Closing the loop on the same path-resolution bug
(``path segment 'X' not found``): even after beta.51 propagated
``patient_id`` to the orphan subtree, the same URL still 404'd in
production. Root cause: ``_folders_in_patient_tree`` and
``_resolve_path`` filtered ``Folder.owner_subject_id == caller``
across **all** branches, including patient-scoped folders. A folder
created by an agent token (whose subject differs from the human
manager) stayed invisible to the manager despite both having
legitimate access via the patient ACL.

* Patient-scoped folders are now visible to anyone with read access
  to the patient, regardless of who created the row. Owner remains
  the filter for personal-workspace folders (no patient_id set).
* The parent/children index used by the walk-UP / walk-DOWN passes
  is built across both lenses (``owner_subject_id == caller`` OR
  ``patient_id == patient.id``), so the walks can cross owner
  boundaries when (and only when) the bridge node is patient-scoped.
* ``_resolve_path`` now matches against ``Folder.id IN tree_folders``
  directly, delegating visibility entirely to that set. The
  ``owner_subject_id`` parameter is kept for signature compatibility
  but ignored (a re-introduction of the filter would re-introduce
  the bug).
* ``POST /api/folders`` and ``PATCH /api/folders/{id}`` accept a
  patient-scoped parent regardless of who created it; the patient
  access check downstream is the actual gate. Without this the
  beta.51 inheritance fix would have refused new subfolders under
  patient-scoped parents created by an agent.

## 3.0.0-beta.51 (2026-05-04)

### Fixed: subfolder ``patient_id`` inheritance and patient-tree descent

Two architectural bugs combined to make a subfolder under a
patient-scoped parent disappear from the fascicolo Drive UI with a
``404 path segment 'X' not found`` error.

1. ``POST /api/folders`` accepted ``patient_id`` independently from
   ``parent_folder_id``. UI/agent calls that passed
   ``parent_folder_id`` but omitted ``patient_id`` produced an orphan
   subtree (``patient_id = NULL``) under a patient-scoped parent.
   Fixed: ``patient_id`` now propagates from the parent. A body that
   passes a different ``patient_id`` is rejected (400).
2. ``PATCH /api/folders/{id}`` accepted any ``parent_folder_id``
   without checking that the new parent shared the same
   ``patient_id``. Fixed: a move whose new parent has a different
   ``patient_id`` is rejected (400). Cross-patient grafting is no
   longer expressible via the API.
3. ``_folders_in_patient_tree`` only walked UP from leaf items and
   patient-scoped folders, never DOWN. An empty / orphan-leaf
   intermediate folder fell off the tree. Fixed: walk DOWN from
   patient-scoped seeds to include all descendant scaffolding (year,
   topic, lesion nodes); leaf-only seeds still walk UP only.

Backfill for already-orphan branches:
``backend/scripts/repair_subfolder_patient_id.py [--owner-subject-id
<uuid> | --all-owners] [--dry-run]`` propagates ``patient_id`` from
each patient-scoped folder to its ``NULL`` descendants. Conflicting
descendants (different ``patient_id``) are reported, never
overwritten. Pure-logic walker covered by
``tests/test_repair_subfolder_patient_id.py``.

## 3.0.0-beta.50 (2026-05-04)

### Fixed: bulk-uploaded documents land in the chosen folder

Stage 5 of `services/bulk_ingest.py` (folder linking) was inserting a
`folder_items` row only for `studies_created`, never for
`documents_created`. PDFs / images / text files uploaded via the
fascicolo's InlineFascicoloUploader to a specific folder ended up in
the patient's namespace but with no link to `target_folder_id`, so
the Drive UI showed them in the root instead of the chosen folder.
Symmetric to the ISO archive path which already creates the
FolderItem row in the worker. Re-uploading the same file does not
fix it: the dedup pass on `content_sha256` skips before stage 5 runs.

Backfill for documents already orphaned by this bug:
`backend/scripts/repair_orphan_document_folder_items.py
--patient-id <uuid> --folder-name <name>` (or `--folder-id <uuid>`)
inserts the missing FolderItem rows. `--dry-run` previews. ISO bundles
are excluded by default; use `repair_iso_folder_items.py` for those.

## 3.0.0-beta.4 (2026-05-03)

### Added: care timeline and clinical phases

Per-patient semantic timeline that groups `clinical_event` rows into
clinically meaningful chapters (`imaging`, `surgery`, `followup`,
`surveillance`, `visit`, `reassessment`, `other`). LLM classifier
proposes phases, humans review and edit, the result is rendered as
JSON, Markdown or SVG (server-side, byte-identical layout to
the local reference SVG (not committed)). Cross-patient operations
are unrepresentable end-to-end (composite FK, scoped REST namespace,
MCP `patient_id` first-input invariant). Full spec in
[docs/care-timeline-phases.md](docs/care-timeline-phases.md).

**Database** (Alembic `0080_care_phase`):

- New tables `care_phase`, `care_phase_revision`, `care_phase_proposal`.
- New columns on `clinical_event`: `phase_id`, `phase_assigned_by`,
  `phase_assigned_at`, `phase_assignment_confidence`.
- Composite FK `(patient_id, phase_id) -> care_phase (patient_id, id)`
  ON DELETE SET NULL; cross-patient assignment rejected at the DB
  layer.

**Backend REST** (17 endpoints under
`/api/patients/{patient_id}/...` plus `GET /api/me/scopes`):

- Read: `GET care-phases`, `GET care-phases/{phase_id}`, `GET
  care-phases/{phase_id}/material`, `GET
  care-phases/{phase_id}/revisions`, `GET care-timeline?lang=&format=`
  (`json|svg|markdown|ics|pdf`; PDF returns `501` until weasyprint is
  bundled), `GET care-timeline/health`.
- Write: `POST care-phases`, `PATCH care-phases/{phase_id}` (`If-Match`
  required, `412` on mismatch, `428` when missing), `DELETE
  care-phases/{phase_id}`, `PUT/DELETE
  care-phases/{phase_id}/events/{event_id}`, `POST
  care-phases:propose` (with `?async=true` to enqueue on arq instead
  of running in-request), `POST care-phases:apply-proposal`
  (`Idempotency-Key` required, `428` when missing), `POST
  care-phases:reorder`, `POST care-phases/{phase_id}/restore`.
- Caller introspection: `GET /api/me/scopes` returns the OAuth scopes
  the current token holds, used by the GUI to gate buttons and by MCP
  agents to decide whether to attempt a write.
- Full reference: [docs/api-reference.md §10b](docs/api-reference.md).

**MCP** (19 new tools in `mcp/src/bvmcp/tools/care_phases.py` —
strict superset of the GUI per memory
`feedback_mcp_must_be_gui_superset`):

- Read: `get_care_timeline`, `render_care_timeline_svg`,
  `get_care_phase`, `list_care_phases`, `list_care_phase_material`,
  `list_care_phase_revisions`, `get_care_timeline_health`,
  `export_care_timeline_ics`, `export_care_timeline_pdf` (stub
  returning structured 501).
- Write: `propose_care_phases`, `apply_phase_proposal`,
  `create_care_phase`, `update_care_phase`, `delete_care_phase`,
  `assign_event_to_phase`, `unassign_event_from_phase`,
  `reorder_care_phases`, `restore_care_phase_revision`.
- Introspection: `get_my_scopes` (only tool not patient-scoped).
- Full reference: [docs/agent-protocols.md §1](docs/agent-protocols.md).

**Worker**: arq task `propose_care_phases` registered in
`workers/src/bvworkers/tasks/registry.py`; reuses the classifier code
path from a worker process to keep long classifier runs off the
request thread.

**Scopes**: `phases:read`, `phases:propose`, `phases:write` added to
the catalog (`mcp/src/bvmcp/scopes.py`); grantable independently per
AI assistant via `/settings/ai-assistants`.

**Frontend**:

- Tab Events on the Health Record now mounts `<CareTimeline />`
  (vertical chips + dots layout). The previous
  `ClinicalEventsTimeline` is removed.
- New per-phase page `/patients/[id]/care-phases/[slug]` with header,
  narrative, sub-tabs for studies, documents, reports / consultations,
  annotations, and an inline phase editor (HTML5 DnD, revision panel
  with restore).
- New components: `CareTimeline`, `PhaseChip`, `TimelineEventDot`,
  `CarePhaseEditor`. Theme dark/light, WCAG-corrected text, URL state
  for filters and expanded set, export buttons (SVG, PDF, iCal).

### Tooling, lint, tests

- `backend/scripts/lint_patient_scope.py` enforces `patient_id` as
  the first kw-only argument on every public function in
  `services/care_phases.py` and `services/care_phase_classifier.py`,
  closing the loop on the cross-patient invariant at the service
  layer (defence in depth on top of the composite FK and the nested
  REST routes).
- `@media print` rule added to `frontend/src/app/globals.css`: hides
  the React timeline and shows the backend-rendered SVG inside
  `[data-care-timeline-print]` so printed copies match the reference
  visual byte-for-byte.
- Backend tests: `tests/test_care_phases_model.py` (raw-SQL composite
  FK rejection), `tests/test_care_phase_svg.py` (4 snapshot tests),
  `tests/integration/test_care_phase_e2e_no_anthropic.py` — full
  pipeline (propose → apply → 7 phases with correct event-to-phase
  assignment) using a deterministic `FakeLLM` provider that needs no
  Anthropic API key. Same canary file
  `tests/integration/test_classifier_canary.py` runs the real
  Anthropic-backed 7/7 golden when `BVP_LLM_PROVIDER=anthropic` and
  the API key are configured.
- MCP tests: 74/74 passing; `test_no_tool_takes_two_patient_ids`
  guards the cross-patient invariant at the MCP layer.
- Frontend tests: 16/16 contract tests; Playwright spec
  `frontend/e2e/care-timeline.spec.ts` covers a 10-step scenario
  (mocked APIs by default, real backend via `E2E_USE_REAL_BACKEND=1`).
  Activate with `pnpm add -D @playwright/test`.

### Documentation

- Added care timeline section to
  [docs/fascicolo.md](docs/fascicolo.md).
- Extended [docs/api-reference.md](docs/api-reference.md) with the
  17 care-phase endpoints plus `/api/me/scopes` (cross-patient
  invariant + ETag / Idempotency-Key contract + `?async=true` on
  propose + `?format=ics|pdf`).
- Extended [docs/agent-protocols.md](docs/agent-protocols.md) with the
  19 MCP tools and the 3 new scopes; explicit GUI-vs-MCP superset
  note.

### Known follow-ups

- Classifier execution against the real Anthropic API on patient
  Patient X (canary) pending an online run with a real API key. The
  `FakeLLM` E2E test above already exercises the same 7/7 acceptance
  bar deterministically, so the prompt iteration can happen against
  it before paying for live calls.
- PDF export endpoint returns a structured `501` until `weasyprint`
  is added as a runtime dependency. The MCP tool name
  (`export_care_timeline_pdf`) and the REST query param
  (`?format=pdf`) are stable so the GUI button and any agent share
  the contract today.
