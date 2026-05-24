# 3D rendering controls

The 3D volume renderer (`frontend/src/components/VolumeViewer.tsx`)
is driven by a single set of presets plus three layers of runtime
override. Together they give the radiologist full control over the
rendered image without leaving the viewer.

The control surface is exposed in the right sidebar of
`/viewer/series/[id]` whenever the 3D pane is active, and lives in
`frontend/src/app/viewer/series/[id]/page.tsx` (the `render3DControls`
function).

## Layered model

A 3D rendering is the product of:

1. **Modality** of the source series (CT / MR / PT). Drives the
   default preset and the resolution mode for `displayRange`.
2. **Preset** — a `(color stops, opacity stops, range mode, range
   hint)` quadruple identified by a `ColorPreset` string.
3. **Override props** sent into `VolumeViewer`:
   - `customRange` — `[min, max]` HU/scalar window. Replaces the
     preset's resolved `displayRange`.
   - `customColorStops` — array of `{pos, [r, g, b]}`. Replaces the
     preset's color ramp.
   - `customOpacityStops` — array of `{t, alpha}`. Replaces the
     preset's opacity ramp.
   - `cropBox` — axis-aligned `{lower, upper}` in normalised volume
     space. Mounts up to six `vtkPlane` clipping planes on the
     mapper.
4. **Live knobs** the user adjusts on top:
   - `threshold` — slides every opacity stop's t-position along the
     histogram (positive = grow the transparent region).
   - `opacityShift` — scales every alpha (range −50…+50, mapped to
     `1 + shift / 50`; midpoint preserves the preset).
   - `sampleDistance` — ray-march step in world units.
   - `shade` / `cinematic` — Phong vs gradient-opacity-augmented
     shading.

The render loop applies these in this exact order, so e.g. a custom
range narrows the histogram first, then the custom CTF (if any) gets
remapped onto the new `[lo, hi]`, and finally `threshold` /
`opacityShift` operate on the resolved curve.

## Built-in presets

Defined in `VolumeViewer.tsx` (`PRESET_DEFS`). Each preset declares a
`rangeMode` (`hu` / `percentile` / `suv` / `full`), an optional
`rangeHint` for HU-based presets, plus paired RGB and opacity stops.

### Generic CT body windows

| Preset             | HU range     | Notes                                        |
|--------------------|--------------|----------------------------------------------|
| `ct-bone`          | 200 → 1500   | Cortical bone surface render                 |
| `ct-soft-tissue`   | -100 → 300   | Skin / fat / soft-tissue shell               |
| `ct-lung`          | -1000 → -300 | Air-space + parenchyma                       |
| `ct-angio`         | 100 → 600    | Iodine-filled vasculature                    |

### Organ-tuned CT presets

Each clamps the HU window to the target organ and pairs it with a
tissue-coloured RGB ramp. They land best on contrast-enhanced studies
but degrade gracefully on non-contrast scans.

| Preset               | HU range    | Use case                                    |
|----------------------|-------------|---------------------------------------------|
| `ct-liver`           | 30 → 200    | Portal-venous parenchyma + hepatic vessels  |
| `ct-liver-vessels`   | 60 → 300    | Vessel-emphasising; HCC staging             |
| `ct-kidney`          | 20 → 250    | Cortex / medulla; CMP through excretory     |
| `ct-pancreas`        | 20 → 150    | Pancreas + peripancreatic fat planes        |
| `ct-spleen`          | 30 → 200    | Splenic parenchyma in portal phase          |
| `ct-cardiac`         | 30 → 400    | Myocardium + chambers + great vessels       |

### MR / PT / artistic

`mri-t1`, `mri-t2`, `mri-edge`, `pet-suv-hot`, `pet-suv-rainbow`,
plus generic ramps (`bone`, `grayscale`, `cool-warm`, `hot`, `ice`,
`spectral`, `jet`, `viridis`, `magma`, `skin`, `tissue`, `muscle`).

PET presets ship a hard alpha cutoff at the 65% mark of the
`displayRange` to prevent the "patient silhouette cylinder" that
otherwise emerges from compositing low-alpha PET background voxels.

## Quick-highlight macros

A button strip at the top of the 3D control panel applies preset +
HU range + override resets in one click:

| Macro            | Preset            | HU range     |
|------------------|-------------------|--------------|
| Fegato           | `ct-liver`        | 30 → 200     |
| Vasi epatici     | `ct-liver-vessels`| 60 → 300     |
| Reni             | `ct-kidney`       | 20 → 250     |
| Pancreas         | `ct-pancreas`     | 20 → 150     |
| Milza            | `ct-spleen`       | 30 → 200     |
| Cuore            | `ct-cardiac`      | 30 → 400     |
| Polmone          | `ct-lung`         | -1000 → -300 |
| Vasi (CTA)       | `ct-angio`        | 100 → 600    |
| Osso             | `ct-bone`         | 200 → 1500   |
| Tessuti molli    | `ct-soft-tissue`  | -100 → 300   |

