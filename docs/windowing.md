# Viewer windowing (W/L)

Short reference for how the bitvision_phoenix viewer decides on a
Window Center / Window Width.

## Three-tier fallback

Given a series, the viewer picks a W/L using this order:

1. **DICOM tags** (`suggestedFromDicom`). The backend reads
   `WindowCenter (0028,1050)` and `WindowWidth (0028,1051)` off the
   middle instance and returns them on `GET /api/series/{id}` as
   `suggested_wc` / `suggested_ww`. When present, these reflect the
   radiologist's own window from the acquisition site — the best
   default we have.
2. **Modality presets** (`modalityDefaults`). A set of clinically
   standard presets, filtered by the series' `modality` and bubbled
   by `body_part_examined`. Example: CT chest → CT Lung first, CT
   Mediastinum second.

   Current coverage:
   - **CT generic**: Abdomen, Lung, Bone, Brain, Soft Tissue,
     Mediastinum, Angio.
   - **CT liver phase variants**: Liver, Liver non-contrast,
     Liver arterial, Liver portal venous, Liver delayed, Liver
     narrow (HCC).
   - **CT renal phase variants**: Kidney, Kidney corticomedullary,
     Kidney nephrographic, Kidney excretory, Kidney stone.
   - **CT lung variants**: Lung HRCT, Lung emphysema, Lung airways.
   - **CT other organs**: Pancreas, Adrenal, Spleen.
   - **MR**: T1, T2, FLAIR, DWI.
   - **PT**: SUV body, SUV hot lesions, SUV brain, SUV myocardium.

   Body-part-aware bubble priority recognises tokens for
   `liver` / `hepat`, `kidney` / `renal`, `pancreas`, `adrenal`,
   `spleen`, `lung` / `chest` / `thorax`, `abdomen` / `pelvis`,
   `head` / `brain` / `skull`, `bone` / `spine` / `extremity`,
   `angio` / `vessel` / `aorta`. The matching presets get sorted
   to the front of the list so the radiologist's first click is on
   the right window for the body part being read.
3. **Histogram auto** (`computeAutoWL`). Percentile clip at 1% and
   99% of the voxel CDF, using a 1024-bin histogram. Robust to the
   handful of outlier voxels (metal, air, noise) that otherwise blow
   out the dynamic range. Two passes over the Float32Array.

## API additions

```
GET /api/series/{id}
{
  ...,
  "suggested_wc": 40.0 | null,
  "suggested_ww": 400.0 | null
}
```

Enrichment reads the middle instance's DICOM header via
`pydicom.dcmread(..., stop_before_pixels=True)` so no pixel data is
decoded. Failure (no instances, S3 down, garbage tags) is non-fatal —
the fields are simply left `null` and the frontend falls back to the
next tier. List endpoints (`/studies`, `/studies/{id}`) still skip
the enrichment to keep them cheap.

## Frontend module

All three helpers live in `frontend/src/lib/windowing.ts`:

```ts
computeAutoWL(volume: Float32Array): { wc, ww }
modalityDefaults(modality: string, bodyPart?: string): Array<{label, wc, ww}>
suggestedFromDicom(series): { wc, ww } | null
```

The viewer's sidebar renders, in order: an **Auto** button, an
optional **DICOM** button (when `suggestedFromDicom` returns a value),
the per-modality presets, and a **Reset** button that drops back to
the VTK-default window.

## Notes

- The backend's existing thumbnail path already reads WC/WW — we
  factored it into `thumbnails.read_dicom_wc_ww(ds)` so both paths
  share the same multi-value handling.
- `modalityDefaults("")` (unknown modality) returns every preset so
  the user can still pick manually.
- `computeAutoWL` does not allocate a copy of the volume — it runs
  three O(N) / O(bins) passes over the existing Float32Array.
