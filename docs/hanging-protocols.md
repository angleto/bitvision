# Hanging protocols

A *hanging protocol* is a preset arrangement of viewports — which planes
are shown, in which grid cells, at what size. Radiologists pick one as
soon as they open a study so they do not have to toggle axial /
sagittal / coronal / 3D by hand every time.

bitvision phoenix ships a small, modality-aware catalogue of hanging
protocols and automatically applies the best default when a series
loads.

---

## 1. Concept

A protocol is three things:

1. A **layout** — the grid shape, e.g. `1x1`, `1x2`, `2x2`, `2x3`, `3x3`.
2. A list of **plane assignments** — which views populate the cells in
   row-major order (`axial`, `sagittal`, `coronal`, `3d`).
3. A **modality hint** — so the viewer can pick a sensible default.

All layouts use a fixed CSS grid (`display: grid` + `grid-template-columns:
repeat(cols, 1fr)` + `grid-template-rows: repeat(rows, 1fr)`). Cells
without a plane assignment stay blank, which is fine — comparison
workflows often want empty slots to drop peer studies into.

## 2. Catalogue

Defined in [`frontend/src/lib/hangingProtocols.ts`](../frontend/src/lib/hangingProtocols.ts):

| id                   | label                         | layout | planes                              |
|----------------------|-------------------------------|--------|-------------------------------------|
| `ct-mpr-3d`          | CT · MPR + 3D (2×2)           | 2×2    | axial, sagittal, coronal, 3D        |
| `mr-axial-sagittal`  | MR · Axial + Sagittal (1×2)   | 1×2    | axial, sagittal                     |
| `xr-single`          | XR · Single view (1×1)        | 1×1    | axial                               |
| `axial-only`         | Axial only (1×1)              | 1×1    | axial                               |
| `mpr-triple`         | MPR triple                    | 2×2    | axial, sagittal, coronal            |
| `compare-2x3`        | Compare (2×3)                 | 2×3    | axial, sagittal, coronal, 3D        |
| `grid-3x3`           | Grid 3×3                      | 3×3    | axial, sagittal, coronal, 3D        |

## 3. Modality-aware defaults

`pickDefaultProtocol(modality)` picks the best protocol for an incoming
series:

- `CT`, `PT`, `NM` → `ct-mpr-3d` (2×2 with MPR + 3D).
- `MR` / `MRI` → `mr-axial-sagittal` (1×2).
- `XR`, `CR`, `DX`, `MG`, `RF` → `xr-single` (1×1).
- Anything else → `axial-only`.

The default is applied in a `useEffect` on the viewer page so the user
never sees the pre-protocol state.

## 4. UI

The `HangingProtocolPicker` component is a `<select>` dropdown rendered
in the viewer sidebar above the per-plane Viewports buttons. The user
can either accept the automatic default or switch to any other protocol
at any time — the page state is kept in sync in both directions (picker
updates the visible planes; toggling planes manually does **not** rename
the currently-selected protocol, it just changes which cells are
populated).

## 5. Architecture

```
pickDefaultProtocol(modality)
        │
        ▼
SeriesViewerPage                     ←── HangingProtocolPicker
  │  layout: LayoutId
  │  showAxial / showSagittal /
  │  showCoronal / show3D
  ▼
MPRLayout
  grid-template-columns: repeat(cols, 1fr)
  grid-template-rows: repeat(rows, 1fr)
```

The `layout` prop on `MPRLayout` is optional. When set, the grid uses
the fixed `(cols, rows)` from `LAYOUT_DIMS[layout]`. When omitted, the
grid falls back to the old auto-sizing behaviour based on the number of
visible panels — this keeps all existing callers working.

## 6. Extending the catalogue

Add a new entry to `HANGING_PROTOCOLS` in
`frontend/src/lib/hangingProtocols.ts`. Give it a unique `id`, a
human-readable `label`, one of the allowed `LayoutId` values, and a
list of `planes`. If the protocol should be a modality default, update
`pickDefaultProtocol` accordingly.

There is no server-side persistence yet — protocols are a pure frontend
concern. A future iteration may let users save their own protocols per
account; the store would then live in the `users` or `preferences`
table alongside the existing user settings.
