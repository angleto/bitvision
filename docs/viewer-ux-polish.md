# Viewer UX Polish (Unit T4)

This unit adds four cross-cutting UX improvements to the series viewer:

1. **Fullscreen mode**
2. **Touch / trackpad gestures**
3. **ARIA accessibility**
4. **Light / Dark / Auto theme toggle**

Each feature lives in its own small module so it can be reused in other views
(study viewer, DICOMweb preview, etc.) without further refactoring.

---

## 1. Fullscreen — `frontend/src/lib/useFullscreen.ts`

A thin React hook wrapping the
[Fullscreen API](https://developer.mozilla.org/en-US/docs/Web/API/Fullscreen_API).

```ts
const { isFullscreen, isSupported, enter, exit, toggle } = useFullscreen(ref);
```

- `ref` — optional `RefObject<HTMLElement>`; defaults to `document.documentElement`.
- `isFullscreen` — reactive boolean kept in sync with the `fullscreenchange`
  event (and the `webkit`, `moz`, `MS` variants for older browsers).
- `isSupported` — feature-detection flag so the UI can hide the button where
  unavailable (e.g. iOS Safari on iPhone, where element-level fullscreen is not
  supported for non-video elements).
- `enter / exit / toggle` — returns a `Promise<void>` and swallows the
  `SecurityError` a user gesture may produce (no console noise on cancel).

### Usage in the viewer

The button is mounted inside `viewer/series/[id]/page.tsx` next to the sidebar
toggle. It targets `layoutRef` (the viewer `.viewer-layout` div) so the header
stays hidden in fullscreen and the MPR grid takes the full viewport.

## 2. Gestures — `frontend/src/lib/useTouchGestures.ts`

A Pointer-Events hook that recognizes:

| Gesture | Action in viewer |
| --- | --- |
| Two-finger pinch | Multiplicative zoom around pinch center |
| Two-finger drag | Pan (same as middle-mouse drag) |
| Double-tap | Fit: reset zoom to 100 % and pan to (0, 0) |

### Design choices

- **Pointer Events only** — we rely on `pointerType === "touch" \|\| "pen"` so
  the desktop mouse path (`onMouseDown/Move/Up` in `MPRViewport.tsx`) is
  completely untouched. That also sidesteps iOS Safari's legacy `TouchEvent`
  quirks.
- **No new state in the viewport** — the hook calls back into the existing
  `setZoom` / `setPan` setters, so the existing draw pipeline renders the
  updates without extra wiring.
- **No gesture unless 2+ fingers** — a single finger keeps behaving like a
  click (crosshair placement / W-L drag).

### Attaching handlers

```tsx
const touch = useTouchGestures({
  enabled: true,
  onPinch: (factor) => setZoom((z) => z * factor),
  onPan: (dx, dy) => setPan((p) => ({ x: p.x + dx, y: p.y + dy })),
  onDoubleTap: () => { setZoom(1); setPan({ x: 0, y: 0 }); },
});

<div {...touch} /* existing mouse handlers... */ />
```

## 3. ARIA accessibility — `MPRViewport.tsx`

Minimal surgical changes:

- `role="img"` on the viewport container — announces the canvas as an image
  region to screen readers.
- `aria-label="DICOM slice viewer (<axis> view)"` — readers get a meaningful
  name per-axis (axial / sagittal / coronal).
- `tabIndex={0}` — the viewport becomes keyboard-focusable so the global
  `:focus-visible` ring (added in `globals.css`) shows which axis currently has
  focus. This is a stepping-stone for future keyboard-driven slice navigation
  (arrow keys).
- `aria-hidden="true"` on the inner `<canvas>` — the canvas pixels themselves
  carry no text, and we don't want AT to announce it as a separate image.

## 4. Theme toggle — `themeToggle.ts` + `ThemeToggle.tsx`

Three modes: **light**, **dark**, **auto** (follows `prefers-color-scheme`).

- `initTheme()` — call once on client boot. `<ThemeToggle />` does this on
  mount, so no layout change is needed (server rendering stays untouched).
- `setTheme(mode)` — persists to `localStorage["bv-theme"]` and toggles the
  `.dark` class on `<html>`.
- `subscribeTheme(cb)` — small pub/sub so multiple toggles on the page stay in
  sync.

### CSS variables

`globals.css` now exposes per-theme CSS variables:

```css
:root { --bv-bg, --bv-fg, --bv-card-bg, --bv-card-border, --bv-muted, ... }
html.dark { /* overrides */ }
```

`body` reads `var(--bv-bg)` and `var(--bv-fg)` — existing hardcoded colors on
individual surfaces (viewer sidebar, site header) still render as before and
can be migrated to tokens incrementally.

The shared `.viewer-chip-btn` class in `globals.css` deduplicates the
28×28 glass-style overlay buttons used by the theme toggle, the fullscreen
button, and the sidebar toggle.

### Reduced motion

`@media (prefers-reduced-motion: reduce)` in `globals.css` silences the
loading-bar pulse and the spinner for users with vestibular sensitivity.

---

## Files

### Created
- `frontend/src/lib/useFullscreen.ts`
- `frontend/src/lib/useTouchGestures.ts`
- `frontend/src/lib/themeToggle.ts`
- `frontend/src/components/ThemeToggle.tsx`
- `docs/viewer-ux-polish.md` (this file)

### Modified
- `frontend/src/app/globals.css` — added theme CSS variables, `:focus-visible`
  ring, reduced-motion media query.
- `frontend/src/components/MPRViewport.tsx` — ARIA role/label, `tabIndex`,
  opt-in touch-gesture handlers.
- `frontend/src/app/viewer/series/[id]/page.tsx` — mounted `<ThemeToggle />`
  and fullscreen button inside the existing bottom-right overlay.
