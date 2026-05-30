"use client";

// Client-side GPU MIP viewport (Addendum A §6).
//
// Where the legacy ``MipCineViewport`` plays back a server-rendered
// PNG sprite (pre-baked frames at fixed angles), this one runs a
// real-time raycast on the volume already loaded for the MPR triple.
// Trade-offs:
//
//   * pros: 60 fps rotation, free zoom + pan, slab thickness slider,
//     re-renders instantly when the SUV window or LUT changes (no
//     server round-trip, no PNG decode).
//   * cons: needs the volume.raw bytes in the browser cache (which
//     ``CornerstoneMPRLayout`` ensures via ``createLocalVolume``); a
//     cold visit pays a brief upload-to-GPU cost the cine flow
//     hides behind sprite caching.
//
// We sit on top of Cornerstone's ``VolumeViewport3D`` and flip the
// underlying vtk volume mapper into ``MAXIMUM_INTENSITY_BLEND``.
// ``TrackballRotateTool`` becomes the primary mouse drag, so dragging
// orbits the camera around the volume centre exactly the way the
// OHIF live example does.

import * as cs from "@cornerstonejs/core";
import * as csTools from "@cornerstonejs/tools";
import { useCallback, useEffect, useRef, useState } from "react";

import { ensureCornerstoneInit } from "@/lib/cornerstoneSetup";
import { buildLocalVolume } from "@/lib/cornerstoneVolume";
import type { VolumeData } from "./VolumeViewer";

interface Props {
  /** Stable id used to derive the cache key for the loaded volume.
   *  Should match the seriesId the parent MPR layout used so the
   *  same cached volume is reused (no second upload). */
  seriesId: string;
  /** Already-decoded volume data the parent has fetched. We re-use
   *  the parent's cached Cornerstone volume by referencing the same
   *  volumeId; the prop is kept for the rare case where the cache
   *  miss happens (e.g. someone deep-links straight to the MIP
   *  viewport). */
  volume: VolumeData;
  /** DICOM modality of the primary series. Drives the
   *  active-volume picker below: when ``modality === "PT"`` we MIP
   *  the primary; when the FUSION is the PT we MIP the fusion. */
  modality?: string | null;
  /** Optional fusion overlay context. When present AND the fusion
   *  is the PT (or another modality more clinically useful for MIP
   *  than the primary), the MIP renders the FUSION volume instead
   *  of the primary. PET-CT review is the driving case: primary is
   *  CT, fusion is PT, and a CT-MIP is rarely useful — radiologists
   *  read the PT-MIP. */
  fusionSeriesId?: string | null;
  fusionVolume?: VolumeData | null;
  fusionModality?: string | null;
  /** "Fusion is expected" hint forwarded by the layout from the page's
   *  URL search param (``?fusion=<id>``). Available synchronously at
   *  page mount, before the parent fetches the fusion volume. The MIP
   *  uses this — combined with ``fusionSeriesId`` for non-URL flows like
   *  the picker — to decide whether to suspend ``setup()`` while waiting
   *  for the volume to land.
   *
   *  Why a separate prop and not just ``fusionSeriesId``: the parent
   *  publishes ``csFusionSeriesId`` only after ``fetchVolume`` resolves,
   *  because the MPR layout's local-volume cache entries need the
   *  matching FoR sintetizzata before Cornerstone tries to layer them
   *  (publishing the seriesId before the volume causes a stub cache
   *  entry with default FoR = volumeId, and a later ``setVolumes``
   *  rejects two volumes with mismatched FoRs — beta.79 regression).
   *  So the URL deep-link case used to see ``fusionSeriesId === null``
   *  on the first MIP render, ran setup with the CT primary, then
   *  re-ran with the PT fusion when the volume landed ~1.5 s later,
   *  leaving the renderer stuck on a stale frame ("white MIP at first
   *  mount", reproducible only on the URL flow). With this prop the
   *  MIP knows fusion is incoming as soon as the URL is parsed and
   *  waits before touching the viewport. */
  fusionExpected?: boolean;
  /** Failure flag for the fusion fetch (URL ``?fusion=`` flow). Forwarded
   *  by the layout from the page's `csFusionFailed` state, set to
   *  ``true`` on ``.catch()`` of ``fetchVolume`` or on the 30s
   *  watchdog abort. Releases the ``fusionPending`` gate without
   *  requiring a successful ``fusionVolume`` payload, so the MIP
   *  proceeds primary-only (or stays unmounted if it isn't on the
   *  PET path) instead of hanging forever waiting for a volume that
   *  will never arrive. ``false`` (default) means "in progress or
   *  successful". */
  fusionFailed?: boolean;
  /** SUV body-weight factor for the active PET volume. When set,
   *  the W/L slider is labelled in SUV units (pixel × factor) and
   *  the default window targets a clinical SUV scale instead of
   *  raw Bq/mL — so the slider reads "SUV 0..15" the way Hermes /
   *  Syngo / Mirada present it, not "Bq/mL 0..27000". */
  suvFactorBw?: number | null;
}

// Reuse the MPR layout's RenderingEngine + ToolGroup instead of
// spawning our own. Each cs.RenderingEngine opens a private
// offscreen WebGL context, and modern browsers cap the per-tab
// context budget at ~16. Stacking a MIP engine on top of the MPR
// engine plus the StrictMode dev double-mount was reliably pushing
// the count past that ceiling, the browser was reclaiming the
// MPR's contexts as 'oldest active', and the user saw the MPR
// slice panes go black while the MIP was on. Sharing the engine
// keeps the whole viewer at one WebGL context per study.
const MPR_ENGINE_ID_PREFIX = "bvp-mpr-cs-engine:";
const MPR_TOOL_GROUP_PREFIX = "bvp-mpr-cs-toolgroup:";
const VP_PREFIX = "bvp-mip-3d:";
// Volume cache prefixes mirror the ones in CornerstoneMPRLayout so
// the MIP picks up the same ``ImageVolume`` (and therefore the same
// ``vtkOpenGLTexture``) the MPR has already uploaded. One scalar
// texture lives on the GPU per study, regardless of how many
// viewports project it; the MIP's MAXIMUM_INTENSITY blend mode is
// confined to the MIP's own mapper instance — see the mapper-swap
// block in ``setup`` for the rationale.
const VOLUME_PREFIX_PRIMARY = "bvp-vol-primary:";
const VOLUME_PREFIX_FUSION = "bvp-vol-fusion:";

