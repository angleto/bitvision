# Viewer keyboard shortcuts

The DICOM viewer (`/viewer/series/[id]`) supports a keyboard-first workflow
for radiologists. All shortcuts are active whenever the viewer page is
mounted, except while typing in an `<input>`, `<textarea>`, `<select>`, or any
element with `contentEditable=true`.

Press `?` (or `/`) at any time to open a quick-reference overlay. Press
`Esc` to dismiss it.

## Cheat sheet

### Navigation
| Key | Action |
| --- | --- |
| `↑` | Previous slice |
| `↓` | Next slice |
| `PageUp` | Jump back 10 slices |
| `PageDown` | Jump forward 10 slices |
| `Space` | Play / pause cine |

### Zoom & pan
| Key | Action |
| --- | --- |
| `+` / `=` | Zoom in (applied to every 2D viewport) |
| `-` | Zoom out |
| `0` | Reset zoom and pan |

Zoom hotkeys broadcast to all visible MPR viewports via a window
`CustomEvent` (see `VIEWPORT_ZOOM_EVENT` in `frontend/src/lib/hotkeys.ts`).
Per-viewport wheel zoom (Ctrl+scroll) continues to work independently.

### Window / Level
| Key | Action |
| --- | --- |
| `←` | Narrow window width (−25) |
| `→` | Widen window width (+25) |
| `[` | Lower window center (−25) |
| `]` | Raise window center (+25) |
| `w` | Reset window / level to volume defaults |
| `i` | Invert colors |

### Orientation
| Key | Action |
| --- | --- |
| `r` | Rotate 90° clockwise (all visible 2D viewports) |
| `f` | Flip horizontally |
| `Shift+F` | Flip vertically |

### Layout
| Key | Action |
| --- | --- |
| `1` | Toggle axial viewport |
| `2` | Toggle sagittal viewport |
| `3` | Toggle coronal viewport |
| `4` | Toggle 3D viewport |

### General
| Key | Action |
| --- | --- |
| `?` / `Shift+/` | Show the keyboard-shortcut overlay |
| `/` | Show the keyboard-shortcut overlay |
| `Esc` | Close the overlay, or blur the currently focused element |

## Architecture

The shortcut system lives in two files:

- `frontend/src/lib/hotkeys.ts` — `useHotkeys(bindings)` hook and helpers.
  The hook attaches a single `window` keydown listener, bypasses bindings
  when the event originates from a typing target, matches on key + modifier
  state, and calls `preventDefault` + `stopPropagation` unless
  `allowDefault: true` is set on the binding. Bindings are read through a
  ref so handlers can close over fresh state without forcing the listener
  to reattach each render.
- `frontend/src/components/HotkeyHelpOverlay.tsx` — modal that renders the
  registered bindings grouped into sections.

Each binding has the shape:

```ts
interface HotkeyBinding {
  key: string;              // e.g. "ArrowUp", "+", "?", " "
  handler: (event: KeyboardEvent) => void;
  when?: "viewer" | "global"; // descriptive tag
  ctrl?: boolean;             // require Ctrl / Cmd
  shift?: boolean;
  alt?: boolean;
  description?: string;       // used in the help overlay
  disabled?: boolean;         // conditionally skip the binding
  allowDefault?: boolean;     // don't call preventDefault
}
```

Two helpers dispatch viewport-scoped commands that don't fit a single
React state tree:

- `dispatchViewportZoom(factor: number)` — broadcasts a zoom factor to every
  `MPRViewport`, which listens via the `bitvision:viewport-zoom`
  `CustomEvent`.
- `dispatchViewportResetView()` — resets zoom and pan on every viewport via
  the `bitvision:viewport-reset-view` `CustomEvent`.

## Adding a new shortcut

1. Decide where the handler naturally lives. If it manipulates MPR-layout
   state, add a method to `MPRLayoutHandle` and call it through `mprRef`.
   If it touches the 3D viewer, call `volumeViewerRef.current.*`. If it
   needs to broadcast to all viewports, emit a `CustomEvent` and subscribe
   inside `MPRViewport`.
2. Append a binding to `hotkeyBindings` in
   `frontend/src/app/viewer/series/[id]/page.tsx`.
3. Add it to the appropriate `helpSections` group so it shows up in the
   overlay.
4. Update this document.

## Design notes

- **No library dependencies.** The project rule is "no new libraries", so
  the dispatcher is intentionally small (~100 lines) and uses only
  React + the DOM `KeyboardEvent` API.
- **Single listener.** One `window` keydown listener handles every
  binding. This keeps ordering deterministic and avoids leaks if bindings
  churn between renders.
- **Typing-target guard.** The dispatcher inspects `event.target` and
  skips bindings when the user is inside an editable field. This means the
  W/L range inputs and the search bar never swallow slice-navigation keys,
  and shortcuts never corrupt text input.
- **Case-insensitive letter matching.** `event.key` for a letter depends
  on the Shift modifier (`"R"` vs `"r"`). The dispatcher compares
  single-character keys case-insensitively and applies modifier matching
  separately, so bindings can be written as `key: "r"` regardless of
  caps-lock state.
