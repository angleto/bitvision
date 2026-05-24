# Imaging AI integration plan

Snapshot 2026-05-01.

This document is the dedicated plan for the **imaging AI** front of
BitVision Phoenix. It is **not** a roadmap phase: F9 in `DESIGN.md` §8
is the P2P dataset marketplace + Stripe Connect, not imaging AI. The
imaging AI work is fragmented across the gap analysis in `DESIGN.md`
§10 and the agents-API ROADMAP residuals; this file consolidates it
into a single executable plan.

Related documents:
- `docs/segmentation-engines.md` — current state of the three engines
- `docs/agents-api/ROADMAP.md` — agents API sprint history
- `docs/agents-api/decisions/` — ADR index (ADR 0013 covers ARM64)
- `docs/DESIGN.md` §8 (roadmap), §10 (gap analysis)

## 1. Current state (verified on tree 2026-05-01)

Three segmentation engines are already integrated, gated by
`write:annotations`, with masks stored under
`segmentations/{series_id}/{label}.bin` (raw `uint8`, x-fastest) and
consumed by the volume renderer through `setSegmentationMask`.

| Engine | Mode | Endpoint | Worker | Notes |
|---|---|---|---|---|
| TotalSegmentator (Apache-2.0, 117 ROIs) | automatic batch | `POST /api/series/{id}/segmentations/auto` | `segment_auto` | UI presets Fegato / Addome / Tutto, CPU 5-15 min/scan |
| MedSAM-2 / SAM-2 | interactive 2D per click | `POST /api/series/{id}/segmentations/interactive/predict` | `medsam_predict_2d` | manual install, lazy import |
| MONAI Label | external server proxy | `GET /api/segmentations/monai_label/info` | (proxy only) | upstream URL via `BVP_MONAI_LABEL_URL` |

## 2. Immediate residuals (P2, blocking production)

1. **TotalSegmentator worker on ARM64**. ADR 0013, "wheel spike in
   ops backlog". Blocking for the production cluster (managed
   Kubernetes, 2 ARM64 nodes). Three options:
   - (a) build a `manylinux2014_aarch64` wheel in-house;
   - (b) run x86 emulation in the cluster (slow, demo only);
   - (c) add a separate x86 GPU node pool for AI workers.

   Preference: (a) or (c).

2. **E2E test "agent reclassification flow"**. Requires an LLM harness
   (real or recorded). Tracked under the agents-API roadmap.

## 3. Open gaps (DESIGN.md §10 cross-reference)

§10.1 — Critical (clinical use blockers):
- LLM citation provenance `(entity_kind, id, span)` on every response — **absent**.
- DICOM SR Comprehensive3D import / export via `highdicom` — **partial** (only `Marker` today).
- Critical findings closed-loop — **absent**.
- Structured reporting with template + sign workflow — **partial**.

§10.2 — High value, directly imaging AI:
- Lesion tracking RECIST cross-study (segmentation propagation + rigid registration) — **absent**.
- Fusion auto-registration 6-DOF rigid (today only manual overlay via `fusionActor`) — **partial**.
- Auto-prioritization PE / ICH triage on the worklist — **absent**, depends on worklist (§10.3).
- Voice dictation Whisper-large-v3 — **absent**.

§10.4 — Long-term:
- App store / certified extensions, distinct from the F9 dataset marketplace.
- WebGPU rendering pipeline.
- Federated learning.

## 4. Proposed plan (3 steps, ordered by value-to-effort)

### Step 1 — Unblock TotalSegmentator on ARM64 in production

Without this the rest of the plan is theoretical: the production
cluster cannot run `segment_auto`.

Tasks:
- Re-read ADR 0013 in `docs/agents-api/decisions/`.
- Verify whether upstream TotalSegmentator now publishes ARM64 wheels
  (status may have changed since the ADR).
- Decide between (a) building a `manylinux2014_aarch64` wheel and
  caching it in the workers image, or (c) adding a GPU x86 node pool
  to the cluster with a tainted nodeselector for AI workers.
- Land the chosen path behind a feature toggle so dev and prod
  can diverge during rollout.

Acceptance: `segment_auto` runs end-to-end on the production cluster
against a live CT phantom, producing the same outputs as the dev box.

### Step 2 — DICOM SEG export via `highdicom`

Today masks ship as raw `.bin` blobs; this is fine internally but
opaque to any third-party tool.

Tasks:
- Add a `GET /api/series/{id}/segmentations/{label}.dcm` endpoint
  that materializes a DICOM SEG instance from the existing `.bin`
  mask + the source series' geometry.
- Use `highdicom.seg.SegmentationStorage` with appropriate
  `SegmentDescription` (anatomic region from the label name when
  available).
- Reuse the same approach for the `Marker` table to start closing
  the SR Comprehensive3D gap (§10.1).
- Frontend: download button in `SegmentationImporter`.

Acceptance: SEG opens correctly in 3D Slicer and OHIF without
re-encoding.

### Step 3 — MONAI Deploy MAP wrapper as second backend of `segment_auto`

A MONAI Application Package (MAP) is a DICOM-in / DICOM-SEG-out
container. Wrapping the worker around a MAP runner means new models
can be added without backend changes.

Tasks:
- Open an ADR in `docs/agents-api/decisions/` covering: storage
  contract (S3 input prefix, output prefix), runtime (local docker
  vs Kubernetes Job), and how the existing `segment_auto` worker
  selects between native TotalSegmentator and MAP execution.
- First MAP to host: TotalSegmentator itself, since NVIDIA
  distributes it as a MAP. This validates the pipeline without
  introducing a new model.
- Subsequent MAPs (BraTS, AbdomenCT-1K, retinopathy from MONAI
  Bundle Zoo) load by registering a new container, no backend code.
- Verify the MAP path also produces output mappable to our
  `segmentations/{series_id}/{label}.bin` layout (or convert from
  DICOM SEG output of the MAP).

Acceptance: a second MAP (any non-TotalSegmentator one) runs
through `segment_auto` end-to-end with no Python code change in the
backend.

This step naturally feeds §10.4 "App store / certified extensions"
as a future surface, distinct from the F9 dataset marketplace.

## 5. Follow-ups (after the three steps above)

In §10.2 priority order:

4. **Fusion auto-registration 6-DOF rigid** baseline. Replace the
   manual overlay path with a registration solver (SimpleITK or
   MONAI registration block).
5. **Lesion tracking RECIST** with propagation across serial studies.
   Builds on Step 4 and on the Step 2 SEG export.
6. **LLM citation provenance + critical findings closed-loop**.
   Lifts the platform from "viewer + LLM playground" to a complete
   reporting tool. Cross-cuts §10.1 and §10.2.

## 6. Out of scope (intentional)

- F9 (dataset marketplace) and Stripe Connect: orthogonal to this plan.
- MDR / CE certification: tracked separately under §10.3.
- WebGPU and full PBR cinematic rendering: §10.4 long-term.

## 7. How to refresh this document

- Verify with `git log` that ADR 0013 has not been superseded.
- Re-check `docs/segmentation-engines.md` to confirm the three
  engines are still wired the same way.
- If the upstream TotalSegmentator status has changed (ARM64 wheels
  published officially, MAP package deprecated), update Steps 1 and 3
  before starting work.
