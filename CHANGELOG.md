# Changelog

All notable changes to bitvision phoenix are listed here. The
project follows semantic versioning; pre-release suffixes (`alpha`,
`beta`) gate Kubernetes deployments via the GHCR image tag (without
the leading `v`, see deployment guide).

## 4.4.104 (2026-07-01)

### Workers: shared bounded DB engine for the embed tasks (connection-leak fix)

* The BGE-M3 / MiniLM / BiomedCLIP-text embed tasks created a fresh async engine
  (and pool) *per job* and disposed it *outside* the `async with`, so an error
  (a `TooManyConnectionsError` under load) skipped dispose and leaked the engine
  + its connection — and every arq retry leaked again. The 4.4.103 study-vector
  backfill (`embed-studies --all`, 12k jobs) drove HPA to 4 workers whose per-job
  pools exhausted Postgres `max_connections`, so the jobs then failed 100% and
  thrashed (the app stayed healthy on its established pool throughout).
* Fix: one bounded engine per worker created in arq `startup`
  (`pool_size=5, max_overflow=0, pool_pre_ping, pool_recycle`) and disposed in
  `shutdown`; the embed tasks use `ctx["db_engine"]`. Caps each worker to
  `pool_size` connections regardless of `max_jobs`, and removes the leak-on-error
  (no per-job dispose to skip). Workers image only. One-shot low-volume tasks keep
  their per-job engine (they don't flood).

## 4.4.103 (2026-07-01)

### Search: find_similar_findings + study-level dense-text coverage

* **find_similar_findings** (task c390b2a5) — cohort-by-lesion visual discovery,
  the core value of a biobank. New `GET /api/findings/{id}/similar` + MCP tool
  (`findings:read`, read-only): given a finding, return findings on studies whose
  imaging is visually similar via the existing BiomedCLIP series vectors, reusing
  the tested `/similar-to` ANN. Visibility-scoped (never cross-patient), excludes
  the anchor's own study and soft-deleted/retracted findings; PHI-free,
  retrieval-not-diagnosis. `bvphoenix-backfill embed-findings` gives pre-existing
  findings their coarse text vector after a new text model is activated.
* **Study-level dense-text coverage** (task 0ece383b) — the `text_dense` arm
  projected only `report_content` / `finding` vectors, which real exams (DICOM SR,
  few app findings) rarely have, so in prod it contributed ~0. Every study now
  carries a coarse `study` vector composed from its structural metadata
  (description + modalities + series body parts): migration `0041` admits
  `target_kind='study'` on both text stores, `study_text_search` reads it directly
  (visibility-intersected), on-write hooks (ingest finalize / patient assignment /
  study rename + metadata edit) keep it fresh, and `bvphoenix-backfill embed-studies`
  (`--all`, incl. public OpenData that had no dense text) is the catch-up.
* Backend + workers images. Requires the `0041` migration and, to populate the new
  vectors, the `embed-studies` / `embed-findings` backfill Jobs (Arq worker with
  the `ai` extra computes them).

## 4.4.102 (2026-07-01)

### Search: multilingual dense-text arm in hybrid search

* `/api/search/hybrid` gains a 4th signal, `text_dense`, fused via RRF beside
  the existing `tag` / `text` (lexical) / `image` (BiomedCLIP) arms. It encodes
  the query with the active registry text model (MiniLM / BGE-M3, the same
  encoder the per-patient chunk search uses — in-process, no BiomedCLIP), runs
  an ANN over the coarse whole-object text vectors that map to a study
  (`report_content` → study via its clinical_event; `finding` → `Finding.study_id`),
  and projects the hits to a study ranking. Unlike the lexical `text` arm this
  is semantic and multilingual: "neoplasia epatica" can rank a study whose
  report reads "hepatic tumour" with no shared token — recall the IT/EN
  thesaurus alone can't reach.
* Visibility is enforced in the projection (each candidate study is intersected
  with the caller-visible set), so the arm can never surface a study the caller
  couldn't already read. It degrades to an empty contribution (never raises) when
  the registry has no routed text model, the encoder can't load, or the store is
  unprovisioned — mirroring the other arms' fail-soft behaviour.
* Backward-compatible: `text_dense` defaults to weight 2, so existing
  `tag:2,text:1,image:2` weight strings still parse and inherit it. No migration,
  no new worker, no backfill — it projects from the coarse text vectors already
  written on the report_content / finding write paths. Backend image only.

## 4.4.101 (2026-07-01)

### Search: activate BiomedCLIP image search in prod (inference-svc)

* Semantic image search (`/api/search/semantic?model=biomedclip`) and the
  `image` arm of `/api/search/hybrid` returned nothing in prod: the backend
  image deliberately omits `open-clip` (BiomedCLIP runs only in the arq
  workers), so the in-process query encoder raised 503 / silently produced no
  image signal. Meanwhile 32,572 series image vectors (`biomedclip-v1`) were
  already indexed and idle.
* Fix is the architecturally-intended one (search-overhaul Phase E), not a
  regression: **build and deploy the already-written `inference-svc`** — a
  lean CPU ONNX BiomedCLIP dual-encoder (`POST /encode`, no torch/open-clip
  at runtime) — and point the backend at it via `BVP_INFERENCE_SVC_URL`.
  `embed_query_biomedclip` and the hybrid image arm already prefer this
  out-of-process encoder and fall back transparently, so **no backend code
  change**: this release only adds `inference-svc` to the CI image matrix so
  the image is built + mirrored to the Scaleway registry. The service holds
  no secrets, no patient data, and never touches S3/DB (storage isolation).
* Deploy side (bvphoenix-deploy): apply `inference-svc.yaml` and set
  `BVP_INFERENCE_SVC_URL=http://bvphoenix-inference-svc:80` on the backend.

## 4.4.100 (2026-07-01)

### Public dataset catalog (browsable + citable OpenData commons)

* New public, anonymous-friendly read surface over the OpenData library:
  `GET /api/catalog/collections` (every public collection with aggregate
  counts, modalities, license, commercial-use flag),
  `GET /api/catalog/collections/{slug}` (one collection in full + a few
  sample studies + a DataCite-4 metadata block), and
  `GET /api/catalog/collections/{slug}/citation?format=text|bibtex|ris|datacite`.
  This is the *read* side of `services.public_dataset` (which ingests):
  it groups public studies by their upstream `source_collection` into
  TCIA/IDC-style collections and turns the per-study provenance into a
  citable catalog. Fills the largest gap a microsite-only competitor
  leaves: a real, browsable, citable commons.
* **Storage isolation by construction.** The aggregation hard-codes
  `is_public` and never consults the caller, so no path can surface a
  private study; only aggregate counts and attribution metadata (license,
  citation) leave the service — no S3 keys, no patient identity. A DB CHECK
  invariant (`is_public ⇒ t4 ⇒ source_collection IS NOT NULL`) makes the
  membership filter provably total.
* **Citation.** Each collection carries a stable local PID
  (`bitvision:dataset:<slug>`) and a resolvable landing URL; the upstream
  academic citation is surfaced verbatim and, when it carries a DOI, the
  DataCite record back-links it as `IsDerivedFrom`. A real DataCite DOI is
  a fast-follow (external account) — the metadata is already DOI-ready.
* **Next.js landing.** `/datasets` (collection grid) and `/datasets/{slug}`
  (detail + copy-citation + BibTeX/RIS/DataCite download), reachable from
  the site header for anonymous external citers. No auth, no PHI.
* **MCP parity** (`catalog:read` scope): `list_public_datasets`,
  `get_public_dataset`, `get_dataset_citation` — the MCP stays a superset
  of the browse surface.
* Backend + frontend + mcp-http images; no workers change, no migration
  (the catalog is derived purely from existing columns).

## 4.4.99 (2026-06-30)

### Findings: SNOMED CT codes for the controlled vocabulary

* Migration `0040` fills in the `code`/`code_system` columns that `0020`
  deliberately seeded NULL, anchoring the Finding controlled vocabulary to
  **SNOMED CT** (FHIR-native, interoperable): all 18 anatomy sites, 15/16
  finding types (`other` left NULL), and the 6 morphology terms with a clean
  dedicated SNOMED radiographic margin/shape concept (spiculated, lobulated,
  circumscribed, irregular, ill_defined, necrotic). Descriptors with no clean
  1:1 concept (smooth, well_defined, solid, part_solid, ground_glass,
  calcified, cavitary) stay NULL rather than mapped approximately. Every
  concept id was verified against the live EBI OLS4 SNOMED ontology.
* The migration is idempotent (`UPDATE ... WHERE code IS NULL`, never clobbers
  a manual code) and reversible (downgrade clears only the rows it stamped).
* The training-labels manifest now emits each code as `{system, code}` instead
  of a bare `{code}`, so a SNOMED concept id is unambiguous. Backward-compatible
  addition to the `bvphoenix.training-labels/v1` schema. Backend + workers
  images + a `migrate-job` run; no frontend change.

## 4.4.98 (2026-06-30)

### Training: COCO / nnU-Net / MONAI cohort export serializers

* The de-identified training-cohort bundle gains a `format` axis
  (`POST /api/training-exports`, MCP `export_training_cohort_bundle`): a cohort
  can now ship in the exact layout researchers ingest — `nnunet` / `monai`
  (NIfTI image+label pairs + `dataset.json`) or `coco` (per-slice PNG + RLE
  annotations) — alongside the default `bvphoenix` (raw DICOM + `.bin` masks +
  `labels.json`). Builds on the geometry-preserving DICOM SEG work (4.4.96).
* Geometry is correct by construction: the image volume and the label volume
  are stacked from the SAME source instances sorted by the canonical volume key
  the SEG export uses, so `image[k]` and `mask[k]` line up voxel-for-voxel — no
  world↔image projection (the usual silent-misalignment footgun). NIfTI uses a
  spacing-only diagonal affine (matching the TotalSegmentator writer); image and
  label share it. A mask whose voxel count ≠ the image volume is **refused**, so
  a mis-rasterized label is never published as ground truth.
* A dataset-wide class→int index keeps per-case labels mutually consistent
  (0 = background); nnU-Net `dataset.json` carries `channel_names` + `labels`,
  MONAI an MSD-style datalist, COCO uncompressed RLE + bbox + categories.
* Same safety envelope as the raw bundle, re-validated at run time: training
  consent + contribution tier (t3/t4) + k-anonymity + synthetic re-keying (no
  patient / study / author identifier reaches the artifact). The burned-in-PHI /
  face-risk gate runs per series — one high-risk slice drops the WHOLE series
  (a volume can't ship with a PHI hole), and every drop is recorded, never
  silent.
* No new dependency (`nibabel` + `Pillow` already present); no DB migration.
  Backend + worker + mcp images.

## 4.4.97 (2026-06-29)

### Chore: MCP tool lint cleanup

* Cosmetic-only cleanup of three MCP tool modules (quote-style normalization,
  drop of an unused import, one-line string collapse). No behavior change;
  affects the mcp-http image only.

## 4.4.96 (2026-06-29)

### Imaging: geometry-preserving DICOM SEG export

* Stored segmentation masks (headerless raw `.bin`, no geometry) can now be
  exported as a conformant, geo-referenced **DICOM SEG** object (SOP class
  `…66.4`) that references the source series — openable in 3D Slicer / OHIF /
  MONAI. The serializer reads the source instances back, sorts them by the same
  key the volume-build used so `mask[k]` lines up with `source[k]`, and lets
  highdicom copy the exact per-frame `ImagePositionPatient`/orientation/spacing +
  `FrameOfReferenceUID` + `ReferencedSeriesSequence`. A slice-count/plane
  mismatch (multi-stack / resampled) is refused, never emitted mis-aligned.
* AI provenance preserved: agent/automatic masks are declared `AUTOMATIC` with an
  `AlgorithmIdentificationSequence` (DCM 123110 "Artificial Intelligence"); human
  masks are `MANUAL`. Multi-label masks map each `label_map` value to a segment.
* `GET /series/{id}/segmentations/{label}/dicom-seg` returns the bytes inline
  (sync, storage-isolated) for the GUI/viewer; `POST …/dicom-seg/export` enqueues
  an async Job whose `.dcm` artifact is fetched off-platform via the standard
  job-result download token — with an `export_segmentation_dicom_seg` MCP tool
  (poll `get_job`, then `issue_download_token`). Unblocks the COCO/nnU-Net/MONAI
  cohort serializers. No new dependency (highdicom already present).

## 4.4.95 (2026-06-29)

### Search: reach studies by structured fields + clinical IT/EN thesaurus

* `/api/search` free-text now also matches the **structured fields** — the
  body part and the DICOM modality code — not just study/series descriptions.
  A study whose clinical meaning lives in its modality (e.g. a mammography with
  a null `StudyDescription`) or body part is now reachable from the search bar.
* Migration `0039` enriches the synonym thesaurus with a curated bilingual
  IT↔EN + code core (anatomy, oncology, the cholangio/mammography families), so
  an Italian query OR-expands to its English/coded equivalents:
  `fegato`→liver, `mammografia`→{mammography, MG, breast}, `colangio`→{cholangiography, MRCP},
  `cancro`→{cancer, carcinoma, …}. Idempotent — union-merges the existing rows.
* Follow-ups tracked: route study/report discovery through the multilingual
  vector arm (Phase 2); re-enable BiomedCLIP image search in prod (Phase 3).

### De-identification: wire the face-risk review signal into egress gates (M6d)

* When de-facing is enabled (`face_deid_enabled`), a face-risk instance
  (head/face CT/MR/PT) is no longer auto-passed: `PixelPhiCheck` routes it to
  human review and records the de-facer outcome, and the training-cohort export
  excludes it alongside high-risk burned-in-pixel instances. With de-facing off
  (default) behaviour is unchanged — no regression.

## 4.4.93 (2026-06-29)

### Findings: create candidate findings from hot spots

* New `POST /series/{id}/findings-from-hot-spots` runs the hot-spot lesion
  finder and creates **one `candidate` finding per detected spot** — a
  creation flow, distinct from promote-measurement (which writes onto one
  existing finding). Each finding gets the spot's `volume_ml` (+ SUVmax/peak/
  mean when the series is a dosed PET), `author_kind=agent`, and a
  `bbox.lesion` marker (box + metrics) linked as its `bbox` geometry.
* **Idempotent on the spot signature** (centroid + bbox, stored in the
  marker's `computed`): re-running detection on a series does not duplicate
  findings for spots already materialised. Patient-write gated; findings stay
  `candidate` for a human to confirm or retract.
* The hot-spot detector is now a reusable `compute_hot_spots_core` (extracted
  from the `/series/{id}/hot-spots` route, no math change). New MCP tool
  `create_findings_from_hot_spots` (`findings:write`). This completes the
  finding measurement-promotion task (ROI-stats + measure_volume landed in
  4.4.92).

## 4.4.92 (2026-06-29)

### Findings: promote ROI-stats + measure_volume measurements

* The measurement-promotion endpoint (`POST /findings/{id}/promote-measurement`,
  PET-VOI in v4.4.77) now accepts two more sources, so a finding's
  quantitative columns are measured server-side from pixels for structural
  ROIs too, not just PET VOIs:
  * `roi_stats` — a bbox/sphere ROI → `hu_mean`/`hu_std` (Hounsfield on CT),
    or the `suv_*` columns when the ROI request carries a PET `suv_variant`.
  * `measure_volume` — two bbox corners → `longest_diameter_mm` (max extent),
    `short_axis_mm` (median extent), `volume_ml`.
* The ROI-stats math is now a reusable `compute_roi_stats_core` (extracted
  from the `/series/{id}/roi-stats` route with no change to the numpy
  computation or its memory-release behaviour), so the promotion path
  re-runs the exact same number. Each source resolves its own series +
  `READ_PIXELS` gate and re-asserts the series belongs to the finding's
  study; SUV ROIs never write a PET value into the HU columns. The MCP
  `promote_finding_measurement` tool gains both sources.
* `status` is still left untouched (a promoted measurement stays
  `candidate` until a human confirms it). Hot-spots → multi-finding
  creation is a tracked follow-up of the same task.

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
