# Measurement tools (2D DICOM viewer)

The 2D viewer in `bitvision_phoenix` ships the set of measurement tools a
radiologist actually reaches for during reading. All tools render on an SVG
overlay on top of the slice canvas and compute physical units (mm, mm²) using
the pixel spacing carried on the series volume metadata.

## Tool inventory

| Tool      | Kind        | Input                                  | Output                                  |
| --------- | ----------- | -------------------------------------- | --------------------------------------- |
| Distance  | `distance`  | 2 clicks                               | line + length in mm                     |
| Angle     | `angle`     | 3 clicks (arm, vertex, arm)            | two segments + angle in degrees         |
| Area      | `area`      | N clicks + double-click to close       | filled polygon + area in mm²            |
| Ellipse   | `ellipse`   | 2 clicks (opposite bounding-box corners)| ellipse + major×minor axis + area      |
| Freehand  | `freehand`  | left-drag; release to finish            | traced path + perimeter in mm           |
| Arrow     | `arrow`     | 2 clicks (tail, head)                   | line with arrowhead + optional text     |
| Text      | `text`      | 1 click (prompts for label)             | text label anchored at the point        |
| Pixel probe | `probe`   | 1 click                                | crosshair + sampled HU/intensity value  |

## Data model

Source of truth: `frontend/src/lib/measurements.ts`.

```ts
type Measurement = {
  id: string;                 // crypto.randomUUID()
  kind: 'distance' | 'angle' | 'area' | 'ellipse'
      | 'freehand' | 'arrow' | 'text' | 'probe';
  points: [number, number][]; // canvas-space (screen px) coordinates
  label?: string;             // pre-computed display string (e.g. "12.4 mm")
  color?: string;             // stroke / fill hint
};
```

Points are stored in canvas (screen) pixel space so the SVG overlay can render
them directly without reprojection. Physical units are computed at creation
time via the `Spacing = { spU, spV }` struct passed by the viewer (derived from
`volume.spacing` projected onto the active axis).

Persistence of measurements (saving to the backend, linking to a report, etc.)
is the responsibility of T2 and is deliberately out of scope here —
measurements live in React state only.

## State machine

Each tool is driven by `appendPoint(kind, pending, next, spacing)`, which
returns either:

- `{ status: "pending", points }` — keep collecting clicks
- `{ status: "complete", measurement }` — commit to the measurement list

Polygon-shaped tools (`area`, `freehand`) are closed by the viewer:

- `area` — double-click
- `freehand` — pointer-up after a drag

Both go through `finalize(kind, pending, spacing)`.

Single-click tools (`text`, `probe`) complete immediately on the first click.
`text` prompts for the label; `probe` asks the viewer for a pixel value via the
`sampleAt(x, y)` callback.

## Rendering

`MeasurementOverlay.tsx` renders every kind as a primitive SVG group:

- **Ellipse** — `<ellipse>` + dashed major/minor axis crosshair; label shows
  `major×minor mm · area mm²`.
- **Freehand** — `<path>` with a `d` built from the sampled points; label is
  the perimeter in mm.
- **Arrow** — `<line>` with an SVG `<marker>` arrowhead; optional text label.
- **Text** — just a `<text>` anchored at the point.
- **Pixel probe** — small `+` cross with the HU/intensity value next to it.
- Distance / angle / area rendering is unchanged from the previous behaviour.

`MPRViewport.tsx` renders the same primitives directly on its 2D canvas so the
tools also work in the MPR viewports (which don't use the SVG overlay
component). Tool dispatch lives in the existing `onMouseDown` / `onMouseMove` /
`onMouseUp` / `onDoubleClick` handlers; no refactoring of the viewport was
necessary.

## Adding a new tool

1. Add a new `MeasurementKind` variant in `measurements.ts`.
2. Add a constructor (`createFoo`) and, if needed, a branch inside
   `appendPoint` / `finalize`.
3. Add a rendering branch in `MeasurementNode` (SVG) and in the canvas loop of
   `MPRViewport.drawContent` (2D canvas).
4. Add a toolbar button in the viewer sidebar (`app/viewer/series/[id]/page.tsx`).

---

## Current state (post-Cornerstone migration)

