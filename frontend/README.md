# frontend — Next.js + OHIF viewer

Web UI for bitvision phoenix. Server-rendered (Next.js App Router) for
SEO on public datasets, with the OHIF v3 viewer embedded for DICOM
visualization.

## Stack

- Next.js 15 + React 19 (App Router, Server Components by default)
- TypeScript (strict)
- [Biome](https://biomejs.dev/) for lint + format (replaces ESLint +
  Prettier — faster, single tool)
- Vitest for unit tests
- OHIF v3 / Cornerstone3D / VTK.js (integrated in phase F1)

## Run

```sh
make frontend.install
make frontend.dev   # http://localhost:3000
```

## Layout

```
src/
  app/             Next.js App Router pages + layouts
  components/      React components
  lib/
    api/
      core.ts      request(), ApiError, qs(), Paginated<T>, auth shims
      index.ts     domain endpoints + re-exports from ./core
    auth-context.tsx
    i18n/          next-intl plumbing
    care_phase_realtime.ts
    iso9660.ts     client-side DVD parsing for UniversalUploader
messages/
  en.json          translation catalogue (authoritative)
  it.json          mirror
middleware.ts      CSP nonce per-request (post 3.7.10)
next.config.mjs    security headers (CSP, HSTS, XFO, Referrer-Policy)
```

## What's here

Production UI. Implemented surfaces:

- Auth: login (cookie-based session post 3.7.9), register, MFA / TOTP,
  password reset, email verification, OIDC opt-in.
- Health Record (Drive UX): folder tree + content pane, hardlinks,
  drag-drop, batch actions, polymorphic context menu, inline preview,
  per-folder header strip, deep-link contract (`?path=`, `?view=`).
- Tabs Drive / Eventi / Documenti / Sintesi & Evidenze / Provenance;
  per-phase page `/patients/[id]/care-phases/[slug]`.
- Viewer at `/viewer/series/[id]`: 2D, MPR, 3D with organ-tuned
  presets (Vr3DRangeControl, Vr3DColorEditor, Vr3DCropBox, plus
  cinematic / shade), measurements, segmentations, fiducials,
  similar-cases panel.
- Settings: AI assistants (per-assistant `client_id`/`client_secret`
  reveal-once card), MFA, privacy, wallet, language switcher.
- Upload: `UniversalUploader` accepts folder / ZIP / ISO 9660 / loose
  DICOM + PDF / images, with client-side DICOMDIR walk.
- i18n: bilingual EN / IT (`next-intl`, see `../docs/i18n.md`).
- Public surfaces: transparency, share-link landing, OpenData library.

Most state is server-driven via the typed client in `lib/api/`.
