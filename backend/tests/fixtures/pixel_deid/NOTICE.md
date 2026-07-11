# Burned-in-pixel de-identification — evaluation corpus

The pixel-redaction pipeline (`services/pixel_deid`, M4) is validated against
three sources. None of the binary data lives in git or in container images.

## 1. Synthetic (hard gate, always run)
`services.pixel_deid_eval.synthesize_case` generates DICOM frames with PHI text
(including Italian: codice fiscale, names, addresses, dates) drawn into the
pixels at known boxes. Ground truth = the drawn boxes. CI gate:
`test_pixel_deid_redaction.py` asserts **recall == 1.0** (every PHI box masked)
on this set. Deterministic per seed; generated at test time, nothing committed.

## 2. TCIA Pseudo-PHI-DICOM-Data (tracked, marker-gated)
- The Cancer Imaging Archive, "A DICOM dataset for evaluation of medical image
  de-identification (Pseudo-PHI-DICOM-Data)".
- License: **CC BY 4.0** (commercial use permitted, attribution required).
- Citation: Rutherford, M. et al. *Sci Data* 8, 183 (2021).
  https://doi.org/10.1038/s41597-021-00967-y
- https://www.cancerimagingarchive.net/collection/pseudo-phi-dicom-data/
- Synthetic PHI at both header and pixel level, with a ground-truth answer key.

## 3. MIDI-B (tracked, marker-gated)
- NIH/NCI Medical Imaging De-Identification Benchmark validation/test sets (TCIA).
- Per-collection terms must be re-verified at integration time; only
  CC-BY-confirmed slices enter the synced corpus.

## Sync + run
Corpora are pulled to a dataset dir (model-sync pattern: `s3://bvphoenix-datasets/pixel-deid/public/`),
NOT committed. Point `BVP_PIXEL_DEID_CORPUS` at the local dir;
`pixel_deid_eval.load_public_corpus(root)` yields cases (with `answer_key.json`
boxes if present). Recall on the public corpus is **tracked, not gated** — TCIA
pixel labels are noisy and no automated pipeline reaches 100% (MIDI-B 2024-25);
the synthetic set is the hard gate, the human-review quarantine (M1) is the
safety floor.

## 4. Header-engine corpus (same collection, HARD gate, marker-gated)
The Pseudo-PHI collection also plants synthetic PHI in the **headers**, with a
ground-truth answer key — the golden corpus of the PS3.15 header engine
(`services/deid/`). Fetch with `bvphoenix-fetch-deid-header-corpus` (the
answer-key spreadsheet is downloaded manually from the collection page and
converted to `answer_key_header.json`); point `BVP_DEID_HEADER_CORPUS` at the
dir and `test_deid_header_corpus.py` runs a **hard** gate (header ground truth
is exact): no answer-key value or original UID may survive
`deidentify_dicom_bytes`. Same CC BY 4.0 attribution as §2; optional private
sync under `s3://bvphoenix-datasets/deid-header/public/`. This corpus must
never enter the OpenData library.
