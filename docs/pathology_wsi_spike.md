# Pathology / WSI support — spike + Step 1 implementation notes

**Status**: Step 1 landed in 3.7.8 (2026-05-20). Steps 2 (viewer +
polygon annotations) and 3 (production-grade, DICOMweb transcoding,
LIS integration) remain open.
**Audience**: bvphoenix maintainers + product owner.

Section 1-10 of this document is the original spike that scoped what
it would take for BitVision to handle digital pathology (whole-slide
imaging, "vetrini") alongside DICOM radiology. Section 13 captures
what Step 1 actually shipped on 2026-05-20 and points at the live
code.

## 1. The domain in one paragraph

A pathology slide is a glass coverslip stained with a histology stain
(H&E, special, IHC) and scanned at 20×–80× magnification by a desktop
scanner (Hamamatsu, Leica Aperio, Philips UFS, 3DHISTECH, Roche).
A single scan is a multi-resolution pyramid (1–10 GB) holding ~10⁹
pixels at the base level. A patient case typically carries 3–20
slides spanning different blocks of the same specimen, stained with
different markers. The pathologist navigates them in a tile viewer at
arbitrary zoom, marks ROIs, and signs out a free-text report.

## 2. File formats — what arrives from labs

The DICOM-WSI standard (Supplement 145) exists since 2010 but uptake
is uneven. In practice a `pathology_slides/` ingest queue sees:

| Format | Vendor / origin | Notes |
|---|---|---|
| `.svs` | Aperio (Leica) | TIFF derivative, ICC profile, most common in US labs |
| `.ndpi` | Hamamatsu | Custom JPEG2000 tiling, label image embedded |
| `.mrxs` | 3DHISTECH | Folder of .dat + .ini + slides |
| `.tif` / `.tiff` (pyramidal) | OME-TIFF, generic | Multi-page tiled TIFF, open + widely interoperable |
| `.scn` | Leica | TIFF derivative |
| `.svslide` | Sakura | Encrypted SVS variant |
| `.dcm` (WSI) | DICOM Supp 145 | Rare in the wild, common in newer cloud platforms |
| `.iSyntax` | Philips UFS | Proprietary; needs Philips SDK |

A pragmatic ingest should support SVS + NDPI + OME-TIFF + DICOM-WSI at
minimum. MRXS and iSyntax can wait (vendor SDK / multi-file packaging
shift the complexity).

## 3. Reader libraries

| Library | Strengths | Caveats |
|---|---|---|
| **OpenSlide** (`openslide-python`) | The de-facto standard, covers SVS/NDPI/MRXS/OME-TIFF/SCN; small dep | C library, no native DICOM-WSI support |
| **pyvips** (libvips) | Fast tile + thumbnail extraction, scriptable for DZI generation | Heavier install (libvips + bindings); some less-common formats missing |
| **DICOMweb / dicomweb-client** | Native DICOM-WSI handling, integrates with the existing radiology DICOMweb pipeline | Only covers DICOM-WSI input, not SVS/NDPI |
| **RAPIDS cuCIM** | GPU-accelerated tile reads (10×+ vs CPU on multi-slide compositing) | Needs CUDA, ARM64 wheel availability is a moving target — incompatible with our Scaleway Kapsule ARM pool |
| **Bio-Formats** (Java) | Widest format support including microscopy formats | JVM dep, slower, awkward to ship in a Python service |

Recommended stack for v1: **openslide + pyvips** in the backend image,
DICOM-WSI handled through the existing DICOMweb path. cuCIM is a
non-starter on the current ARM cluster.

## 4. Storage model — three options

A 10⁹-pixel slide cannot be served as a single asset. It must be
exploded into a tile pyramid so the viewer fetches only what the user
sees.

**A. Deep Zoom (DZI)** — generate at ingest, store as
`s3://.../slide-uuid/level_{N}/{col}_{row}.jpg`. Pro: simplest, served
from any HTTP cache, no backend tile API. Con: 2–4× storage blow-up
(JPEG tiles + DZI XML); regenerating to change tile size or quality
requires a full rerender.

**B. IIIF Image API 3.0** — store the source pyramid intact (or pre
-tiled) and front it with an IIIF server (`iipsrv`, `cantaloupe`,
`pyramidal-tiff-server`). Pro: standard URL contract, ROI/zoom by
URL, no JPEG blow-up. Con: extra service to deploy, latency depends
on the server.

