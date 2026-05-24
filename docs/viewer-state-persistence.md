# Viewer state persistence

Each (user, series) pair has at most one saved viewport state. When the
user reopens a series they have already looked at, the viewer is
restored to the last state they left it in: viewport layout, active
tool, crosshair position, 3D rendering preset, and sidebar visibility
all come back automatically.

---

## 1. Why per-user state

Radiologists develop personal habits — one may prefer the 3D panel
open with a `ct-lung` preset; another reads axial-only at Windows
`(40, 400)`. A workstation shared between users needs each user to pick
up exactly where they left off, independent of what a colleague did.

State is *not* shared across users: two readers of the same case see
their own restored layout, not the other's. This is distinct from
annotations, which *are* shared (and authored explicitly).

---

## 2. Data model

### viewport_states table

```
viewport_states
  id               uuid pk
  user_subject_id  uuid  ->  subjects(id)  on delete cascade
  series_id        uuid  ->  series(id)    on delete cascade
  state            jsonb not null default '{}'
  updated_at       timestamptz not null default now()

  unique (user_subject_id, series_id)
```

- The `(user_subject_id, series_id)` uniqueness makes upserts trivial
  and guarantees a single row per pair.
- `ON DELETE CASCADE` on both FKs means deleting the user or the
  series cleans up the state automatically — there is nothing of
  independent value here to preserve.
- `state` is `JSONB` because the viewer's shape is free to evolve
  without a migration; the backend never looks inside.

### State shape (current viewer)

The frontend page owns this shape. As of this document:

```jsonc
{
  "showAxial":     true,
  "showSagittal":  false,
  "showCoronal":   false,
  "show3D":        false,
  "sidebarOpen":   true,
  "activeTool":    "wl" | "measure-dist" | "measure-angle"
                 | "measure-area" | "bbox" | "text",
  "mprCrosshair":  [x, y, z],
  "vrPreset":      "bone" | "grayscale" | ... ,
  "vrBlend":       "composite" | "mip" | "minip" | "average",
  "vrOpacity":     -50..50,
  "vrSample":      0.2..3.0,
  "vrShade":       true
}
```

New keys may be added at any time — restore is best-effort: if a key
is missing or has an unexpected type, the current default stays.

---

## 3. API

Both endpoints require auth. A reader without access to the series is
still free to save their own state — the sharing layer governs what
data they can *see*, not what they can remember about their own UI.

### GET `/api/viewport-state/{series_id}`

- `200 ViewportStateOut` — `{ series_id, state, updated_at }`.
- `404` — no state has been saved yet for `(current_user, series_id)`.

### PUT `/api/viewport-state/{series_id}`

Body: `{ state: { … } }` — the full blob. The server overwrites; there
is no partial-update merge.

- `200 ViewportStateOut` — the just-written row.
- `404` — series does not exist.

Response is the same shape as GET so the client can immediately reuse
it for the next round-trip.

---

## 4. Frontend hook

`frontend/src/lib/viewportState.ts` exposes `useViewportState`:

```ts
useViewportState(seriesId, current, { debounceMs: 1000, onRestore });
```

- Fetches once on mount (or when `seriesId` changes). If the server
  returns 404 the hook reports `loaded=true, restored=null` and the
  viewer stays at its defaults.
- Calls `onRestore(state)` once, synchronously, after a successful
  load — the page maps individual fields into its own state setters.
- Debounces PUTs: every change to `current` resets a timer; when the
  timer fires, the full blob is PUT. Default 1000 ms is long enough to
  collapse a drag gesture into a single write but short enough that a
  user who closes the tab a second later still has their state saved.
- Skips saves until the initial fetch completes. Without this guard,
  the page's initial default `current` would clobber real saved state
  before `onRestore` can apply it.

---

## 5. Operational notes

- **Volume cache**: saving viewport state is deliberately *decoupled*
  from volume caching. A cold cache means the volume rebuilds; the
  viewport state still restores instantly as soon as volume metadata
  is available.
- **Privacy**: viewport state never contains DICOM or patient content —
  only UI coordinates and preset names. It is nonetheless scoped to the
  user and cascades on user deletion.
- **Backfill**: none needed. Users who have never saved state simply
  get the viewer's defaults on first visit, as before.

---

## 6. Future work

- Named layouts (e.g. "Lung reading", "Brain reading") selectable from
  a menu — implemented as multiple rows with a `name` column, keyed
  `(user, series, name)`.
- Cross-series defaults: a user-level fallback row applied when no
  series-specific state exists yet. Natural extension, same endpoint
  family, with `series_id` nullable.
