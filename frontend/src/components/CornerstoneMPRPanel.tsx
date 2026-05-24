"use client";

// Spike: replace the hand-rolled MPR + crosshair plumbing with
// Cornerstone3D's volume viewport + ResliceCursor / CrosshairsTool.
// Wired to support two co-registered volumes (CT primary + PET
// overlay) via ``setVolumesForViewports``: the first volume becomes
// the grayscale base layer, the second is rendered as a translucent
// color overlay (the standard PET-CT fusion view in OHIF / RadiAnt
// / Syngo).
//
// Why this exists at all: replicating the OHIF crosshair example
// (https://www.cornerstonejs.org/live-examples/crosshairs) means
// either rebuilding ``vtkResliceCursorWidget`` ourselves or letting
// Cornerstone host the viewports. We chose the second after a
// trade-off review with the user; this component is the smoke-test
// before migrating the rest of MPRViewport.

import * as cs from "@cornerstonejs/core";
import * as csTools from "@cornerstonejs/tools";
import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE_URL, ApiError, fetchVolume, getStoredToken } from "@/lib/api";
import { ensureCornerstoneInit } from "@/lib/cornerstoneSetup";
import { useIsMobile } from "@/lib/useIsMobile";
import type { VolumeData } from "./VolumeViewer";

interface Props {
  /** Primary anatomic series id (typically CT). The grayscale base. */
  primarySeriesId: string;
  /** Optional fusion-overlay series id (typically PT). When set, the
   *  Cornerstone viewports load both volumes and render the second
   *  as a translucent color overlay using a hot colormap. */
  fusionSeriesId?: string | null;
  /** ``"axial" | "sagittal" | "coronal"`` — picks which orthogonal
   *  triple is shown. The component always shows all three (per
   *  Cornerstone's MPR convention); this prop is reserved for future
   *  single-pane variants. */
  defaultPlane?: "axial" | "sagittal" | "coronal";
  className?: string;
}

const RENDERING_ENGINE_ID = "bvp-mpr-engine";
const TOOL_GROUP_ID = "bvp-mpr-toolgroup";
const VOLUME_PRIMARY_PREFIX = "bvp-volume-primary:";
const VOLUME_FUSION_PREFIX = "bvp-volume-fusion:";
const VIEWPORT_AXIAL = "bvp-mpr-axial";
const VIEWPORT_SAG = "bvp-mpr-sagittal";
const VIEWPORT_COR = "bvp-mpr-coronal";

/** Build a Cornerstone ``LocalVolume`` from the float32 volume the
 *  backend serves at ``/api/series/{id}/volume.raw`` (32-byte header
 *  + Float32 scalars). Wraps ``createLocalVolume`` so the volume id
 *  is stable across re-mounts and the cache reuses the same buffer
 *  on the second visit to the same study. */
function makeVolumeFromVolumeData(volumeId: string, data: VolumeData): cs.Types.IImageVolume {
  // Identity orientation. Our ``volume.raw`` is sampled along the
  // canonical i/j/k axes; co-registration with the fusion volume
  // assumes both come from the same frame of reference (same scanner
  // bed, same coordinate system) — which is true for PET/CT combo
  // scanners. For unrelated series the user should not enable fusion.
  const direction: cs.Types.Mat3 = [1, 0, 0, 0, 1, 0, 0, 0, 1];
  const origin: cs.Types.Point3 = [0, 0, 0];
  const dimensions: cs.Types.Point3 = [data.dimensions[0], data.dimensions[1], data.dimensions[2]];
  const spacing: cs.Types.Point3 = [data.spacing[0], data.spacing[1], data.spacing[2]];
  return cs.volumeLoader.createLocalVolume(volumeId, {
    metadata: {
      BitsAllocated: 32,
      BitsStored: 32,
      SamplesPerPixel: 1,
      HighBit: 31,
      PhotometricInterpretation: "MONOCHROME2",
      PixelRepresentation: 0,
      Modality: "OT",
      ImagePositionPatient: origin,
      ImageOrientationPatient: [1, 0, 0, 0, 1, 0],
      PixelSpacing: [data.spacing[0], data.spacing[1]],
      Columns: data.dimensions[0],
      Rows: data.dimensions[1],
      FrameOfReferenceUID: volumeId,
      voiLut: [{ windowCenter: 0, windowWidth: 1 }],
      VOILUTFunction: "LINEAR",
    } as unknown as cs.Types.Metadata,
    dimensions,
    spacing,
    origin,
    direction,
    scalarData: data.scalars,
  });
}