**C. DICOMweb WADO-RS WSI** — store as DICOM-WSI (or transcode at
ingest), serve through the radiology DICOMweb path. Pro: one server,
one auth model, fits the existing OHIF / Cornerstone WSI plugin. Con:
DICOM-WSI is verbose (per-frame metadata × pyramid levels), encoding
SVS → DICOM-WSI is non-trivial and slow.

For our context (everything else is DICOM-shaped, ARM cluster, AGPL
codebase) **C is the architectural fit**, but A is the cheapest first
step. Recommendation in §10.

## 5. Viewer

OHIF v3 ships an experimental WSI extension built on Cornerstone3D
that consumes DICOMweb WSI. **OpenSeadragon** is the canonical
DZI/IIIF viewer in the JS ecosystem (small, mature, framework-free).
Cytomine / QuPath JS bridges are heavier; out of scope.

Annotation coordinate system differs from radiology: pathology
annotations are polygons in pyramid-level coordinates, normalised to
the base level so they survive zoom-out. Existing `annotations` table
(rectangle/marker in voxel coords) needs a new `kind='polygon_wsi'`
or a sibling table.

## 6. PHI risk

This is the single most under-appreciated issue. Three vectors:

* **Label image** — the photo of the glass slide's printed label.
  Open the SVS, ask OpenSlide for `slide.associated_images['label']`:
  on most lab outputs it is a JPEG of the handwritten / printed
  patient name + MRN + date of birth. Hard PHI. Must be stripped
  before ingest for OpenData and before share-link / export for
  private data.
* **Macro image** — a 1× overview of the slide; usually clean but
  occasionally includes lab barcode tying back to the LIS.
* **EXIF / TIFF tags** — scanner make, slide ID at acquisition,
  sometimes operator name. Need to walk the tag dictionary at ingest
  and drop the unsafe ones.

The existing DICOM de-id pipeline does not cover these. Need a
parallel `wsi_deid.py` that wraps OpenSlide's associated_images API
and writes a scrubbed copy.

## 7. Storage cost estimate

Compressed at the source, an average SVS H&E slide is 0.5–3 GB; IHC
slides cluster around 2 GB. Assuming a real lab feed:

| Volume | Raw GB | DZI overhead (×2.5) | S3 / month @ €0.012/GB |
|---|---|---|---|
| 100 slides | 200 GB | 700 GB | €8.4 |
| 1 000 slides | 2 TB | 7 TB | €84 |
| 10 000 slides | 20 TB | 70 TB | €840 |

DICOMweb WSI is closer to ×1.4 overhead vs DZI's ×2.5, so the
medium-term storage win of option C is real.

## 8. Fit with the bvphoenix data model

Pathology fits the existing patient → clinical_event → asset shape
naturally, but the leaf differs:

* New table `pathology_slides` with columns
  `id, patient_id, clinical_event_id, slide_block_label, stain,
  scanner_make, scanner_model, magnification, base_width, base_height,
  pyramid_levels, source_format, ingest_format, s3_thumbnail_key,
  s3_label_key (nullable, may be redacted), license_spdx, ...`.
* `clinical_event.kind = 'pathology_slide'` (new enum value).
* The existing `tags` table covers slide labels (stain, organ,
  finding) without changes — `target_kind='pathology_slide'`.
* Reports / sign-outs reuse `reports` table — the diff is the
  embedded ROI image extracted from a slide tile at known coords.

Migration: 1 new table + 1 enum extension + 1 partial unique on
`(s3_key)` for idempotency. Modest.

## 9. MCP surface (per memory `feedback-mcp-must-be-gui-superset`)

Any UI capability needs an MCP twin. Day-one tools:
`list_pathology_slides`, `get_pathology_slide`, `ingest_pathology_slide`,
`extract_slide_roi`, `write_pathology_annotation`. Scopes
`pathology:read` and `pathology:write` orthogonal to imaging scopes.

## 10. Recommendation — three-step roadmap

### Step 1 (demo / showcase) — 5-7 dev-day

Goal: a vetrino imported via CLI appears as a card in the patient
fascicolo with thumbnail + macro + stain + magnification, marked
`tier=t4` for OpenData if it came from a public source. No viewer
yet.

Tasks:
* `openslide` + `pyvips` to backend image
* Migration `0005_pathology_slides`
* `bvphoenix-import-pathology` CLI (mirrors `bvphoenix-import`)
* `wsi_deid.py` (strip label by default)
* TreeNode + `_pathology_node` so the patient page renders the card
* Smoke test with 2–3 public OpenSlide example slides (CMU-1, CMU-2 — CC-BY)

