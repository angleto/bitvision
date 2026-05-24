# DICOM photometric interpretation + pixel spacing

Two small fidelity rules that the viewer needs to respect for images to
look "right" on screen. Both come straight from the DICOM headers.

## PhotometricInterpretation

DICOM encodes which end of the pixel range is "bright" with the
`(0028, 0004) PhotometricInterpretation` tag. The two monochrome values
we care about:

- **`MONOCHROME2`** — the modern default. Low pixel value = black,
  high pixel value = white. Rendered as-is.
- **`MONOCHROME1`** — the "film negative" convention. High pixel value
  = black, low pixel value = white. Without correction, an X-ray
  rendered naively looks inverted (bones dark, soft tissue bright).

### How we correct it

Two places apply the fix:

1. **Thumbnail pipeline** (`backend/src/bvphoenix/services/thumbnails.py`).
   When we generate the JPEG slice for the preview card or the 2D slice
   viewer, we detect `MONOCHROME1` on the raw pydicom dataset and flip
   the pixel array (`arr = arr.max() - arr`) *before* windowing. We
   also flip `WindowCenter` (`max + min - WC`) because the DICOM
   WC/WW values are stored in the original, un-flipped intensity space.
   The output JPEG is therefore always in the `MONOCHROME2` dialect,
   and every downstream consumer (browser, embeddings, LLM) sees a
   conventional "high = bright" image.

2. **MPR viewport** (`frontend/src/components/MPRViewport.tsx`).
   The packed Float32 volume at `/api/series/{id}/volume.raw` does
   *not* rewrite pixels — it is a faithful copy of the DICOM scalar
   data (after rescale slope / intercept). The viewport instead reads
   the `/api/series/{id}/display-metadata` endpoint on mount and, when
   `invert: true`, applies a `ctx.filter = "invert(1)"` to the render.
   The user's manual invert toggle XORs against the auto-invert: if
   the DICOM is already a negative *and* the user asks for a negative,
   we display the original, un-inverted pixels.

## PixelSpacing

`(0028, 0030) PixelSpacing` is a two-element array `[row_spacing,
column_spacing]` in millimetres. For isotropic pixels the two values
are equal and nothing special needs to happen. Some modalities —
especially ultrasound, some mammography, and cine sequences —
store anisotropic pixels where `row_spacing != column_spacing`. Naive
rendering shows a stretched or squashed image.

### How we correct it

The MPR render loop already projects through physical space: it
computes `physW = cols * sx`, `physH = rows * sy`, and fits the image
to the viewport by physical millimetres, not by pixel count. So the
volume path is aspect-correct as long as the volume header carries
`PixelSpacing` correctly (it does — see `pack_series` in
`backend/src/bvphoenix/services/volumes.py`).

For the single-slice 2D thumbnail path, aspect correction is handled
implicitly at the JPEG level: the PIL image keeps the original DICOM
pixel grid, and CSS `object-fit: contain` scales both axes equally.
If the DICOM stores truly anisotropic pixels and exact physical
fidelity is required, the front-end helper
`frontend/src/lib/imageCorrections.ts::correctAspectRatio` returns
a ready-made CSS `transform: scale(x, y)` that the `SliceViewer` can
apply.

## Endpoint

```
GET /api/series/{id}/display-metadata
```

Returns:

```json
{
  "series_id": "…",
  "photometric_interpretation": "MONOCHROME1" | "MONOCHROME2" | …,
  "invert": true,                          // convenience: pi == MONOCHROME1
  "pixel_spacing": [sx_mm, sy_mm],         // column, row
  "rows": 512,
  "columns": 512
}
```

The endpoint reads the first DICOM instance of the series with
`stop_before_pixels=True`, so it is fast (header-only, a few KB of
S3 transfer). Values are assumed to be uniform across the series —
true for every modality we currently ingest.

### Why not bake it into the volume header?

The `/api/series/{id}/volume.raw` blob has a frozen 32-byte header
(documented in `services/volumes.py`) so cached derivatives stay
readable by older clients. A separate endpoint costs one extra HTTP
request on mount but keeps the binary contract stable.