export default function CornerstoneMPRPanel({ primarySeriesId, fusionSeriesId, className }: Props) {
  const axialRef = useRef<HTMLDivElement | null>(null);
  const sagRef = useRef<HTMLDivElement | null>(null);
  const corRef = useRef<HTMLDivElement | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Mobile: drop the SAG/COR/info quadrants and keep only the axial
  // viewport, full-bleed. Cornerstone tolerates a tool group bound to
  // a single viewport (the crosshair widget degrades into a no-op when
  // there's nothing to translate to), so we just don't add the other
  // viewports to the engine in the first place.
  const isMobile = useIsMobile();
  // Capture the current active draggable tool in the toolgroup so the
  // sidebar / parent can reflect it. For the spike we hardcode
  // crosshair as the primary left-mouse tool.

  const setupOnce = useCallback(async () => {
    await ensureCornerstoneInit();

    // 1. Fetch volumes (primary + optional fusion).
    const primary = await fetchVolume(primarySeriesId);
    const fusion = fusionSeriesId ? await fetchVolume(fusionSeriesId) : null;

    const primaryVolumeId = `${VOLUME_PRIMARY_PREFIX}${primarySeriesId}`;
    const fusionVolumeId = fusion ? `${VOLUME_FUSION_PREFIX}${fusionSeriesId}` : null;

    // 2. Cache / register both volumes. ``createLocalVolume`` caches
    //    by volumeId; calling it twice for the same id reuses the
    //    cached entry, so HMR re-mounts don't re-allocate.
    if (!cs.cache.getVolume(primaryVolumeId)) {
      makeVolumeFromVolumeData(primaryVolumeId, {
        dimensions: [primary.header.nx, primary.header.ny, primary.header.nz],
        spacing: primary.header.spacing,
        scalars: primary.scalars,
        range: primary.header.valueRange,
      });
    }
    if (fusion && fusionVolumeId && !cs.cache.getVolume(fusionVolumeId)) {
      makeVolumeFromVolumeData(fusionVolumeId, {
        dimensions: [fusion.header.nx, fusion.header.ny, fusion.header.nz],
        spacing: fusion.header.spacing,
        scalars: fusion.scalars,
        range: fusion.header.valueRange,
      });
    }

    // 3. Rendering engine + viewports. We always create three
    //    ORTHOGRAPHIC viewports for the standard MPR triple. The
    //    refs must be populated before this fires (React mounts the
    //    elements before useEffect runs).
    const ax = axialRef.current;
    const sg = sagRef.current;
    const co = corRef.current;
    // On mobile we only mount the axial element; sg/co refs stay null
    // and we skip those viewports entirely. Without this guard the
    // engine throws on the missing element references and the user
    // sees the "viewport refs not mounted" error.
    if (!ax) throw new Error("axial viewport ref not mounted");
    const mobileMode = !sg || !co;
    if (!mobileMode && (!sg || !co)) throw new Error("viewport refs not mounted");

    let engine = cs.getRenderingEngine(RENDERING_ENGINE_ID);
    if (!engine) engine = new cs.RenderingEngine(RENDERING_ENGINE_ID);
    const viewportInputs: cs.Types.PublicViewportInput[] = [
      {
        viewportId: VIEWPORT_AXIAL,
        type: cs.Enums.ViewportType.ORTHOGRAPHIC,
        element: ax,
        defaultOptions: {
          orientation: cs.Enums.OrientationAxis.AXIAL,
          background: [0, 0, 0],
        },
      },
    ];
    if (sg && co) {
      viewportInputs.push(
        {
          viewportId: VIEWPORT_SAG,
          type: cs.Enums.ViewportType.ORTHOGRAPHIC,
          element: sg,
          defaultOptions: {
            orientation: cs.Enums.OrientationAxis.SAGITTAL,
            background: [0, 0, 0],
          },
        },
        {
          viewportId: VIEWPORT_COR,
          type: cs.Enums.ViewportType.ORTHOGRAPHIC,
          element: co,
          defaultOptions: {
            orientation: cs.Enums.OrientationAxis.CORONAL,
            background: [0, 0, 0],
          },
        },
      );
    }
    engine.setViewports(viewportInputs);

    // 4. Bind the volumes to all three viewports. Cornerstone takes
    //    an array — first entry is the base, subsequent entries are
    //    rendered as overlays via VTK's multi-volume actor pipeline.
    //    We pass a callback to set the colormap on the fusion layer.
    const volumeInputs: cs.Types.IVolumeInput[] = [{ volumeId: primaryVolumeId }];
    if (fusionVolumeId) {
      volumeInputs.push({
        volumeId: fusionVolumeId,
        callback: ({ volumeActor }) => {
          // PET-Hot colormap on the overlay; opacity ramped so
          // background fades and lesions glow. Mirrors the OHIF
          // PET-CT fusion preset.
          const ofun = volumeActor.getProperty().getScalarOpacity(0) as unknown as {
            addPoint: (x: number, y: number) => void;
            removeAllPoints: () => void;
          };
          ofun.removeAllPoints();
          ofun.addPoint(0, 0);
          ofun.addPoint(0.65, 0);
          ofun.addPoint(0.66, 0.4);
          ofun.addPoint(1, 0.85);
          const ctf = volumeActor.getProperty().getRGBTransferFunction(0) as unknown as {
            addRGBPoint: (x: number, r: number, g: number, b: number) => void;
            removeAllPoints: () => void;
          };
          ctf.removeAllPoints();
          ctf.addRGBPoint(0, 0, 0, 0);
          ctf.addRGBPoint(0.5, 0.4, 0, 0);
          ctf.addRGBPoint(0.7, 0.95, 0.35, 0);
          ctf.addRGBPoint(0.85, 1, 0.85, 0.1);
          ctf.addRGBPoint(1, 1, 1, 0.95);
        },
      });
    }
    const activeViewports =
      sg && co ? [VIEWPORT_AXIAL, VIEWPORT_SAG, VIEWPORT_COR] : [VIEWPORT_AXIAL];
    await cs.setVolumesForViewports(engine, volumeInputs, activeViewports);

    // 5. Tool group — bind CrosshairsTool + WL + Pan + Zoom + Scroll.
    //    The crosshair widget is what the user wanted from the OHIF
    //    example: drag lines to translate, drag T-handles to rotate
    //    (true oblique reformat under the hood).
    let tg = csTools.ToolGroupManager.getToolGroup(TOOL_GROUP_ID);
    if (!tg) tg = csTools.ToolGroupManager.createToolGroup(TOOL_GROUP_ID);
    if (!tg) throw new Error("failed to create tool group");
    for (const vp of activeViewports) {
      tg.addViewport(vp, RENDERING_ENGINE_ID);
    }
    tg.addTool(csTools.CrosshairsTool.toolName, {
      getReferenceLineColor: (viewportId: string) =>
        viewportId === VIEWPORT_AXIAL
          ? "rgb(68, 170, 255)"
          : viewportId === VIEWPORT_SAG
            ? "rgb(255, 68, 68)"
            : "rgb(68, 255, 68)",
    });
    tg.addTool(csTools.WindowLevelTool.toolName);
    tg.addTool(csTools.PanTool.toolName);
    tg.addTool(csTools.ZoomTool.toolName);
    tg.addTool(csTools.StackScrollTool.toolName);
    if (isMobile) {
      // Single-pane mobile: the crosshair widget has nothing to
      // translate to (the sag/cor viewports aren't mounted), so we
      // bind the primary pointer to Pan and let the user scroll
      // through slices via WindowLevel-on-touch-drag is too foreign on
      // a phone. Pan + pinch-zoom (handled by Cornerstone's touch
      // bindings) + slice scroll on touch swipe matches the OHIF
      // mobile preset.
      tg.setToolActive(csTools.PanTool.toolName, {
        bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
      });
      tg.setToolActive(csTools.StackScrollTool.toolName, {
        bindings: [{ mouseButton: csTools.Enums.MouseBindings.Wheel }],
      });
    } else {
      tg.setToolActive(csTools.CrosshairsTool.toolName, {
        bindings: [{ mouseButton: csTools.Enums.MouseBindings.Primary }],
      });
      tg.setToolActive(csTools.PanTool.toolName, {
        bindings: [{ mouseButton: csTools.Enums.MouseBindings.Auxiliary }],
      });
      tg.setToolActive(csTools.ZoomTool.toolName, {
        bindings: [{ mouseButton: csTools.Enums.MouseBindings.Secondary }],
      });
      tg.setToolActive(csTools.StackScrollTool.toolName, {
        bindings: [{ mouseButton: csTools.Enums.MouseBindings.Wheel }],
      });
    }

    engine.render();
  }, [primarySeriesId, fusionSeriesId, isMobile]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    setupOnce()
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof ApiError ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      // Tear down so HMR / route changes don't leak GPU resources.
      const engine = cs.getRenderingEngine(RENDERING_ENGINE_ID);
      try {
        engine?.destroy();
      } catch {
        /* engine already gone */
      }
      try {
        csTools.ToolGroupManager.destroyToolGroup(TOOL_GROUP_ID);
      } catch {
        /* not created or already destroyed */
      }
    };
  }, [setupOnce]);

  // Keep the canvas aspect-correct on container resize. Without
  // an explicit ``engine.resize()`` Cornerstone keeps the previous
  // canvas dimensions and the browser scales the bitmap to the new
  // CSS box, which deforms the image. Camera state is preserved.
  useEffect(() => {
    if (typeof ResizeObserver === "undefined") return;
    const targets: HTMLElement[] = [];
    for (const ref of [axialRef, sagRef, corRef]) {
      if (ref.current) targets.push(ref.current);
    }
    if (targets.length === 0) return;
    let raf = 0;
    const ro = new ResizeObserver(() => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        const engine = cs.getRenderingEngine(RENDERING_ENGINE_ID);
        if (!engine) return;
        try {
          engine.resize(true);
        } catch {
          /* engine torn down between frames */
        }
      });
    });
    for (const el of targets) ro.observe(el);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  // Suppress the unused-import warnings; ``API_BASE_URL`` /
  // ``getStoredToken`` are wired through ``fetchVolume`` for now but
  // we'll need them when we replace ``createLocalVolume`` with a
  // streaming loader (Phase 2).
  void API_BASE_URL;
  void getStoredToken;

  if (isMobile) {
    return (
      <div
        className={className}
        style={{
          position: "relative",
          width: "100%",
          height: "100%",
          background: "#000",
        }}
      >
        <div
          ref={axialRef}
          style={{ position: "absolute", inset: 0, background: "#000", touchAction: "none" }}
          onContextMenu={(e) => e.preventDefault()}
        />
        {(loading || err) && (
          <div
            style={{
              position: "absolute",
              top: 8,
              left: 8,
              right: 8,
              padding: "0.4rem 0.6rem",
              background: "rgba(15,20,30,0.85)",
              color: err ? "#fca5a5" : "#94a3b8",
              fontSize: "0.75rem",
              fontFamily: "ui-monospace, monospace",
              borderRadius: 4,
              pointerEvents: "none",
            }}
          >
            {loading ? "loading…" : err ? `ERROR: ${err}` : null}
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      className={className}
      style={{
        position: "relative",
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        gridTemplateRows: "1fr 1fr",
        gap: 4,
        width: "100%",
        height: "100%",
        background: "#000",
      }}
    >
      <div
        ref={axialRef}
        style={{ position: "relative", background: "#000" }}
        onContextMenu={(e) => e.preventDefault()}
      />
      <div
        ref={sagRef}
        style={{ position: "relative", background: "#000" }}
        onContextMenu={(e) => e.preventDefault()}
      />
      <div
        ref={corRef}
        style={{ position: "relative", background: "#000" }}
        onContextMenu={(e) => e.preventDefault()}
      />
      <div
        style={{
          position: "relative",
          background: "#0a0d14",
          color: "#94a3b8",
          fontSize: "0.7rem",
          padding: 8,
          fontFamily: "ui-monospace, monospace",
          whiteSpace: "pre-wrap",
        }}
      >
        Cornerstone3D MPR spike{"\n"}
        primary: {primarySeriesId.slice(0, 8)}
        {"\n"}
        fusion : {fusionSeriesId ? fusionSeriesId.slice(0, 8) : "(none)"}
        {"\n"}
        {loading ? "loading…" : err ? `ERROR: ${err}` : "ready"}
        {"\n"}
        {"\n"}
        Drag a line → translate{"\n"}
        Drag a T-mark → oblique rotate{"\n"}
        Right-drag → zoom{"\n"}
        Middle-drag → pan{"\n"}
        Wheel → slice scroll
      </div>
    </div>
  );
}
