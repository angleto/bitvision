# Side-by-side comparison view

The comparison view lets a radiologist open multiple series at once and
compare a new study against a prior. It is intentionally minimal: each pane
is an independent `MPRViewport`, and any cross-pane coupling (slice scroll,
window/level) is opt-in.

## Entry point

Route: `/viewer/compare`

Query parameters (all optional, all repeatable):

- `left=<seriesId>` — preload a series into a pane
- `right=<seriesId>` — same semantics; `left` and `right` are just
  conventions for the two-pane case
- `s=<seriesId>` — generic slot, useful when you want more than two panes
  via URL

At most **4 panes** are rendered. Extra ids beyond the limit are ignored.
Duplicate ids are deduplicated while preserving first-seen order.

Example URLs:

```
/viewer/compare?left=<newStudySeriesId>&right=<priorStudySeriesId>
/viewer/compare?s=<a>&s=<b>&s=<c>&s=<d>
```

## Layout

- 1 pane → single full-size viewport
- 2 panes → 2×1 grid (side-by-side)
- 3 or 4 panes → 2×2 grid

Each pane displays:

- The series description / modality as overlay label
- An independent axial/sagittal/coronal slice (axis is selected globally
  for the whole view via the sidebar)
- A remove (×) button in the top-right

## Sync options (opt-in, default OFF)

Two checkboxes in the sidebar:

- **Sync slice scroll** — when you scroll one pane by N slices, every
  other pane moves by the same delta along the current axis (clamped to
  each pane's own dimension limits). Useful when the two volumes have a
  similar Z range.
- **Sync window / level** — any W/L adjustment on one pane is mirrored
  onto every other pane. Useful when you want consistent contrast across
  a new study and its prior.

Keeping both off gives you fully independent panes, which is the safest
default for priors captured with different protocols.

## Adding panes

The sidebar has an **+ Add series** button that opens `SeriesPicker`, a
modal that lists your recent studies (`studiesApi.list({ limit: 30 })`)
and lets you drill down into each study's series. Already-added series
are disabled in the picker to prevent duplicates.

## Implementation notes

- `MPRViewport` is reused as-is; this view never forks or modifies it
- `ComparisonLayout` owns per-pane state (volume, crosshair, W/L) and
  implements the sync logic by translating a single pane's crosshair
  change into a synchronized delta applied to the other panes
- Volumes are streamed from `/api/series/<id>/volume.raw` via the same
  binary format used by the single-series viewer
- There is no shared 3D viewport; comparison is 2D/MPR only

## Files

- `frontend/src/app/viewer/compare/page.tsx` — route and sidebar
- `frontend/src/components/ComparisonLayout.tsx` — grid of N viewports
- `frontend/src/components/SeriesPicker.tsx` — modal series picker