/** PT-skewed default colormap stops used when the volume looks PET-y
 *  (most voxels in the upper percentile tail). For non-PET volumes
 *  we use the identity grayscale, which gives a clean MIP-style
 *  black-on-white readout. The colormap is wired via ``setProperties``
 *  on the viewport, not the actor, so the user can still flip it
 *  later via the layout's preset picker. */
function isLikelyPet(volume: VolumeData): boolean {
  // Heuristic: PET volumes have a long zero-dominated tail; the 99th
  // percentile is much greater than the median. CT/MR volumes have
  // a more uniform histogram.
  const N = volume.scalars.length;
  if (N === 0) return false;
  const SAMPLE = Math.min(20000, N);
  const stride = Math.max(1, Math.floor(N / SAMPLE));
  const samples: number[] = [];
  for (let i = 0; i < N; i += stride) {
    const v = volume.scalars[i];
    if (Number.isFinite(v) && v > 0) samples.push(v);
  }
  if (samples.length < 100) return false;
  samples.sort((a, b) => a - b);
  const median = samples[Math.floor(samples.length * 0.5)];
  const p99 = samples[Math.floor(samples.length * 0.99)];
  return p99 > median * 5;
}

/** PT/PET/NM modalities — those for which the inverse-grayscale-on-
 *  white MIP is the clinical convention. */
function isPetModality(mod: string | null | undefined): boolean {
  const m = (mod ?? "").toUpperCase();
  return m === "PT" || m === "PET" || m === "NM";
}

/** Robust percentile-based [lo, hi] for the OTF / CTF mapping. The
 *  packed volume header reports the data's full min/max, but for MIP
 *  rendering we want the "useful tissue" band: the 1st percentile
 *  marks the air floor (CT) or near-zero background (PT), the 99.5th
 *  percentile marks the brightest sustained signal (cortical bone /
 *  contrast pool / hot lesion), without bleed from outliers. Sampling
 *  is sparse so the call is sub-millisecond on a typical CT.
 */
function computeMipRange(
  scalars: Float32Array,
  fullRange: [number, number],
  isPet: boolean,
): [number, number] {
  const N = scalars.length;
  if (N === 0) return fullRange;
  const SAMPLE = Math.min(50000, N);
  const stride = Math.max(1, Math.floor(N / SAMPLE));
  const samples: number[] = [];
  for (let i = 0; i < N; i += stride) {
    const v = scalars[i];
    // PET: ignore zero background (which dominates voxels outside
    // the body). CT/MR: keep all finite samples (air at -1024 HU
    // is still meaningful for the floor calculation).
    if (!Number.isFinite(v)) continue;
    if (isPet && v <= 0) continue;
    samples.push(v);
  }
  if (samples.length < 100) return fullRange;
  samples.sort((a, b) => a - b);
  const pick = (p: number) =>
    samples[Math.max(0, Math.min(samples.length - 1, Math.floor(p * samples.length)))];
  if (isPet) {
    // PET MIP: lo at 1st percentile of nonzero (just above the air
    // floor, so the body silhouette is visible — the radiologist
    // expects to see the patient outline plus the hot spots), hi at
    // 99.5th so the brightest lesions saturate at the dark end of
    // the inverse-grayscale ramp. Earlier we used p50 as the floor,
    // which suppressed half the body and produced an all-white MIP
    // on scans without dramatic lesions.
    return [pick(0.01), pick(0.995)];
  }
  // CT/MR MIP: lo at 1st percentile (just above the air floor),
  // hi at 99th (well above the brightest sustained tissue).
  return [pick(0.01), pick(0.99)];
}