The MPR viewer now drives drawing through Cornerstone3D's
annotation tools (`LengthTool`, `AngleTool`, `EllipticalROITool`,
`RectangleROITool`, `BidirectionalTool`, `ArrowAnnotateTool`,
`ProbeTool`, `PlanarFreehandROITool`). The legacy SVG overlay path
above still serves the 2D Series viewer and the documented data
model; the sections below describe the additions that landed on
top of the Cornerstone tools.

### Tool selection

`activeTool` (state in `viewer/series/[id]/page.tsx`) defaults to
`null` — "no toolbar selection". In that default mode the
`CrosshairsTool` owns the Primary mouse button (click moves the
focal point, drag rotates the reference lines). Picking a tool
button binds the corresponding Cornerstone tool to Primary; clicking
the same button again deselects, returning to the crosshair-only
default.

W/L (`WindowLevelTool`) and Pan (`PanTool`) are toolbar tools like
the measurement ones — they only acquire Primary when explicitly
selected. Pan also keeps a permanent secondary binding on
Auxiliary (middle-button) so the user can pan while a measurement
tool owns Primary.

The activation effect in `CornerstoneMPRLayout.tsx` rebuilds the
binding map on every change (`setToolDisabled` on Pan / W/L,
`setToolPassive` on Crosshair, `setToolActive` for the chosen
tool). Cornerstone's `setToolActive` merges bindings instead of
replacing them, so the explicit reset is mandatory — without it a
previously-active Pan would keep claiming Primary even after the
user moved on to a measurement.

### Labels on measurements

Every measurement carries an optional `data.label` plus a numeric
value. After a measurement is completed (`ANNOTATION_COMPLETED`
event) the layout pops up an in-app modal asking for an optional
free-text label; a non-empty entry is stored on
`annotation.data.label` and triggers a render.

The per-tool `getTextLines` configuration is overridden on
`addTool` so the label renders as the first line on the canvas,
followed by the auto-computed value (length / angle / area / mean /
HU). Tools without an organic label (`LengthTool`, `AngleTool`,
`EllipticalROITool`, `RectangleROITool`) all use the same overrides;
`ArrowAnnotateTool`'s native label callback is wired to the same
modal so the user only ever sees one dialog style.

### Persistence

Labels round-trip through the marker store via the existing
`marker.body` field. Previously `body` was used only for
`measurement.text` (where it was both the value and the label);
now any kind populates it from `data.label`, and the seeded-load
path restores `data.label` from `body` so the on-canvas overlay
re-renders correctly after a refresh.

The diff effect in `viewer/series/[id]/page.tsx` (around line 632)
sends the label through `markersApi.create` / `update`. The
side-panel inline edit (✎ button) calls `markersApi.update(id, {
body })` and notifies the viewer via `onMarkerLabelChanged`, which
mirrors the new label into the Cornerstone annotation
(`updateAnnotationLabel`) so the canvas refreshes immediately
without forcing a re-render of the whole panel.

### Side panel layout

`MarkerListPanel.tsx` (titled "Annotazioni" / "Annotations") splits
its rows into two sections:

1. **Misurazioni / Measurements** — every `measurement.*` kind,
   grouped by axial slice. Always visible above the Annotations
   heading. Filter buttons don't apply here; the section is the
   measurements view.
2. **Annotazioni / Annotations** — notes, fiducials, text-overlays.
   Filter buttons (`all` / `text` / `fiducials` / `notes`) and the
   note-creation form scope this section only.

Both sections share the same row component (`renderGroups` helper
inside the panel) — icon + scope badge + summary + jump-to + edit
+ delete. Edits and deletes update both the Cornerstone annotation
state and the React state in lockstep so the on-canvas overlay and
the panel never drift.

### Voxel HUD

The bottom-right corner of each MPR viewport shows the voxel value
under the cursor: HU on CT, SUV (when `suvFactorBw` is known) or
Bq/mL on PT, plus the `(i, j, k)` voxel index. Sampling goes
through `voxelManager.getAtIJK()` (the `getScalarData()` method was
removed in Cornerstone3D 4.x); the effect waits for the
`toolGroupReady` signal before subscribing so the volume cache is
guaranteed to be populated.
