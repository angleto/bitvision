# Cinematic rendering & PET/CT fusion

Two upgrades to the 3D volume viewer (`frontend/src/components/VolumeViewer.tsx`)
that together move the renderer closer to what a modern clinical
workstation produces:

1. A **cinematic shading approximation** layered on top of the existing
   Phong volume-rendering path.
2. A **second volume layer** for PET/CT (or CT/MR) **fusion overlay**,
   fed by a new backend endpoint that surfaces sibling series in the
   same study.

Neither change is a full implementation of the corresponding clinical
feature — true cinematic rendering uses Monte-Carlo path tracing, and
proper fusion needs per-voxel registration — but both are meaningful
approximations that run entirely in the browser on the volumes we
already serve.

## Cinematic shading approximation

vtk.js ships a Phong-style volume renderer. It does not ship a path
tracer. We therefore approximate "cinematic" by re-tuning the shading
parameters and enabling gradient-based opacity modulation.

### What the toggle does

The Cinematic button in the 3D sidebar calls
`VolumeViewer.setCinematic(true)`, which flips the volume property to:

| Parameter           | Plain Phong | Cinematic |
|---------------------|-------------|-----------|
| Interpolation       | linear      | linear    |
| `setShade`          | true        | true      |
| `useGradientOpacity`| false       | **true**  |
| gradient min/max    | —           | 0 / 200   |
| `ambient`           | 0.2         | **0.3**   |
| `diffuse`           | 0.7         | 0.6       |
| `specular`          | 0.3         | **0.5**   |
| `specularPower`     | 20          | **10**    |

### Why these numbers

- **Gradient opacity** adds a secondary opacity multiplier that depends on
  the local scalar gradient magnitude. Flat tissue (low gradient) is
  faded out, edges (high gradient) stay opaque. This mimics how
  cinematic renderers resolve crisper surfaces — light gets absorbed at
  the tissue boundary instead of smearing through soft regions.
- **Higher ambient (0.3)** reduces the depth of cavities. Real cinematic
  renderers account for environment lighting / skylight; bumping
  ambient gives a comparable "softly lit from everywhere" look.
- **Stronger specular (0.5) with lower power (10)** produces a broader,
  softer highlight — closer to sub-surface scattering on skin than to
  the tight hotspot of raw Phong.

The toggle forces shading on (it can't contribute if the viewer is in
flat composite mode). Blend modes other than `composite` are untouched —
MIP / MinIP / average ignore the shading parameters by design.

### Limits of the approximation

- No global illumination, no path tracing, no soft shadows.
- Gradient opacity is computed per sample, not per ray, so it doesn't
  accumulate depth information.
- On MIP / MinIP / average blend modes the cinematic parameters have no
  visible effect — only composite uses them.

## PET/CT fusion overlay

### Backend: fusion candidates

```
GET /api/studies/{study_id}/fusion-candidates
    ?exclude_series_id={primary_series_id}
→ list[SeriesOut]
```

Returns sibling series inside the same study that:

- are `ingestion_complete = true` (so `volume.raw` won't 409),
- have an ID different from `exclude_series_id`,
- are ordered so that different-modality siblings come first (PET over
  CT, MR over CT, …), with same-modality siblings at the tail.

The endpoint trusts that series sharing a DICOM study share a frame of
reference — which is the working clinical assumption for PET/CT and MR
multi-sequence acquisitions. Per-voxel registration is explicitly out
of scope.

Permission model: identical to the rest of `api/studies.py` —
`READ_METADATA` on the parent study; 404 (not 403) when forbidden.

### Frontend: FusionControls

`frontend/src/components/FusionControls.tsx` renders in the 3D sidebar.
It calls the candidates endpoint, shows a list of eligible sibling
series, and when one is clicked it fetches its `volume.raw` and hands
a `FusionVolume` to the viewer.

Controls:

- **Series picker** — one button per candidate, active on selection.
- **Blend opacity slider** — 0…1, drives the top of the secondary
  piecewise opacity function.
- **Colormap picker** — `hot`, `pet`, `jet`, `green`, `magenta`, `cyan`.
  The first two are the standard PET-uptake colormaps; the single-hue
  ones are there for CT/MR overlays where PET-style rainbows would be
  inappropriate.
- **Remove overlay** — drops the secondary volume.

### Frontend: VolumeViewer integration

The viewer now holds an optional second `vtkVolume` actor alongside the
primary. The secondary volume:

- uses its own `vtkImageData`, `vtkVolumeMapper`, color + opacity
  transfer functions,
- is rendered with `shade=false`, `ambient=1`, `diffuse=0`,
  `specular=0` — pure emissive color, so PET uptake never gets
  interpreted as anatomy,
- is always in composite blend mode (MIP of a PET overlay on top of a
  composite CT has no clinical meaning),
- shares the primary's origin — we do **not** attempt re-registration.

The secondary opacity function ramps from 0 at the low end of the
secondary's range to the user-chosen blend strength at the top. Low
background uptake stays transparent; hotspots glow through the primary
volume.

### Limits of the fusion MVP

- No re-sampling: if the PET and CT volumes have different dimensions
  or spacings, vtk.js renders each in its own world-space grid. Frame
  of reference coincidence is assumed but not enforced.
- No explicit registration. A reasonable assumption for PET/CT from a
  combo scanner; a weaker one for series acquired on separate machines.
- Fusion is always composite — other blend modes apply only to the
  primary volume.
- Only two volumes at once. Segmentation masks still work alongside
  fusion (they stack as a third actor).

## File map

| File | Change |
|------|--------|
| `backend/src/bvphoenix/api/studies.py` | + `GET /studies/{id}/fusion-candidates` |
| `frontend/src/lib/api.ts` | + `studiesApi.fusionCandidates` |
| `frontend/src/components/VolumeViewer.tsx` | + cinematic toggle, + fusion volume support |
| `frontend/src/components/FusionControls.tsx` | new — sidebar UI for fusion |
| `frontend/src/app/viewer/series/[id]/page.tsx` | wires Cinematic button + FusionControls |