export default function CornerstoneMipViewport({
  seriesId,
  volume,
  modality,
  fusionSeriesId,
  fusionVolume,
  fusionModality,
  fusionExpected: fusionExpectedProp,
  fusionFailed,
  suvFactorBw,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const engineRef = useRef<cs.RenderingEngine | null>(null);
  const animFrameRef = useRef<number | null>(null);
  // Default off: a rotating view on initial load disorients the
  // radiologist before they've taken in the static anterior
  // projection, which is the canonical PET-MIP read. The toggle
  // stays in the panel so they can spin through orientations on
  // demand.
  const [autoplay, setAutoplay] = useState(false);
  // Slab thickness (in mm) — the depth along the camera direction
  // that the MIP integrates over. ``0`` = whole volume MIP (the
  // standard rotating MIP). >0 = thin-slab MIP, useful for focal
  // analysis without the silhouette of distant uptake.
  const [slabMm, setSlabMm] = useState(0);
  // Mirror of ``slabMm`` for use inside the imperative ``applySlab``
  // closure stashed on the viewport (see ``setup``). The closure is
  // captured once per setup and is invoked from monkey-patched
  // viewport methods on every CS3D camera-state change; reading from
  // a ref avoids re-patching on every slider tick.
  const slabMmRef = useRef(slabMm);
  // Window/level for the MIP transfer function. ``null`` means
  // "auto from percentile" (the default until the user touches the
  // slider). Stored as [lo, hi] in the volume's native units (Bq/mL
  // for PET BQML, HU for CT). The W/L panel exposes them in SUV
  // units when ``suvFactorBw`` is set.
  const [winLoHi, setWinLoHi] = useState<[number, number] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Auto window range computed from data percentiles — used as the
  // default and as the bound for the W/L slider so the user can't
  // accidentally pick a window so narrow that the MIP turns black.
  const autoRangeRef = useRef<[number, number] | null>(null);
  // Maximum slab thickness in mm — derived from the active volume
  // extent so whole-body PET (~1700 mm in the cranio-caudal axis)
  // can be covered. The previous hard-coded 300 mm cap was thinner
  // than the patient, making "full" via the slider impossible.
  const [slabMaxMm, setSlabMaxMm] = useState(2000);

  // Pick the volume to MIP. PET-CT convention: when the primary is
  // CT and the fusion is PT, render the PT MIP — the CT-MIP is
  // clinically uninformative (a soft-tissue silhouette), the PT-MIP
  // is the standard read for tumour staging and metabolic survey.
  // When the primary IS PT or no fusion is loaded, render the
  // primary; when neither side is PT, default to the primary.
  const primaryIsPet = isPetModality(modality);
  const fusionIsPet = isPetModality(fusionModality);
  const useFusion = fusionVolume != null && fusionSeriesId != null && fusionIsPet && !primaryIsPet;
  const activeSeriesId = useFusion && fusionSeriesId ? fusionSeriesId : seriesId;
  const activeVolume = useFusion && fusionVolume ? fusionVolume : volume;
  const activeModality = useFusion ? fusionModality : modality;
  const activeIsPet = isPetModality(activeModality);

  // Engine / viewport / toolgroup ids are pinned to the PARENT
  // series id, not to ``activeSeriesId``. The active volume can
  // flip from primary to fusion (or vice versa) while the user is
  // looking — for instance the deep-link fusion fetch lands a
  // few hundred ms after the page mounts. If the engine id
  // changed each time, we'd spawn a new ``cs.RenderingEngine`` per
  // flip, each one opening its own WebGL context; the browser
  // reclaims oldest contexts after ~16, which is the symptom users
  // see as 'MPR panes go black after a brief correct render'.
  // Pinning the engine id keeps a single context alive for the
  // lifetime of the viewer, and we just rebind the volume on
  // ``activeSeriesId`` changes.
  const engineId = `${MPR_ENGINE_ID_PREFIX}${seriesId}`;
  const toolGroupId = `${MPR_TOOL_GROUP_PREFIX}${seriesId}`;
  const vpId = `${VP_PREFIX}${seriesId}`;
  // Volume id matches the prefix the MPR layout uses, so when the
  // active volume is the fusion we point at the SAME cache entry the
  // MPR's fusion overlay is already using (and same for the primary
  // case). One ImageVolume + one ``vtkOpenGLTexture`` per study
  // regardless of how many viewports project it — without this, the
  // MIP would force a second upload of an already-loaded volume and
  // the doubled GPU pressure is what was getting the browser to evict
  // MPR contexts.
  const volumeId = useFusion
    ? `${VOLUME_PREFIX_FUSION}${activeSeriesId}`
    : `${VOLUME_PREFIX_PRIMARY}${activeSeriesId}`;

  // Fusion-arrival gate. We "expect" fusion when EITHER the URL search
  // param told the page so (forwarded as ``fusionExpectedProp``,
  // available synchronously at page mount) OR the parent has already
  // published a ``fusionSeriesId`` (covers the picker path, where the
  // user opts into fusion after the page is up).
  //
  // ``fusionVolume`` + ``fusionModality`` land via separate async
  // fetches — typically a few hundred ms after the page mounts.
  // Running ``setup`` before they arrive renders the primary (CT)
  // MIP first, then a second pass swaps volumeId / blend / OTF / CTF
  // mid-flight when the fusion lands. That second pass reuses the
  // existing viewport (background pinned to the CT-era ``[0,0,0]``)
  // and leaves the renderer stuck on a stale frame: the user sees
  // an all-white MIP that only un-sticks when they toggle the pane
  // off+on. Wait until the fusion is fully resolved before touching
  // the viewport, so the very first ``setup`` run already sees the
  // correct ``activeVolume`` / ``activeIsPet`` and configures
  // everything in a single pass.
  //
  // Earlier we keyed the gate off ``fusionSeriesId`` alone, but the
  // parent (correctly) only publishes ``csFusionSeriesId`` after
  // ``fetchVolume`` resolves — otherwise the MPR layout's
  // ``setVolumesForViewports`` registers a stub cache entry with the
  // default FoR (= volumeId) and the next layered ``setVolumes``
  // rejects two volumes with mismatched FoRs (beta.79 regression).
  // Adding the URL-derived prop closes the URL-deep-link race
  // without disturbing the MPR's publish ordering.
  const fusionExpected = fusionExpectedProp === true || fusionSeriesId != null;
  // ``fusionFailed`` rilascia il gate quando il fetch ha rigettato
  // (404 / network / watchdog 30s) così la MIP esce dal wait e
  // procede primary-only — gestito dalla logica downstream
  // (``useFusion=false`` quando ``fusionVolume`` è null), niente hang
  // perpetuo aspettando un volume che non arriverà mai.
  const fusionPending =
    fusionExpected && fusionFailed !== true && (fusionVolume == null || fusionModality == null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  const setup = useCallback(async () => {
    if (fusionPending) return;
    await ensureCornerstoneInit();
    const el = containerRef.current;
    if (!el) return;

    // Reuse the parent layout's cached volume when available; build
    // a local copy only as a fallback. Keeps the GPU upload cost at
    // zero on the typical case where the user already has the MPR
    // mounted.
    if (!cs.cache.getVolume(volumeId)) {
      // Cold-cache fallback (MIP mounted before the MPR layout). Build
      // through the shared helper so the geometry and FrameOfReference
      // match exactly what the MPR layout would produce for this same
      // ``volumeId`` — they share the Cornerstone cache, so a divergent
      // frame here would desync crosshair-jump and fusion layering.
      buildLocalVolume(
        volumeId,
        activeVolume,
        activeVolume.frameOfReferenceUid ?? `bvp-for:${activeSeriesId}`,
      );
    }

    // Wait for the MPR layout to register its rendering engine.
    // The MPR mounts in parallel with the MIP and its setup is
    // async; if we ran ahead of it and instantiated a fresh
    // engine here we'd get TWO engines on the same study (each
    // with its own WebGL context — the original bug).
    let engine = cs.getRenderingEngine(engineId);
    let waited = 0;
    while (!engine && waited < 8000) {
      await new Promise((r) => setTimeout(r, 100));
      waited += 100;
      engine = cs.getRenderingEngine(engineId);
    }
    if (!engine) {
      setErr("MPR rendering engine not available");
      return;
    }
    engineRef.current = engine;

    // Pin the viewport background up-front: white for PT (so the
    // inverse-grayscale MIP reads like a paper print) and pure
    // black otherwise.
    const bg: cs.Types.Point3 = activeIsPet ? [1, 1, 1] : [0, 0, 0];
    // ``enableElement`` adds (or replaces) ONE viewport on the
    // existing engine without touching the other three; the MPR
    // layout's axial/sagittal/coronal viewports keep their state.
    // ``setViewports`` would have nuked them.
    try {
      const existing = engine.getViewport(vpId);
      if (existing) {
        // Already attached on a previous setup — re-use without
        // disabling first to avoid a context flicker on the
        // re-mount. Only update the viewport if the element
        // changed.
      } else {
        const ee = engine as unknown as {
          enableElement: (entry: {
            viewportId: string;
            type: cs.Enums.ViewportType;
            element: HTMLElement;
            defaultOptions?: { background?: cs.Types.Point3 };
          }) => void;
        };
        ee.enableElement({
          viewportId: vpId,
          type: cs.Enums.ViewportType.VOLUME_3D,
          element: el,
          defaultOptions: { background: bg },
        });
      }
    } catch (exc) {
      setErr(exc instanceof Error ? exc.message : String(exc));
      return;
    }

    // Compute a clinically meaningful [lo, hi] window from the
    // active volume's data, NOT from the header's nominal range.
    // The header's range can be wide (e.g. CT [-1024, 3071]) even
    // when the actual data sits in a narrow band (a slab of
    // abdomen with no bone), and using the header window pushes
    // the per-ray max into the alpha=0 region of any non-trivial
    // OTF — which was the all-black symptom users hit on
    // contrast-enhanced abdominal MIP.
    const autoRange = computeMipRange(activeVolume.scalars, activeVolume.range, activeIsPet);
    autoRangeRef.current = autoRange;
    // User-driven W/L overrides the percentile default once the
    // slider is touched; until then ``winLoHi`` stays null and the
    // setup uses the auto range so the first frame is sensible.
    const [lo, hi] = winLoHi ?? autoRange;
    const span = hi - lo || 1;

    await cs.setVolumesForViewports(
      engine,
      [
        {
          volumeId,
          callback: ({ volumeActor }) => {
            const ofun = volumeActor.getProperty().getScalarOpacity(0) as unknown as {
              addPoint: (x: number, y: number) => void;
              removeAllPoints: () => void;
            };
            const ctf = volumeActor.getProperty().getRGBTransferFunction(0) as unknown as {
              addRGBPoint: (x: number, r: number, g: number, b: number) => void;
              removeAllPoints: () => void;
            };
            ofun.removeAllPoints();
            ctf.removeAllPoints();
            // MIP samples a single scalar per ray (the max), so the
            // OTF is just a binary floor: alpha 0 below the air /
            // background cutoff, alpha 1 above. The CTF carries the
            // visible shading — a linear ramp over [lo, hi]. The
            // earlier OTFs that ramped alpha gradually 0→1 across
            // the body band produced very dim images that read as
            // "all black" on a clinical monitor.
            //
            // ``lo`` / ``hi`` come from ``computeMipRange``: 1st-99th
            // percentile of nonzero data (CT/MR) or 50th-99.5th of
            // nonzero (PT). That clamps the floor onto real air and
            // the peak onto sustained signal regardless of how wide
            // the volume header reports.
            const eps = span * 0.005;
            if (activeIsPet) {
              // PET MIP: inverse grayscale on white. lo → white
              // (background tissue), hi → black (peak uptake).
              ofun.addPoint(lo - eps, 0);
              ofun.addPoint(lo, 1);
              ofun.addPoint(hi, 1);
              ctf.addRGBPoint(lo, 1, 1, 1);
              ctf.addRGBPoint(hi, 0, 0, 0);
            } else {
              // CT / MR / generic MIP: grayscale on black. lo →
              // black (air / background), hi → white (bone /
              // contrast pool).
              ofun.addPoint(lo - eps, 0);
              ofun.addPoint(lo, 1);
              ofun.addPoint(hi, 1);
              ctf.addRGBPoint(lo, 0, 0, 0);
              ctf.addRGBPoint(hi, 1, 1, 1);
            }
            // Per-viewport mapper isolation, free of the texture-
            // upload race that the previous fix paid for.
            //
            // ``setVolumesForViewports`` already gives this actor its
            // own freshly-constructed ``vtkSharedVolumeMapper``
            // instance — each call to ``createVolumeActor`` mints a
            // new mapper and a new ``model.blendMode``. The MPR's
            // axial / sagittal / coronal mappers were created by
            // separate ``createVolumeActor`` invocations, so setting
            // MAXIMUM_INTENSITY on this actor's mapper does not bleed
            // into theirs.
            //
            // We still allocate ANOTHER ``vtkSharedVolumeMapper`` on
            // top, defensively. The mapper that ``setVolumesForViewports``
            // hands us is registered as a graphics-resource user on
            // the offscreen render window the moment the actor is
            // attached; an extra round-trip through
            // ``cs.createVolumeMapper`` gives us a brand-new abstract
            // mapper + brand-new GL view node, provably distinct from
            // any state any other viewport may already hold.
            //
            // Crucially the new mapper REUSES the cached
            // ``ImageVolume.vtkOpenGLTexture`` (the 3D scalar texture
            // already streaming-uploaded on GPU). Cornerstone's view
            // node factory passes the abstract mapper's
            // ``getScalarTexture()`` straight into the streaming
            // OpenGL volume mapper as ``model.scalarTextures[0]``,
            // so the MIP samples the same physical texture as the
            // MPR slices. No extra upload (~80 MB saved per PET on
            // both CPU heap and GPU memory), no first-frame race,
            // no white frame, no rAF kick.
            //
            // Earlier this block called
            // ``convertMapperToNotSharedMapper`` to materialise a
            // plain ``vtkVolumeMapper`` (kitware native), to avoid
            // a presumed-shared abstract mapper. That mapper is
            // rendered by kitware's ``vtkOpenGLVolumeMapper``, which
            // re-uploads its own 3D texture from
            // ``imageData.getPointData().getScalars()`` on the FIRST
            // ``buildBufferObjects``. The first frame committed the
            // upload but rasterised before the new texture became
            // sampleable — the canvas froze on the all-white
            // background until something else triggered another
            // render. The two-rAF "kick" workaround was unreliable
            // and the conversion also paid for a duplicate scalar
            // texture (~80 MB GPU). The shared-mapper-with-shared-
            // texture path sidesteps the whole class of bugs.
            const imageVolume = cs.cache.getVolume(volumeId) as
              | { imageData?: unknown; vtkOpenGLTexture?: unknown }
              | undefined;
            if (imageVolume?.imageData && imageVolume?.vtkOpenGLTexture) {
              const mipMapper = (
                cs as unknown as {
                  createVolumeMapper: (
                    imgData: unknown,
                    tex: unknown,
                  ) => {
                    setBlendModeToMaximumIntensity?: () => void;
                  };
                }
              ).createVolumeMapper(imageVolume.imageData, imageVolume.vtkOpenGLTexture);
              mipMapper.setBlendModeToMaximumIntensity?.();
              (
                volumeActor as unknown as {
                  setMapper: (m: unknown) => void;
                }
              ).setMapper(mipMapper);
            } else {
              // Cache lookup failed — fall back to mutating the
              // mapper that the actor already has. Better than a
              // broken render, even if the defensive isolation is
              // forfeit; the only realistic way this branch fires
              // is a teardown-mid-setup where the volume was
              // evicted between createLocalVolume and here.
              const fallback = volumeActor.getMapper() as unknown as {
                setBlendModeToMaximumIntensity?: () => void;
              };
              fallback.setBlendModeToMaximumIntensity?.();
            }
            // Disable shading: for MIP it makes no physical sense
            // (we're picking max along ray, not Beer-Lambert).
            volumeActor.getProperty().setShade(false);
          },
        },
      ],
      [vpId],
    );

    // Attach the MIP viewport to the MPR layout's existing tool
    // group (same engine, same toolGroup). ``addViewport`` is
    // additive; the axial/sagittal/coronal bindings stay intact.
    // We do NOT add Pan / Zoom / TrackballRotate primary bindings
    // here because the MPR's tool activation effect already
    // controls all primary bindings on this group; the MIP-only
    // gesture (Z-axis drag) lives on a custom DIV listener below.
    const tg = csTools.ToolGroupManager.getToolGroup(toolGroupId);
    if (tg) {
      try {
        tg.addViewport(vpId, engineId);
      } catch {
        /* viewport already in the tool group from a previous mount */
      }
    }

    // Reset camera so the volume fits the viewport, then orient it
    // to the canonical anterior view. Values mirror CS3D's
    // ``mprCameraValues.coronal`` preset:
    //   - ``viewPlaneNormal: [0, -1, 0]`` (camera at -Y looking
    //     toward +Y; in DICOM LPS that's looking from the patient's
    //     anterior into the volume)
    //   - ``viewUp: [0, 0, 1]`` (image space +Z = patient superior;
    //     head ends up at the top of the canvas)
    // Without this explicit orientation vtk's resetCamera lands
    // along +Z, projecting top-down — the 'see head + soles in one
    // frame' view that's never how a radiologist reads a PET MIP.
    //
    // Null-guard: if the component unmounted (or the pane was
    // toggled off / a sibling was maximized) during the awaited
    // ``setVolumesForViewports`` call above, the cleanup ran
    // ``disableElement(vpId)`` and ``getViewport`` now returns
    // undefined. Bail silently — the next mount will re-do setup.
    const vp = engine.getViewport(vpId) as cs.Types.IVolumeViewport | undefined;
    if (!vp) return;

    // Slab-clip-range patches.
    //
    // VolumeViewport3D.setCamera (CS3D 4.x) calls
    // ``renderer.resetCameraClippingRange()`` and then ``render()``
    // on every invocation, which auto-fits the vtk camera's clip
    // range to the volume bounds (i.e. wide-default). Anything that
    // touches the camera (rotate, zoom, pan, resize, autoplay tick,
    // ResizeObserver) goes through ``setCamera`` and clobbers the
    // narrow slab we set for the MIP. Result: the slider visibly
    // changed the slab during drag (because the slab effect re-fired
    // each onChange tick faster than the camera-modified events that
    // followed), but on release the next CS3D internal render reset
    // it back to wide-default and the slab disappeared.
    //
    // Fix: install ``applySlab`` on the viewport instance, then
    //   1. wrap ``setCamera`` to call the original (which fires the
    //      auto-reset internally) and immediately re-apply our slab,
    //      followed by a render. Two renders per setCamera is the
    //      cost; the slab now survives every interaction.
    //   2. replace ``setCameraClippingRange`` (the wide-default
    //      fallback method) with applySlab outright — anything that
    //      reaches for the wide default gets our slab instead.
    //   3. expose ``applySlab`` on the viewport so the slider's
    //      effect can re-apply on slabMm change without going
    //      through setCamera (cheap, no extra render queued).
    //
    // The viewport instance is destroyed by ``disableElement`` on
    // unmount, so the patches don't leak across remounts.
    type VtkCam = {
      setClippingRange: (n: number, f: number) => void;
    };
    const vpAny = vp as unknown as {
      getVtkActiveCamera?: () => VtkCam;
      setCamera: (props: unknown) => void;
      setCameraClippingRange?: () => void;
      _bvp_slab_apply?: () => void;
      _bvp_window_apply?: (lo: number, hi: number) => void;
    };
    // Derive the volume's maximum world-space extent (the diagonal
    // of the bounding box) so ``slab=0`` = "full volume" stays
    // numerically sane: a half-slab of 1e6 mm with near=0.001 gave
    // the depth buffer a far/near ratio of 2e9 and the raymarcher
    // dropped precision until the MIP rendered solid black for
    // whole-body PET. With ``volExtent`` the ratio collapses to
    // ~10:1 (typical patient ~2 m diagonal, focal distance ~1 m)
    // and vtk's MIP integration recovers the expected silhouette.
    const volExtent = Math.sqrt(
      (activeVolume.dimensions[0] * activeVolume.spacing[0]) ** 2 +
        (activeVolume.dimensions[1] * activeVolume.spacing[1]) ** 2 +
        (activeVolume.dimensions[2] * activeVolume.spacing[2]) ** 2,
    );
    // Expose the full-slab cap to the slider so "max" really
    // covers the patient (whole-body PET ≈ 1700 mm cranio-caudal).
    setSlabMaxMm(Math.ceil(volExtent));
    const applySlab = () => {
      const vtkCam = vpAny.getVtkActiveCamera?.();
      if (!vtkCam) return;
      const csCam = vp.getCamera();
      const fp = csCam.focalPoint;
      const cp = csCam.position;
      let fd = 1000;
      if (fp && cp) {
        const dx = fp[0] - cp[0];
        const dy = fp[1] - cp[1];
        const dz = fp[2] - cp[2];
        fd = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1000;
      }
      // ``slabMm === 0`` ⇒ render the whole volume. We use the
      // volume diagonal as the half-width so every voxel along any
      // viewing direction is enclosed, while the depth-buffer
      // far/near ratio stays in a regime where vtk's MIP samples
      // remain accurate (the original 1e6 mm "huge" half blew out
      // the precision and produced the all-black symptom).
      const half = slabMmRef.current > 0 ? slabMmRef.current / 2 : volExtent;
      const near = Math.max(0.001, fd - half);
      const far = fd + half;
      try {
        vtkCam.setClippingRange(near, far);
      } catch {
        /* viewport tearing down */
      }
    };
    const origSetCamera = vpAny.setCamera.bind(vp);
    vpAny.setCamera = (props: unknown) => {
      origSetCamera(props);
      applySlab();
      vp.render();
    };
    vpAny.setCameraClippingRange = applySlab;
    vpAny._bvp_slab_apply = applySlab;

    // Apply a window/level pair to the live OTF/CTF without
    // re-uploading the volume. Called from the W/L slider effect
    // below. We rebuild the same two-point ramp the volume callback
    // installed at setup, so PET and CT keep their direction
    // (PT = inverse grayscale on white; CT/MR = grayscale on black).
    const applyWindow = (lo2: number, hi2: number) => {
      const eng = engineRef.current;
      if (!eng) return;
      const v = eng.getViewport(vpId) as cs.Types.IVolumeViewport | undefined;
      if (!v) return;
      const actor = (
        v as unknown as {
          getDefaultActor?: () => { actor?: { getProperty?: () => unknown } } | undefined;
        }
      ).getDefaultActor?.();
      const property = (
        actor?.actor as unknown as {
          getProperty?: () => {
            getScalarOpacity: (i: number) => unknown;
            getRGBTransferFunction: (i: number) => unknown;
          };
        }
      )?.getProperty?.();
      if (!property) return;
      const ofun2 = property.getScalarOpacity(0) as {
        addPoint: (x: number, y: number) => void;
        removeAllPoints: () => void;
      };
      const ctf2 = property.getRGBTransferFunction(0) as {
        addRGBPoint: (x: number, r: number, g: number, b: number) => void;
        removeAllPoints: () => void;
      };
      const span2 = hi2 - lo2 || 1;
      const eps2 = span2 * 0.005;
      ofun2.removeAllPoints();
      ctf2.removeAllPoints();
      if (activeIsPet) {
        ofun2.addPoint(lo2 - eps2, 0);
        ofun2.addPoint(lo2, 1);
        ofun2.addPoint(hi2, 1);
        ctf2.addRGBPoint(lo2, 1, 1, 1);
        ctf2.addRGBPoint(hi2, 0, 0, 0);
      } else {
        ofun2.addPoint(lo2 - eps2, 0);
        ofun2.addPoint(lo2, 1);
        ofun2.addPoint(hi2, 1);
        ctf2.addRGBPoint(lo2, 0, 0, 0);
        ctf2.addRGBPoint(hi2, 1, 1, 1);
      }
      v.render();
    };
    vpAny._bvp_window_apply = applyWindow;

    vp.resetCamera();
    try {
      vp.setCamera({
        viewPlaneNormal: [0, -1, 0] as cs.Types.Point3,
        viewUp: [0, 0, 1] as cs.Types.Point3,
      });
      vp.resetCamera();
    } catch {
      /* setCamera on a not-yet-bound viewport is a no-op; resetCamera
         leaves the default and the user can rotate to anterior */
    }
    applySlab();
    engine.render();

    // Layout-settle resize. ``enableElement`` above had to derive the
    // offscreen canvas dimensions from the container's bounding rect
    // at call time; on first mount the grid is sometimes still being
    // laid out (the MIP pane just appeared, the MPR cells might not
    // have had time to recompute their share of the row), and the
    // rect could be 0×0 — in which case ``getOrCreateCanvas`` falls
    // back to the 512×512 default. The MPR panes then keep painting
    // to the OLD region of the offscreen canvas while the on-screen
    // canvases pull from the NEW region (because ``_resize`` did
    // shift their sx/sy), and the MIP renders to a 512×512 region
    // inside what is now an 800×800 visible div — leaving most of
    // the visible MIP area white and the MPR panes apparently black
    // or stuck on a stale frame. The user-visible workaround is
    // ``Reset zoom + pan`` (which calls ``vp.resetCamera`` and a
    // single render) or toggling MIP off+on (which redoes
    // ``enableElement`` after the layout has settled).
    //
    // Run one more ``engine.resize(true)`` on the next animation
    // frame, AFTER the browser has had a chance to lay out the new
    // grid. ``resize`` re-reads each viewport's current size,
    // updates the canvas backing buffers, recomputes offscreen
    // canvas region offsets, and re-renders every viewport in one
    // shot — fixing all three symptoms (MIP white, MPR black, MPR
    // showing only PT without CT) at the source.
    requestAnimationFrame(() => {
      const eng = engineRef.current;
      if (!eng || eng !== engine) return;
      const elNow = containerRef.current;
      if (!elNow || elNow.offsetWidth < 4 || elNow.offsetHeight < 4) return;
      try {
        eng.resize(true);
      } catch {
        /* engine torn down between frames */
      }
    });
  }, [
    activeSeriesId,
    activeVolume,
    activeIsPet,
    engineId,
    toolGroupId,
    vpId,
    volumeId,
    fusionPending,
  ]);

  // Constrained primary-drag rotation: spin the camera around the
  // patient's cranio-caudal axis through the volume centre. Pivot
  // is computed from the active volume's bounds rather than from
  // ``cam.focalPoint`` because the latter can drift after pan /
  // zoom interactions (a few mm off centre is enough for the user
  // to perceive 'rotates around an axis external to the volume').
  // Horizontal mouse movement maps to azimuth around Z; vertical
  // movement is ignored. The focal point is explicitly re-pinned
  // to the volume centre on every drag tick so the spin stays
  // visually centred for the rest of the session.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ROTATION_DEG_PER_PX = 0.4;
    let dragging = false;
    let lastX = 0;
    const volumeCenter = (): cs.Types.Point3 => {
      // ``activeVolume`` carries the dimensions + spacing we passed
      // into ``createLocalVolume`` (origin pinned at [0, 0, 0]); the
      // centre is half the world-space extent on each axis.
      const [nx, ny, nz] = activeVolume.dimensions;
      const [sx, sy, sz] = activeVolume.spacing;
      return [((nx - 1) * sx) / 2, ((ny - 1) * sy) / 2, ((nz - 1) * sz) / 2];
    };
    const onDown = (ev: MouseEvent) => {
      if (ev.button !== 0) return; // primary only
      dragging = true;
      lastX = ev.clientX;
      ev.preventDefault();
    };
    const onMove = (ev: MouseEvent) => {
      if (!dragging) return;
      const engine = engineRef.current;
      if (!engine) return;
      const vp = engine.getViewport(vpId) as cs.Types.IVolumeViewport | undefined;
      if (!vp) return;
      const dx = ev.clientX - lastX;
      lastX = ev.clientX;
      if (dx === 0) return;
      const angleRad = (dx * ROTATION_DEG_PER_PX * Math.PI) / 180;
      const cam = vp.getCamera();
      if (!cam.position) return;
      const pivot = volumeCenter();
      const cosA = Math.cos(angleRad);
      const sinA = Math.sin(angleRad);
      // Rotate (px, py) around (pivot.x, pivot.y) at constant z.
      // The Z axis of rotation passes through the volume centre,
      // so the orbit stays centred on the patient regardless of
      // any earlier pan / zoom drift.
      const px = cam.position[0] - pivot[0];
      const py = cam.position[1] - pivot[1];
      const newPos: cs.Types.Point3 = [
        pivot[0] + px * cosA - py * sinA,
        pivot[1] + px * sinA + py * cosA,
        cam.position[2],
      ];
      // viewPlaneNormal points camera-to-focal; rotate it the same
      // way so the projection stays consistent with the camera.
      const vpn = cam.viewPlaneNormal ?? [0, -1, 0];
      const newVpn: cs.Types.Point3 = [
        vpn[0] * cosA - vpn[1] * sinA,
        vpn[0] * sinA + vpn[1] * cosA,
        vpn[2],
      ];
      vp.setCamera({
        position: newPos,
        focalPoint: pivot,
        viewPlaneNormal: newVpn,
        viewUp: [0, 0, 1] as cs.Types.Point3,
      });
      vp.render();
    };
    const onUp = () => {
      dragging = false;
    };
    el.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      el.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [vpId, activeVolume]);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    setup().catch((e) => {
      if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
    });
    return () => {
      cancelled = true;
      if (animFrameRef.current !== null) {
        cancelAnimationFrame(animFrameRef.current);
        animFrameRef.current = null;
      }
      // Detach our viewport from the shared engine + tool group;
      // leave both alive so the MPR can keep using them.
      const engine = cs.getRenderingEngine(engineId);
      if (engine) {
        try {
          (engine as unknown as { disableElement: (id: string) => void }).disableElement(vpId);
        } catch {
          /* viewport already gone */
        }
      }
      const tg = csTools.ToolGroupManager.getToolGroup(toolGroupId);
      if (tg) {
        try {
          (
            tg as unknown as { removeViewports: (eid: string, vid: string) => void }
          ).removeViewports(engineId, vpId);
        } catch {
          /* removed already */
        }
      }
      engineRef.current = null;
    };
  }, [setup, engineId, toolGroupId, vpId]);

  // Keep the canvas aspect-correct on container resize. Guarded so
  // we never call ``engine.resize`` while the engine is missing or
  // the container has zero size: the latter happens during the
  // R/MIP toggle path because the grid cell hasn't laid out yet on
  // the first ``ResizeObserver`` fire, and ``engine.resize`` on a
  // 0x0 element parks the WebGL canvas at 0x0 permanently — the
  // subsequent layout settle isn't always picked up as a size
  // change, so the MIP viewport stays blank until next reload.
  useEffect(() => {
    if (typeof ResizeObserver === "undefined") return;
    const el = containerRef.current;
    if (!el) return;
    let raf = 0;
    const ro = new ResizeObserver(() => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const engine = engineRef.current;
        if (!engine) return;
        if (el.offsetWidth < 4 || el.offsetHeight < 4) return;
        try {
          engine.resize(true);
        } catch {
          /* engine torn down between frames */
        }
      });
    });
    ro.observe(el);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  // Auto-play orbit: rotate the camera around the volume's vertical
  // axis at ~24°/s (15 s per full turn). Pauses while the user
  // drags (TrackballRotate sets an internal grab state — we just
  // keep firing setCamera incrementally and they cancel each other
  // out gracefully).
  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    if (!autoplay) {
      if (animFrameRef.current !== null) {
        cancelAnimationFrame(animFrameRef.current);
        animFrameRef.current = null;
      }
      return;
    }
    let last = performance.now();
    const tick = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      const engine = engineRef.current;
      const vp = engine?.getViewport(vpId) as cs.Types.IVolumeViewport | undefined;
      if (vp) {
        // 24°/s around the patient's cranio-caudal axis through the
        // volume centre. Same pivot as the manual drag (volume-
        // centre, not ``cam.focalPoint``) so the auto cine and the
        // manual rotation produce visually identical orbits.
        const angleRad = ((24 * Math.PI) / 180) * dt;
        const cam = vp.getCamera();
        if (cam.position && cam.viewPlaneNormal) {
          const [nx, ny, nz] = volume.dimensions;
          const [sx, sy, sz] = volume.spacing;
          const pivot: cs.Types.Point3 = [
            ((nx - 1) * sx) / 2,
            ((ny - 1) * sy) / 2,
            ((nz - 1) * sz) / 2,
          ];
          const cosA = Math.cos(angleRad);
          const sinA = Math.sin(angleRad);
          const dx = cam.position[0] - pivot[0];
          const dy = cam.position[1] - pivot[1];
          const newPos: cs.Types.Point3 = [
            pivot[0] + dx * cosA - dy * sinA,
            pivot[1] + dx * sinA + dy * cosA,
            cam.position[2],
          ];
          const vpn = cam.viewPlaneNormal;
          const newVpn: cs.Types.Point3 = [
            vpn[0] * cosA - vpn[1] * sinA,
            vpn[0] * sinA + vpn[1] * cosA,
            vpn[2],
          ];
          vp.setCamera({
            position: newPos,
            focalPoint: pivot,
            viewPlaneNormal: newVpn,
            viewUp: [0, 0, 1] as cs.Types.Point3,
          });
          vp.render();
        }
      }
      animFrameRef.current = requestAnimationFrame(tick);
    };
    animFrameRef.current = requestAnimationFrame(tick);
    return () => {
      if (animFrameRef.current !== null) {
        cancelAnimationFrame(animFrameRef.current);
        animFrameRef.current = null;
      }
    };
  }, [autoplay, vpId]);

  // Slab slider sync. The heavy lifting (computing the focal-distance
  // centred clip range, surviving CS3D's auto-reset on every camera
  // change) lives in ``applySlab`` patched onto the viewport during
  // ``setup``. Here we only:
  //   1. mirror the latest slabMm into the ref the patched closure
  //      reads from, so subsequent camera-driven re-applies pick up
  //      the new value.
  //   2. invoke ``_bvp_slab_apply`` once for the slider tick itself
  //      (otherwise nothing fires until the next setCamera).
  //   3. render once.
  useEffect(() => {
    slabMmRef.current = slabMm;
    const engine = engineRef.current;
    if (!engine) return;
    const vp = engine.getViewport(vpId) as
      | (cs.Types.IVolumeViewport & { _bvp_slab_apply?: () => void })
      | undefined;
    if (!vp || !vp._bvp_slab_apply) return;
    vp._bvp_slab_apply();
    vp.render();
  }, [slabMm, vpId]);

  // W/L slider sync. Same pattern as the slab effect: we rely on
  // ``_bvp_window_apply`` patched onto the viewport during setup so
  // the call is cheap and doesn't trigger a fresh setVolumesForViewports.
  useEffect(() => {
    if (winLoHi === null) return;
    const engine = engineRef.current;
    if (!engine) return;
    const vp = engine.getViewport(vpId) as
      | (cs.Types.IVolumeViewport & {
          _bvp_window_apply?: (lo: number, hi: number) => void;
        })
      | undefined;
    if (!vp || !vp._bvp_window_apply) return;
    vp._bvp_window_apply(winLoHi[0], winLoHi[1]);
  }, [winLoHi, vpId]);

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: "100%",
        background: "#fff",
        overflow: "hidden",
      }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      <div
        style={{
          position: "absolute",
          top: 6,
          left: 6,
          background: "rgba(0,0,0,0.55)",
          color: "#e6ecf3",
          fontSize: "0.7rem",
          padding: "2px 6px",
          borderRadius: 3,
          fontFamily: "ui-monospace, monospace",
          letterSpacing: "0.04em",
          pointerEvents: "none",
        }}
      >
        MIP · GPU
      </div>
      {err && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#fca5a5",
            fontSize: "0.85rem",
            padding: "1rem",
            textAlign: "center",
            background: "rgba(0,0,0,0.55)",
          }}
        >
          {err}
        </div>
      )}
      {fusionPending && !err && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#475569",
            fontSize: "0.8rem",
            letterSpacing: "0.04em",
            background: "#fff",
          }}
        >
          loading fusion…
        </div>
      )}
      {(() => {
        // Resolve current W/L: user-driven override or the percentile
        // auto-range. ``displayScale`` rewrites the slider into SUV
        // units when a body-weight factor is available, so the
        // operator dials in numbers that match the report (PERCIST /
        // EANM SUV scale) instead of raw Bq/mL.
        const auto = autoRangeRef.current ?? [0, 1];
        const [curLo, curHi] = winLoHi ?? auto;
        const useSuv = activeIsPet && suvFactorBw != null && suvFactorBw > 0;
        const displayScale = useSuv && suvFactorBw ? suvFactorBw : 1;
        const displayUnit = useSuv ? "SUV" : activeIsPet ? "Bq/mL" : "HU";
        const sliderMin = activeIsPet ? 0 : auto[0] * displayScale;
        // Cap the slider at 3× the auto p99 to give the user room to
        // expand the window (suppress dim background) or shrink it
        // (saturate hot regions). 3× is enough for PET clinical SUV
        // (auto p99.5 typical 8-12, max useful ~30) without losing
        // resolution at the low end of the range.
        const sliderMax = Math.max(auto[1] * displayScale * 3, curHi * displayScale * 1.2);
        const lvlValue = ((curLo + curHi) / 2) * displayScale;
        const wndValue = (curHi - curLo) * displayScale;
        const setFromLvlWnd = (lvl: number, wnd: number) => {
          const lvlNative = lvl / displayScale;
          const wndNative = Math.max(1e-9, wnd / displayScale);
          setWinLoHi([lvlNative - wndNative / 2, lvlNative + wndNative / 2]);
        };
        return (
          <div
            style={{
              position: "absolute",
              bottom: 6,
              left: 6,
              right: 6,
              display: "flex",
              flexDirection: "column",
              gap: 4,
              background: "rgba(0,0,0,0.55)",
              padding: "4px 6px",
              borderRadius: 4,
            }}
          >
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <button
                type="button"
                onClick={() => setAutoplay((p) => !p)}
                style={{
                  background: "transparent",
                  color: "#e6ecf3",
                  border: "1px solid rgba(255,255,255,0.3)",
                  borderRadius: 3,
                  padding: "1px 8px",
                  fontSize: "0.72rem",
                  cursor: "pointer",
                  minWidth: 50,
                }}
              >
                {autoplay ? "Pause" : "Play"}
              </button>
              <label
                style={{
                  color: "#cbd5e1",
                  fontSize: "0.7rem",
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  flex: 1,
                }}
              >
                Slab
                <input
                  type="range"
                  min={0}
                  max={slabMaxMm}
                  step={1}
                  value={slabMm}
                  onChange={(e) => setSlabMm(Number(e.target.value))}
                  style={{ flex: 1 }}
                />
                <span style={{ minWidth: 56, textAlign: "right" }}>
                  {slabMm === 0 ? "full" : `${slabMm} mm`}
                </span>
              </label>
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <button
                type="button"
                onClick={() => setWinLoHi(null)}
                title="Reset window to auto-percentile"
                style={{
                  background: "transparent",
                  color: "#e6ecf3",
                  border: "1px solid rgba(255,255,255,0.3)",
                  borderRadius: 3,
                  padding: "1px 8px",
                  fontSize: "0.72rem",
                  cursor: "pointer",
                  minWidth: 50,
                }}
              >
                Auto W/L
              </button>
              <label
                style={{
                  color: "#cbd5e1",
                  fontSize: "0.7rem",
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  flex: 1,
                }}
                title="Window center (level)"
              >
                L
                <input
                  type="range"
                  min={sliderMin}
                  max={sliderMax}
                  step={(sliderMax - sliderMin) / 200 || 0.01}
                  value={lvlValue}
                  onChange={(e) => setFromLvlWnd(Number(e.target.value), wndValue)}
                  style={{ flex: 1 }}
                />
              </label>
              <label
                style={{
                  color: "#cbd5e1",
                  fontSize: "0.7rem",
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  flex: 1,
                }}
                title="Window width"
              >
                W
                <input
                  type="range"
                  min={(sliderMax - sliderMin) / 200 || 0.01}
                  max={(sliderMax - sliderMin) * 1.2 || 1}
                  step={(sliderMax - sliderMin) / 200 || 0.01}
                  value={wndValue}
                  onChange={(e) => setFromLvlWnd(lvlValue, Number(e.target.value))}
                  style={{ flex: 1 }}
                />
              </label>
              <span
                style={{
                  minWidth: 90,
                  textAlign: "right",
                  color: "#cbd5e1",
                  fontSize: "0.65rem",
                  fontFamily: "ui-monospace, monospace",
                }}
              >
                {lvlValue.toFixed(useSuv ? 1 : 0)} / {wndValue.toFixed(useSuv ? 1 : 0)}{" "}
                {displayUnit}
              </span>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
