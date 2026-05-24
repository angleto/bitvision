# Oblique MPR (arbitrary-plane reformat)

bitvision_phoenix supports three orthogonal MPR viewports (axial, sagittal,
coronal). The **oblique** viewport adds a fourth reformat viewport that
samples the volume along an arbitrary plane, defined by a unit normal vector
plus an "up" hint.

## Viewport behaviour

| Action                | Effect                                                     |
| --------------------- | ---------------------------------------------------------- |
| Left-drag             | Rotate plane: horizontal → yaw around `up`; vertical → pitch around in-plane right axis |
| Shift + left-drag     | Window / level (same feel as the orthogonal viewports)     |
| Middle click          | Reset plane normal to `(1, 1, 1)` and up to `+Z`            |
| Wheel                 | Scroll the plane along its normal by 1 mm per notch        |
| Ctrl / Cmd + wheel    | Zoom in / out                                              |

The viewport always resamples the full Float32 volume through
`reformatSlice` (trilinear interpolation). Output size is chosen so the
reformatted square can contain the volume's diagonal at the smallest voxel
spacing (clamped 64–768 px) — this keeps sampling isotropic regardless of the
plane orientation while bounding work per frame.

## Enabling the viewport

The sidebar on the series viewer (`/viewer/series/[id]`) has an "Oblique"
button next to Axial / Sagittal / Coronal / 3D. Toggle it on to get a fourth
panel in the MPR grid. The plane origin starts at the volume centre; wheel-
scroll slabs it along the current normal.

## Code layout

- `frontend/src/lib/obliquePlane.ts` — pure-JS math:
  - `normalize`, `cross`, `dot`, `rotateAround` (Rodrigues' formula)
  - `planeBasis(normal, upHint)` — orthonormal `(u, v, n)` basis
  - `trilinear(...)` — voxel-space trilinear interpolation with bounds check
  - `reformatSlice({...})` — resamples the volume into a `Float32Array`
  - `volumeDiagonalMm` — convenience for picking output size
- `frontend/src/components/ObliqueMPRViewport.tsx` — React viewport that
  owns the normal + origin state, draws a 2D canvas (same approach as
  `MPRViewport`), handles drag-to-rotate, slab scroll, and W/L.
- `frontend/src/components/MPRLayout.tsx` — accepts a new `showOblique` prop
  and adds the oblique panel to the grid.
- `frontend/src/app/viewer/series/[id]/page.tsx` — `Oblique` toggle button
  wired into sidebar controls.

## Notes / future work

- Normal is expressed in **voxel-axis space**. Direction cosines from DICOM
  (image orientation patient) are not applied here — same assumption the
  existing orthogonal viewports make.
- A full gizmo widget (draggable handles rendered over the axial / sagittal /
  coronal panels to show the oblique plane and let you rotate from any
  viewport) is out of scope for this iteration; the in-viewport drag-to-rotate
  covers the MVP.
- The resampler is plain JS. For a 512³ volume and 512×512 output this is
  comfortably interactive in modern V8 (~30 ms). If we ever need it faster,
  moving the inner loop to a Web Worker or WebGL fragment shader is the
  obvious next step.
