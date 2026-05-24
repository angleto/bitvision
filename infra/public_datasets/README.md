# Public DICOM datasets — OpenData manifest

The `bvphoenix-public-import` CLI bootstraps the OpenData demo library
from curated public DICOM archives. This directory holds the manifests
it consumes.

## Sources currently supported

| Adapter | What it does |
|---|---|
| `tcia` | Pulls from TCIA REST (`services.cancerimagingarchive.net`). Used for TCIA-hosted collections: TCGA-*, LIDC-IDRI, QIN-*, MIDRC-RICORD-1c, COVID-19-AR. Per-series ZIP. |
| `osirix_zip` | HTTP ZIP from a manifest-supplied URL. Used for Pixmeo educational samples (BRAINIX, MANIX, PHENIX, MAGIX) and any other vendor that ships one ZIP per subject. |

Additional adapters (`idc` gs://, `xnat`, ...) can be added in
`backend/src/bvphoenix/cli/public_import.py` without changing the
manifest schema.

## License policy

Only **CC-BY 3.0/4.0** and **TCIA Public Domain** are accepted as
of 2026-05-20. TCIA Restricted-Use collections require a click-through
agreement that cannot be transferred to OpenData visitors and are
therefore excluded — see commit log for rationale. `license_spdx` is
the SPDX identifier (e.g. `CC-BY-3.0`, `CC-BY-4.0`, `TCIA-PD`).

`citation_text` is the canonical attribution string the license
requires. The frontend renders it on the study viewer when
`citation_required=true`.

## Manifest schema

```yaml
sources:
  - collection: TCIA/LIDC-IDRI     # human + machine readable; "/" separates upstream namespace
    adapter: tcia
    license_spdx: CC-BY-3.0
    license_url: https://creativecommons.org/licenses/by/3.0/
    citation_required: true
    citation_text: |
      Armato SG 3rd, McLennan G, et al. The Lung Image Database
      Consortium (LIDC) and Image Database Resource Initiative
      (IDRI): a completed reference database of lung nodules on
      CT scans. Med Phys. 2011 Feb;38(2):915-31.
    subjects:
      - LIDC-IDRI-0001                # short form for tcia
      - LIDC-IDRI-0002
      - LIDC-IDRI-0003

  - collection: OsiriX/BRAINIX
    adapter: osirix_zip
    license_spdx: TCIA-PD
    license_url: https://www.osirix-viewer.com/resources/dicom-image-library/
    citation_required: true
    citation_text: "OsiriX DICOM Image Library, Pixmeo SARL"
    subjects:
      - id: BRAINIX                  # long form: required for osirix_zip
        url: https://www.osirix-viewer.com/.../BRAINIX.zip
        display_name: "Public Demo · Brain MRI (BRAINIX)"
```

## Idempotency

The DB enforces a partial UNIQUE on
`(source_collection, source_subject_id, study_instance_uid)` (migration
`0004_imaging_studies_provenance`). Re-running the importer with the
same manifest is a no-op: existing rows are detected and skipped, only
new subjects/studies/series/instances are added.

## Pilot vs bulk

For a first prod run, restrict to a handful of subjects with `--only`:

```sh
bvphoenix-public-import \
  --manifest infra/public_datasets/manifest.yaml \
  --only TCIA/LIDC-IDRI/LIDC-IDRI-0001 \
  --only OsiriX/BRAINIX/BRAINIX
```

Then verify in the UI (logged in as a regular user) that the studies
appear, the badge renders, and pixel data loads. Only after the pilot
passes should the full manifest run.