Each macro also clears any custom CTF, custom opacity, threshold and
crop box so the previous session doesn't bleed into the new pick.
The active state is computed from the current preset + range
override pair, so a button highlights when its combination matches.

## HU / scalar range slider

The `Vr3DRangeControl` widget exposes four parametrically-linked
knobs over the same `[lo, hi]` tuple:

- **Min** / **Max** — direct endpoints in raw HU (or SUV / Bq·mL on
  PT, depending on the preset's `rangeMode`).
- **Window (W)** — `max − min`. Drag to widen / narrow the visible
  range, centred on the current Level.
- **Level (L)** — `(max + min) / 2`. Drag to shift the entire window
  up or down the histogram, preserving width.

All four are views on the same state — moving any one updates the
others. `null` (the default) means "follow the preset's resolved
range"; the **Reset to preset** button restores it.

The slider step adapts to the volume's full range: 1 HU resolution
on CT, finer on PT/SUV scans.

## Color transfer function editor (`Vr3DColorEditor`)

A list of stops, each with:

- a **position** slider in `[0, 1]` mapping onto the current
  `displayRange` at render time,
- a native **color picker** (HTML `<input type="color">`).

Buttons:

- **Edit** — initialises the editor from the preset's CTF and takes
  ownership.
- **+ stop** — inserts a midpoint stop between the last two,
  pre-coloured to the average of its neighbours.
- **× per stop** — removes (when more than two stops remain).
- **Reset** — drops back to the preset's RGB ramp.

Stops are sorted client-side before being sent to the renderer:
out-of-order positions would otherwise be silently dropped by vtk.js
and the volume would render black.

## Crop box (`Vr3DCropBox`)

Six sliders driving an axis-aligned crop box in normalised volume
space (`[0, 1]³`). VolumeViewer translates the box into six
`vtkPlane` clipping planes mounted on the volume mapper — the cap of
six matches vtk.js's hardware limit exactly, so we cover all faces
of an axis-aligned box without compositing tricks.

Buttons:

- **Crop** (when off) — enable cropping with a full-volume box; the
  user then narrows it via the sliders.
- **Open** — re-expand to the full volume but keep cropping enabled.
- **Off** — disable cropping entirely (no clipping planes, single
  pass through the full volume).

Each axis pair enforces a 0.01 minimum gap so the user can't
accidentally dial in an empty box (which would render pure black).

## Opacity transfer function editor

Predates the layered model. Lives in
`frontend/src/components/TransferFunctionEditor.tsx` and edits
`customOpacityStops` directly. Drag the handles in the histogram-
shaped editor to redraw the opacity curve; **Reset** drops the
override and restores the preset.

## Blend modes (composite / MIP / MinIP / Average)

Set via the toolbar buttons in the same panel. The three non-
composite modes ignore shading and gradient opacity by definition;
the renderer force-disables both when leaving composite mode and
restores them on the way back, plus calls `modified()` on the mapper
+ volume property to invalidate vtk.js's shader cache (without it
the visible image stays composite even after `setBlendMode` has
taken).

## Camera, lighting, background

- **Reset camera** — re-fit the volume to the viewport.
- **Camera presets** — anterior, posterior, left, right, superior,
  inferior. Drive `setCameraPreset()`.
- **Background** — color picker; calls `setBackground(r, g, b)`.

The light setup itself (ambient + diffuse + specular + headlight +
fill) is fixed per `applyShadeParams`. Exposing the light parameters
to the user is intentionally out of scope: the current values match
the cinematic-render aesthetic radiologists expect, and three more
sliders add complexity without changing what they read off the
image.

## Plumbing summary

```
viewer page state            VolumeViewer prop          vtk.js call
─────────────────────────────────────────────────────────────────
vrPreset                  →  colorPreset             →  applyPreset (CTF + OTF)
vrRangeOverride           →  customRange             →  displayRange override
vrCustomColors            →  customColorStops        →  applyPreset (CTF override)
vrCustomOpacity           →  customOpacityStops      →  applyPreset (OTF override)
vrCropBox                 →  cropBox                 →  mapper.addClippingPlane × N
vrThreshold               →  setThreshold            →  applyPreset (t-shift)
vrOpacity                 →  setOpacityShift         →  applyPreset (alpha scale)
vrSample                  →  setSampleDistance       →  mapper.setSampleDistance
vrShade / vrCinematic     →  setShade / setCinematic →  applyShadeParams
vrBlend                   →  setBlendMode            →  mapper.setBlendMode + modified()
```

All overrides pass through `MPRLayoutProps` first
(`MPRLayoutTypes.ts`); the layout forwards them to the inner
`VolumeViewer` for the 3D pane.
