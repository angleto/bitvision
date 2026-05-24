# Segmentations, Fiducials, and STL Export

This document covers the viewer features introduced in Unit V6:

1. **External segmentation import** — uploading NIfTI / NRRD masks
   produced by 3D Slicer, ITK-SNAP, MONAI Label, nnU-Net, or any
   standards-compliant tool.
2. **3D fiducial landmarks** — labelled points in volume space used for
   surgical planning, cross-series registration cues, and anatomical
   callouts.
3. **Isosurface STL export** — marching cubes on the loaded volume
   followed by ASCII STL download, targeting 3D printing and surgical
   planning workflows.

The segmentation threshold controls and volume rendering described in
earlier units continue to work unchanged; this document only covers
what's new.

---

## 1. Segmentation import

### 1.1 Supported formats

| Format          | Extension           | Notes                                   |
|-----------------|---------------------|-----------------------------------------|
| NIfTI           | `.nii`              | Single-file, uncompressed.              |
| NIfTI gzipped   | `.nii.gz`           | Auto-detected from the gzip magic.      |
| NRRD (detached) | `.nrrd`             | Header + raw or gzipped payload.        |

NRRD variants with separate header + data files (`.nhdr` + `.raw`) are
not supported — export as a single `.nrrd` from the source tool first.
Multi-segment Slicer files (`*.seg.nrrd`) import as a **binary** mask
with all non-zero labels collapsed to foreground — upload each label
separately if you need per-label masks.

### 1.2 Dimension handling

The import pipeline validates that the uploaded volume can be mapped
onto the target series. Two paths exist:

1. **Exact match** — uploaded shape equals the series volume shape.
   The mask is binarised and stored as-is.
2. **Nearest-neighbour resample** — uploaded shape differs. The
   importer resamples the label volume to the target shape without
   introducing partial labels (integer-nearest, never interpolated).

Nearest-neighbour is the only correct choice for discrete labels; any
other filter would fabricate intermediate indices. Spacing / orientation
metadata is not used: we rely on the source tool having segmented the
same voxel grid the viewer loads from the DICOM series. This trades
correctness on reoriented / reformatted datasets for simplicity, which
is acceptable for the V6 scope — a future version will consult
`qform` / `sform` matrices.

### 1.3 Storage layout

Masks live in the derivatives bucket under
`segmentations/{series_id}/{label}.bin`. Each file is a raw `uint8`
buffer, x-fastest (same axis order as `volume.raw`), length
`nx * ny * nz`. Values are `0` (background) or `1` (foreground). No
header: the viewer obtains dimensions from the sibling volume.

`label` must match `[a-zA-Z0-9._-]{1,64}` — enforced at the API layer
so listings are URL- and filesystem-safe.

### 1.4 API surface

| Method | Path                                                         | Permission                    |
|--------|--------------------------------------------------------------|-------------------------------|
| GET    | `/api/series/{id}/segmentations`                             | `read:pixels`                 |
| GET    | `/api/series/{id}/segmentations/{label}`                     | `read:pixels`                 |
| POST   | `/api/series/{id}/segmentations` (multipart: `label`, `file`)| `write:annotations`           |
| DELETE | `/api/series/{id}/segmentations/{label}`                     | `write:annotations`           |

Upload cap: 200 MiB. Typical brain-MR segmentation is under 5 MiB
binarised, so this leaves headroom for dense CT exports.

### 1.5 UI flow

The sidebar's `SegmentationImporter` panel (visible when **3D** is
active) offers:

- A drag-and-drop zone that also accepts a click to open the file
  picker.
- A label input prefilled from the filename (sanitised to match the
  server-side pattern).
- A list of already-imported segmentations for the current series,
  each with **Apply** (load into the volume viewer), **Delete**, and
  color-coded swatches. Apply hands the raw bytes to the existing
  `setSegmentationMask` path on the `VolumeViewer` ref, so the imported
  mask renders identically to a threshold-based mask.

---

## 2. Fiducial landmarks

Fiducials are labelled points attached to the currently-loaded series.
They live entirely in client-side state during V6 — persistence is
deferred to the viewport-state work tracked under Unit V2.

### 2.1 Data model

Each fiducial carries:

