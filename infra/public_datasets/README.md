# Public datasets — OpenData manifests

Two CLIs bootstrap the OpenData library from curated public archives:

- `bvphoenix-public-import` → radiology DICOM, from `manifest.yaml`.
- `bvphoenix-public-import-pathology` → pathology whole-slide images (WSI),
  from `manifest-pathology.yaml`.

Both wire everything to the platform-owner subject (read-only OpenData for
every authenticated user, plus anonymous visitors via `is_public`), record
license/citation provenance for the viewer badge, and are idempotent.

## License policy (the controlling gate)

The platform redistributes these images to anonymous web visitors and has
commercial intent, so only redistribution-permissive licenses qualify:

| SPDX | Eligible? | Treatment |
|---|---|---|
| `CC-BY-3.0`, `CC-BY-4.0`, `CC0-1.0` | yes | commercial reuse OK |
| `CC-BY-NC-*` | yes, **labelled** | ingested but flagged non-commercial / educational use only; `commercial_use_allowed` is derived `false` from the SPDX, the badge says so, and a future commercial tier must exclude them |
| `*-ND`, `CC-BY-(NC-)SA` (viral copyleft) | no | excluded |
| NIH / TCIA Controlled Access, Kaggle embargo | no | excluded (not redistributable) |

`commercial_use_allowed` is derived centrally in
`services/licensing.py::license_allows_commercial_use` (`-NC` not in SPDX)
and surfaced on `StudyOut` / `PathologySlideOut`.

Notes:
- Brain/head collections are almost all Controlled since TCIA's 2022
  face-reconstruction policy. `UPENN-GBM` (defaced) and `ReMIND` are the
  CC-BY exceptions.
- `MIDRC-RICORD-1C` (already loaded) is verified `CC-BY-NC-4.0`, retained
  under the non-commercial label.
- **GDC pathology exclusion**: TCGA diagnostic SVS via the NCI GDC are
  open-access but carry no redistribution grant; the `gdc` adapter refuses
  to run. Move an individual slide to the `http` adapter only with a
  licensed per-slide URL (needs written NCI sign-off).

License covers copyright, not privacy: curated TCIA/CAMELYON/OpenSlide sets
are de-identified upstream (same trust model as the importers). The
pathology importer never writes the slide label and bottom-crops the macro.

## Radiology manifest (`manifest.yaml`)

| Adapter | What it does |
|---|---|
| `tcia` | TCIA NBIA v1 REST (`services.cancerimagingarchive.net`). One ZIP per series. |
| `osirix_zip` | HTTP ZIP from a manifest-supplied URL, one ZIP per subject. |

```yaml
sources:
  - collection: TCIA/LIDC-IDRI       # "/" separates upstream namespace
    adapter: tcia
    license_spdx: CC-BY-3.0
    license_url: https://creativecommons.org/licenses/by/3.0/
    citation_required: true
    citation_text: |
      Armato SG 3rd, McLennan G, et al. ... Med Phys. 2011;38(2):915-31.
    subjects: all                    # whole-collection: enumerate every
                                     # PatientID via NBIA getPatient.
                                     # (or an explicit list of PatientIDs)

  - collection: TCIA/CMB-LCA
    adapter: tcia
    license_spdx: CC-BY-4.0
    license_url: https://creativecommons.org/licenses/by/4.0/
    citation_required: true
    citation_text: "..."
    exclude_body_parts: [HEAD, BRAIN, SKULL, FACE]   # drop NIH-Controlled
                                                     # head series before download
    subjects: all
```

- `subjects: all` is the maximal-wave form. The K8s Job defaults to PILOT
  (a 3-subject smoke set via `--only`); a bulk run clears PILOT.
- `exclude_body_parts` filters series by `BodyPartExamined` (upper-cased)
  before download — used for the otherwise-CC-BY CMB collections.
- As of 2026-06-28 the active set is 48 collections (license SPDX +
  canonical TCIA citation verified against the live collection page + DOI
  for each), projecting to ~32k public studies (~29.7k commercial-eligible).
  A `GATED` block at the bottom of `manifest.yaml` holds collections that
  are verified-eligible but intentionally **not** active — the giants
  (`NLST` ~73k, `Yale-Brain-Mets-Longitudinal` ~11.9k), the synthetic
  `VICTRE` phantom set, the large NC `Breast-Cancer-Screening-DBT`, and the
  `Pseudo-PHI-DICOM-Data` teaching set (designed to contain pseudo-PHI). To
  activate one, uncomment its block and move it up into `sources:`.