This proves the data model. Pathologists do not yet get value, but
the rest of the platform (search, tags, sharing, audit) starts
working on the new entity for free.

### Step 2 (MVP) — 3-4 weeks

Goal: pathologist can open a slide, pan, zoom, drop polygon ROIs,
save annotations.

Tasks:
* DZI generation worker (pyvips dzsave) emitting tiles to S3
* OpenSeadragon-based viewer route `/pathology/{slide_id}`
* Polygon annotation persistence (sibling table or extended
  `annotations.geometry jsonb`)
* Multi-slide side-by-side (compare different stains of the same
  block)
* MCP tools: `extract_slide_roi`, `write_pathology_annotation`

At this point a real lab workflow is technically feasible.

### Step 3 (production-grade) — 2-3 months, possibly more

Goal: clinical use, MDR/CE coverage path.

Tasks:
* SVS/NDPI → DICOM-WSI transcoding worker (move from DZI to
  DICOMweb path; tile API uniforma to radiology)
* Multi-stain compositing (IHC overlay over H&E)
* LIS integration hooks (HL7 / FHIR ImagingStudy + ObservationGroup
  for sign-out)
* Pixel-level audit (who looked at which ROI, at what zoom, when)
* AI overlay layer (cell-density / segmentation predictions; ARM64
  TotalSeg path lacks pathology models — separate research thread)

This is real product work. Several specialist hires worth.

## 11. Recommended next action

If the goal is "shipping pathology this quarter", do **Step 1**.
It is bounded (1 week), lands a new clinical artefact in the
fascicolo with full audit + tag + share, and uses zero new
infrastructure (S3 + Postgres only, the same ones already provisioned
for radiology). Step 2 then becomes a frontend-heavy follow-up that
does not block other roadmap items.

If the goal is "evaluate before committing", run a 2-day spike on
Step 1 architecture against one real lab feed (a single CMU-1 SVS) +
write up the result — happy to do this on request.

## 12. Open questions for the product side

* Are pathology slides expected to be **first-class clinical entities**
  (like a CT study, with its own report, sign-out, billing) or
  **attachments** to an existing radiology workflow (e.g. an
  oncologist's case folder)? The data model is the same either way,
  but the UX is very different.
* Which **stains / IHC markers** are in scope at v1? Single H&E view
  is one card; multi-stain switcher is a viewer-level feature.
* Is there a **target lab partnership** that constrains the
  formats / scanners we need to support first? "Generic OpenSlide" is
  a safe default, but if a specific scanner is the partner that
  shapes priorities.

## 13. Step 1 — landed (2026-05-20, v3.7.8)

Step 1 shipped as scoped in §10:

- Migration `0005_pathology_slides.py` (post-rebase numbering)
  materialises the `pathology_slides` table with the columns proposed
  in §8 plus the CHECK `ck_pathology_slides_label_redacted_when_present`
  added by the 3.7.9 security pass (H7): `s3_label_key IS NULL OR
  label_redacted = TRUE`. Per memory `pathology_step1_live`, the PHI
  label policy is **never** persisted in cleartext.
- `bvphoenix-import-pathology` CLI mirrors `bvphoenix-import` and
  uses `openslide-bin` for cross-platform OpenSlide.
- `wsi_deid.py` strips the embedded label by default.
- The patient-page card renders thumbnail + macro + stain +
  magnification, and the file-size cap `BVP_WSI_MAX_BYTES`
  (default 30 GiB, 3.7.9 H10) blocks oversized uploads before
  OpenSlide memory-maps them.

Steps 2 and 3 remain open. Smoke-test against CMU-1 / CMU-2 (CC-BY)
is the gating acceptance for Step 2.

## References

* [OpenSlide formats](https://openslide.org/formats/)
* [DICOM Supplement 145 — WSI](https://dicom.nema.org/medical/dicom/current/output/html/part17.html#chapter_GGG)
* [IIIF Image API 3.0](https://iiif.io/api/image/3.0/)
* [OHIF WSI extension](https://github.com/OHIF/Viewers/tree/master/extensions/extension-dicom-microscopy)
* [OpenSeadragon](https://openseadragon.github.io/)
* [TCGA pathology imaging](https://portal.gdc.cancer.gov) — CC-licensed slides if we want a public-dataset analog of the radiology library we just shipped.