- `id`: opaque string, stable within the session.
- `label`: free-text, editable inline.
- `position`: `[x, y, z]` voxel coordinates (integers).
- `color`: RGB triple auto-assigned from a rotating palette.

Exporting produces JSON with both `voxel` and `mm` forms (`mm = voxel *
spacing`), suitable for downstream consumption (image-guided surgery
tools typically take mm).

### 2.2 Interactions

- **Add at crosshair**: places a fiducial at the current MPR crosshair,
  optionally with a label (defaults to `F1`, `F2`, …).
- **Rename**: click into the label field.
- **Jump to**: moves the MPR crosshair to that fiducial's voxel.
- **Delete / Clear all**: obvious.
- **Export JSON**: downloads `fiducials.json` for the current session.

### 2.3 3D rendering

`FiducialMarkers3D.tsx` exports `<FiducialGlyphs />`, which mounts
sphere actors into a supplied vtk render window. It's decoupled from
`VolumeViewer` — the volume viewer exposes its `vtkGenericRenderWindow`
indirectly, and the glyph component rebuilds its actors whenever the
fiducial list changes. The panel UI is rendered even when 3D is off,
so fiducials can be collected on MPR slices before enabling 3D.

The default glyph radius is three voxels of the smallest spacing axis —
enough to see on a typical CT without obscuring structure.

---

## 3. Isosurface STL export

### 3.1 Why client-side

`extractAndDownloadStl` runs entirely in the browser. The viewer already
holds the full Float32 volume (downloaded for rendering) and any active
segmentation mask, so a server round-trip would transfer the same data
back and forth for no benefit. Marching cubes at typical CT resolution
(512 × 512 × 300) produces a few hundred thousand triangles in 3–8 s on
a modern laptop — acceptable for an explicit user action.

### 3.2 Implementation choice

`lib/isosurfaceSTL.ts` first attempts to load
`@kitware/vtk.js/Filters/General/ImageMarchingCubes`. If that module is
resolvable in the runtime bundle, the vtk.js filter runs and its output
polydata is flattened into a raw triangle array. Otherwise, the file
ships a minimal JS port of Paul Bourke's classic marching cubes tables
(the same tables VTK and ITK use) — it's 300 lines of tables plus a
straightforward voxel loop.

The fallback is not smoothed or decimated. For cosmetic 3D printing,
run the exported STL through MeshLab's quadric edge collapse filter.

### 3.3 STL format

We emit **ASCII STL**. Binary STL is smaller, but the intended audience
(3D printing services, surgical planning software, academic CAD tools)
universally accepts ASCII and often ships better diagnostics when an
ASCII file fails to load. Coordinates are in millimetres — voxel
positions are multiplied by `spacing` before being written.

Flat per-facet normals are computed from the triangle cross products.
This is good enough for the target workflows; smooth vertex normals
would require per-vertex deduplication, which is out of scope for V6.

### 3.4 UI flow

In the 3D controls sidebar:

- **Iso value** slider spans `[volume.range[0], volume.range[1]]` and
  defaults to the midpoint.
- **Export isosurface as STL** runs `extractAndDownloadStl` and reports
  triangle count and file size when done.
- The generated filename embeds the series id and iso value so users
  can re-run at different thresholds without overwriting.

---

## 4. Permissions and audit

Segmentation upload / delete requires `write:annotations`. Listing and
fetching the raw mask bytes require `read:pixels` (same as the source
DICOM pixels, since a segmentation leaks the same information). STL
export happens client-side with volume data the user already has, so
it requires no additional grants — anyone who can see the pixels can
produce a mesh from them.

Fiducials are session-only, so they generate no audit log entries
today. When they become persistent (Unit V2 follow-up), the same
`write:annotations` / `read:annotations` gates should apply.

---

## 5. Known limitations (V6 scope bound)

1. **Orientation**: segmentation import does not read NIfTI `qform` /
   `sform` or NRRD `space directions`. Masks segmented on a reoriented
   copy of the series will land wrong. Use the as-ingested volume as
   the segmentation target.
2. **Fiducial persistence**: gone on page reload. JSON export is the
   workaround.
3. **Marching cubes quality**: no smoothing, no decimation. Expected
   for V6; the downstream CAD tool is the right place to handle this.
4. **Multi-label segmentations**: collapsed to a single binary mask on
   upload. Split labels server-side in your source tool if you need
   them distinct.