- NBIA collection names are case-sensitive and passed verbatim. Two carry a
  SPACE, not a hyphen: `NSCLC Radiogenomics` and `RIDER Lung CT`. Do not
  fabricate a DOI for a medical dataset.

## Pathology manifest (`manifest-pathology.yaml`)

| Adapter | What it does |
|---|---|
| `http` | Direct per-slide URL. OpenSlide CC0 test data; any TCIA SVS whose licensed HTTPS URL is listed (CPTAC / Post-NAT-BRCA). |
| `aws_open_data` | Anonymous (unsigned) S3 listing + download of a public AWS Open Data bucket. CAMELYON16/17 (CC0). Bucket / region / prefixes come from the manifest. |
| `gdc` | DEFERRED — refuses to run (see GDC exclusion above). |

```yaml
sources:
  - collection: OpenSlide/test-data
    adapter: http
    license_spdx: CC0-1.0
    license_url: https://creativecommons.org/publicdomain/zero/1.0/
    citation_required: false
    citation_text: "OpenSlide freely-distributable test data, CC0 1.0."
    stain: "H&E"                     # source-level default
    slides:
      - subject_id: CMU-1
        url: https://openslide.cs.cmu.edu/.../Aperio/CMU-1.svs
        sha256: <optional integrity check>
        display_name: "Public Demo · Histology (CMU-1)"

  - collection: CAMELYON/CAMELYON16
    adapter: aws_open_data
    license_spdx: CC0-1.0
    license_url: https://creativecommons.org/publicdomain/zero/1.0/
    citation_required: true
    citation_text: "Ehteshami Bejnordi B, et al. JAMA. 2017;318(22):2199-2210."
    stain: "H&E"
    s3_bucket: camelyon-dataset       # verify against the AWS Open Data registry
    s3_region: eu-west-1
    s3_prefixes:
      - CAMELYON16/images/
```

Single-file WSI formats only (`.svs/.ndpi/.tif/.tiff/.scn/.dcm`); `.mrxs`
(multi-file) is rejected over http/aws. A single SVS is 0.5-3 GB, so the
CLI downloads **one slide at a time** and deletes it after import — scratch
never holds more than ~1 slide regardless of collection size.

## Idempotency

Re-running either importer is a no-op for already-ingested data:

- Radiology: partial UNIQUE on
  `(source_collection, source_subject_id, study_instance_uid)` (migration
  `0004`); `completed_series_uids_for_source` skips complete series before
  the per-series ZIP download.
- Pathology: partial UNIQUE on
  `(source_collection, source_subject_id, slide_instance_uid)` (migration
  `0005`) plus `(owner_subject_id, slide_instance_uid)`;
  `completed_slide_keys_for_source` skips complete slides before the
  multi-GB download (keyed on the upstream file id stored in `slide_label`).

## Pilot vs bulk

Radiology:

```sh
bvphoenix-public-import --manifest infra/public_datasets/manifest.yaml \
  --only TCIA/Pancreas-CT/PANCREAS_0001 --dry-run
```

Pathology (one ~170 MB CC0 slide end-to-end):

```sh
bvphoenix-public-import-pathology \
  --manifest infra/public_datasets/manifest-pathology.yaml \
  --only OpenSlide/test-data/CMU-1 --max-slides-per-subject 1 --scratch-dir /scratch
```

Verify in the UI (badge renders, `commercial_use_allowed` correct, pixel
data / WSI tiles load) before clearing PILOT for a full run.

## In-cluster Jobs

Both importers run as one-shot K8s Jobs (the dev box is disk-constrained;
prod S3 is same-region). See
`deploy/bvphoenix-production-k8s-deploy/public-dataset-import-*.yaml` and
`public-pathology-import-*.yaml`. The ConfigMaps embed a copy of these
manifests — keep them in sync with the source-of-truth files here.
