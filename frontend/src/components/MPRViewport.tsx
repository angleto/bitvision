"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { useModal } from "@/components/ModalHost";
import { type DisplayMetadata, studiesApi } from "@/lib/api";
import {
  VIEWPORT_RESET_VIEW_EVENT,
  VIEWPORT_ZOOM_EVENT,
  type ViewportZoomDetail,
} from "@/lib/hotkeys";
import { useTouchGestures } from "@/lib/useTouchGestures";

import type { VolumeData } from "./VolumeViewer";

type Axis = "axial" | "sagittal" | "coronal";
type Tool =
  | "wl"
  | "measure-dist"
  | "measure-angle"
  | "measure-area"
  | "measure-ellipse"
  | "measure-freehand"
  | "measure-arrow"
  | "measure-text"
  | "measure-probe";
type Point = { x: number; y: number };
type Measurement = {
  id: number;
  tool: string;
  points: Point[];
  value: string;
  /** Axial slice (Z voxel index) at the time of placement. Used by
   * the slice-fade renderer to dim the marker on adjacent slices. */
  sliceIndex?: number;
  /** Cornerstone annotation UID, when the measurement originated in
   * the Cornerstone-backed layout. ``points`` (canvas coords) and
   * ``worldPoints`` (frame-of-reference coords) are both populated;
   * the legacy renderer reads ``points``, the Cornerstone layer
   * reads ``worldPoints`` to round-trip annotations across reloads. */
  markerId?: string;
  /** 3D world coordinates of each annotation handle (frame of
   * reference = volume's coordinate system). Authoritative source
   * of truth for measurements created in Cornerstone; legacy ones
   * leave this undefined. */
  worldPoints?: Array<[number, number, number]>;
  /** Cornerstone tool name (``Length``, ``EllipticalROI``, ...).
   * Distinct from the legacy ``tool`` field which uses ``measure-*``
   * names. Set when the measurement comes from Cornerstone. */
  csToolName?: string;
};

interface MarkerFadeConfig {
  enabled: boolean;
  range: number;
  opacity: number;
}

interface Props {
  volume: VolumeData;
  axis: Axis;
  sliceIndex: number;
  crosshair: [number, number, number];
  onCrosshairChange: (pos: [number, number, number]) => void;
  windowCenter: number;
  windowWidth: number;
  rotation?: number;
  flipH?: boolean;
  flipV?: boolean;
  invert?: boolean;
  onWindowChange?: (wc: number, ww: number) => void;
  activeTool?: string;
  showCrosshair?: boolean;
  showOverlay?: boolean;
  seriesDescription?: string;
  externalMeasurements?: Measurement[];
  onMeasurementsChange?: (measurements: Measurement[]) => void;
  /** Opaque series UUID. When provided, the viewport fetches per-series
   *  display hints (auto-invert for MONOCHROME1, non-square pixel spacing). */
  seriesId?: string;
  /** Slice-anchoring fade configuration. Sourced from the
   * ``viewer.marker.fade.*`` app_settings; the parent reads them and
   * passes them down. Defaults are the same as the seed values so the
   * component still works in isolation. */
  markerFade?: MarkerFadeConfig;
  /** Continuous-rotate callback fired by drag of the T-handles on
   * the crosshair lines. ``targetAxis`` is the orthogonal viewport
   * whose displayed slice should rotate; ``deltaDeg`` is the absolute
   * new rotation (not a delta), in degrees. The parent (MPRLayout)
   * propagates this into its ``orient[targetAxis].rotation`` state
   * so the corresponding viewport re-renders with the new angle.
   * Optional — when omitted, the T-handles aren't drawn. */
  onRotateOther?: (targetAxis: Axis, rotationDeg: number) => void;
  /** Current rotation (deg) of the OTHER two axes. Used by this
   * viewport's drag-rotate handler to seed ``rotStartDeg`` when a
   * handle drag begins, so the rotation accumulates from the
   * existing angle instead of snapping to zero. Maps axis → deg. */
  otherRotations?: Partial<Record<Axis, number>>;
}

export type { Measurement };

const AXIS_COLORS: Record<Axis, string> = {
  axial: "#44aaff",
  sagittal: "#ff4444",
  coronal: "#44ff44",
};
let _mid = 0;

export default function MPRViewport({
  volume,
  axis,
  sliceIndex,
  crosshair,
  onCrosshairChange,
  windowCenter,
  windowWidth,
  rotation = 0,
  flipH = false,
  flipV = false,
  invert = false,
  onWindowChange,
  activeTool = "wl",
  showCrosshair = true,
  showOverlay = true,
  seriesDescription,
  externalMeasurements,
  onMeasurementsChange,
  seriesId,
  markerFade = { enabled: true, range: 2, opacity: 0.5 },
  onRotateOther,
  otherRotations,
}: Props) {
  const modal = useModal();
  const tm = useTranslations("viewer");
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const sizeRef = useRef({ cw: 0, ch: 0 });
  const propsRef = useRef({ crosshair, windowCenter, windowWidth });
  propsRef.current = { crosshair, windowCenter, windowWidth };
  const [cursorVal, setCursorVal] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  // Per-series display hints: MONOCHROME1 auto-invert + pixel spacing.
  // Fetched once on mount; the render loop reads them via refs to avoid
  // an extra dependency cascade.
  const [displayMeta, setDisplayMeta] = useState<DisplayMetadata | null>(null);
  useEffect(() => {
    if (!seriesId) return;
    let cancelled = false;
    studiesApi
      .displayMetadata(seriesId)
      .then((m) => {
        if (!cancelled) setDisplayMeta(m);
      })
      .catch(() => {
        /* non-fatal — viewer falls back to raw render. */
      });
    return () => {
      cancelled = true;
    };
  }, [seriesId]);
  const [localMeasurements, setLocalMeasurements] = useState<Measurement[]>([]);
  const measurements = externalMeasurements ?? localMeasurements;
  const updateMeasurements = useCallback(
    (m: Measurement[]) => {
      setLocalMeasurements(m);
      onMeasurementsChange?.(m);
    },
    [onMeasurementsChange],
  );
  const [pendingPts, setPendingPts] = useState<Point[]>([]);
  const zoomRef = useRef(zoom);
  const panRef = useRef(pan);
  zoomRef.current = zoom;
  panRef.current = pan;

  const dragRef = useRef<{
    // ``crosshair-translate``: drag the dashed lines to slide the
    // crosshair voxel-by-voxel (Cornerstone-style continuous move
    // instead of click-and-jump). ``crosshair-rotate``: drag a
    // T-handle near a line's endpoint to rotate the *displayed* slice
    // of the orthogonal axis around the crosshair point. The latter
    // is a cosmetic 2D rotation, not a true oblique reformat — for
    // that, the dedicated Oblique viewport remains the right tool.
    mode: "wl" | "pan" | "measure" | "crosshair-translate" | "crosshair-rotate";
    startX: number;
    startY: number;
    startWC: number;
    startWW: number;
    startPanX: number;
    startPanY: number;
    dragged: boolean;
    /** For crosshair-translate: which line is being grabbed. ``"u"``
     *  for the vertical line (controls the U axis of this viewport),
     *  ``"v"`` for the horizontal line, ``"both"`` when grabbing the
     *  intersection. */
    crossLine?: "u" | "v" | "both";
    /** For crosshair-rotate: which other-axis viewport this handle
     *  rotates, plus the initial mouse angle and the axis's rotation
     *  at drag start. */
    rotTarget?: Axis;
    rotStartAngle?: number;
    rotStartDeg?: number;
  } | null>(null);

  const {
    dimensions: [nx, ny, nz],
    scalars,
    spacing,
    range,
  } = volume;
  const [sx, sy, sz] = spacing;

  const getAxisGeom = useCallback(() => {
    if (axis === "axial") return { w: nx, h: ny, spU: sx, spV: sy };
    if (axis === "sagittal") return { w: ny, h: nz, spU: sy, spV: sz };
    return { w: nx, h: nz, spU: sx, spV: sz };
  }, [axis, nx, ny, nz, sx, sy, sz]);

  const getVoxelAt = useCallback(
    (u: number, v: number): number => {
      const z = Math.min(sliceIndex, (axis === "axial" ? nz : axis === "sagittal" ? nx : ny) - 1);
      if (axis === "axial") return scalars[z * nx * ny + v * nx + u] ?? 0;
      if (axis === "sagittal") return scalars[v * nx * ny + u * nx + z] ?? 0;
      return scalars[v * nx * ny + z * nx + u] ?? 0;
    },
    [axis, sliceIndex, nx, ny, nz, scalars],
  );

  // Convert mouse position to voxel coordinates
  const mouseToVoxel = useCallback(
    (mx: number, my: number): { u: number; v: number } | null => {
      const { cw, ch } = sizeRef.current;
      const { w, h, spU, spV } = getAxisGeom();
      const physW = w * spU;
      const physH = h * spV;
      const fitScale = Math.min(cw / physW, ch / physH) * zoomRef.current;
      const pxPerU = fitScale * spU;
      const pxPerV = fitScale * spV;
      const drawW = ((physW * fitScale) / zoomRef.current) * zoomRef.current;
      const u = Math.round((mx - cw / 2 - panRef.current.x) / pxPerU + w / 2);
      const v = Math.round((my - ch / 2 - panRef.current.y) / pxPerV + h / 2);
      if (u < 0 || u >= w || v < 0 || v >= h) return null;
      return { u, v };
    },
    [getAxisGeom],
  );

  /** Forward: voxel (u,v) → canvas pixel (x,y). Used by mouse-down
   *  hit-testing of the crosshair lines and the T-handles. Mirrors
   *  the math in ``drawContent`` and ``mouseToVoxel``. Returns null
   *  only when the canvas isn't sized yet (pre-mount). */
  const voxelToMouse = useCallback(
    (u: number, v: number): { x: number; y: number } | null => {
      const { cw, ch } = sizeRef.current;
      if (cw < 1 || ch < 1) return null;
      const { w, h, spU, spV } = getAxisGeom();
      const physW = w * spU;
      const physH = h * spV;
      const fitScale = Math.min(cw / physW, ch / physH) * zoomRef.current;
      const pxPerU = fitScale * spU;
      const pxPerV = fitScale * spV;
      const x = cw / 2 + panRef.current.x + (u - w / 2) * pxPerU;
      const y = ch / 2 + panRef.current.y + (v - h / 2) * pxPerV;
      return { x, y };
    },
    [getAxisGeom],
  );

  // --- Draw ---
  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  const drawContent = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { cw, ch } = sizeRef.current;
    if (cw < 1 || ch < 1) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const { w, h, spU, spV } = getAxisGeom();
    const z = Math.min(sliceIndex, (axis === "axial" ? nz : axis === "sagittal" ? nx : ny) - 1);

    const imgData = ctx.createImageData(w, h);
    const wl = windowWidth || 1;
    const lo = windowCenter - wl / 2;
    const hi = windowCenter + wl / 2;
    for (let v = 0; v < h; v++) {
      for (let u = 0; u < w; u++) {
        let val: number;
        if (axis === "axial") val = scalars[z * nx * ny + v * nx + u];
        else if (axis === "sagittal") val = scalars[v * nx * ny + u * nx + z];
        else val = scalars[v * nx * ny + z * nx + u];
        const norm = Math.max(0, Math.min(255, ((val - lo) / (hi - lo)) * 255));
        const idx = (v * w + u) * 4;
        imgData.data[idx] = norm;
        imgData.data[idx + 1] = norm;
        imgData.data[idx + 2] = norm;
        imgData.data[idx + 3] = 255;
      }
    }

    const physW = w * spU;
    const physH = h * spV;
    const fitScale = Math.min(cw / physW, ch / physH) * zoom;
    const drawW = physW * fitScale;
    const drawH = physH * fitScale;

    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, cw, ch);

    const tmp = document.createElement("canvas");
    tmp.width = w;
    tmp.height = h;
    tmp.getContext("2d")?.putImageData(imgData, 0, 0);

    // XOR with the manual toggle: a user asking to invert a MONOCHROME1
    // series should see the *original* negative, not a double-inversion.
    const effectiveInvert = (displayMeta?.invert === true) !== invert;

    ctx.save();
    ctx.translate(cw / 2 + pan.x, ch / 2 + pan.y);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.scale(flipH ? -1 : 1, flipV ? -1 : 1);
    if (effectiveInvert) ctx.filter = "invert(1)";
    ctx.drawImage(tmp, -drawW / 2, -drawH / 2, drawW, drawH);
    ctx.filter = "none";
    ctx.restore();

    // Crosshairs (with optional rotation T-handles for continuous
    // drag-rotate, Cornerstone-style).
    if (showCrosshair) {
      let crossU: number;
      let crossV: number;
      if (axis === "axial") {
        crossU = crosshair[0];
        crossV = crosshair[1];
      } else if (axis === "sagittal") {
        crossU = crosshair[1];
        crossV = crosshair[2];
      } else {
        crossU = crosshair[0];
        crossV = crosshair[2];
      }
      const pxPerU = fitScale * spU;
      const pxPerV = fitScale * spV;
      const cuX = cw / 2 + pan.x + (crossU - w / 2) * pxPerU;
      const cuY = ch / 2 + pan.y + (crossV - h / 2) * pxPerV;
      const otherAxes = (["axial", "sagittal", "coronal"] as Axis[]).filter((a) => a !== axis);
      const colorU = AXIS_COLORS[otherAxes[0]]; // vertical line / sagittal-or-coronal
      const colorV = AXIS_COLORS[otherAxes[1]]; // horizontal line / coronal-or-sagittal

      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      // Vertical line (corresponds to other-axis ``otherAxes[0]``)
      ctx.strokeStyle = colorU;
      ctx.beginPath();
      ctx.moveTo(cuX, 0);
      ctx.lineTo(cuX, ch);
      ctx.stroke();
      // Horizontal line (corresponds to other-axis ``otherAxes[1]``)
      ctx.strokeStyle = colorV;
      ctx.beginPath();
      ctx.moveTo(0, cuY);
      ctx.lineTo(cw, cuY);
      ctx.stroke();
      ctx.setLineDash([]);

      // T-handles at line endpoints. Drawing them only when a
      // rotation callback is wired so non-rotatable layouts (e.g.
      // single panel) don't show floating squares. Each handle is a
      // small filled circle 12 px from the canvas edge — far enough
      // from the dashed line to be a discrete grab target.
      if (onRotateOther) {
        const HANDLE_OFFSET = 16;
        const HANDLE_R = 5;
        const drawHandle = (x: number, y: number, color: string) => {
          ctx.beginPath();
          ctx.arc(x, y, HANDLE_R, 0, Math.PI * 2);
          ctx.fillStyle = color;
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 1.5;
          ctx.fill();
          ctx.stroke();
        };
        // Vertical-line handles (top + bottom): rotating the vertical
        // line inside this viewport rotates the orthogonal axis whose
        // line is colored ``colorU``.
        drawHandle(cuX, HANDLE_OFFSET, colorU);
        drawHandle(cuX, ch - HANDLE_OFFSET, colorU);
        // Horizontal-line handles (left + right).
        drawHandle(HANDLE_OFFSET, cuY, colorV);
        drawHandle(cw - HANDLE_OFFSET, cuY, colorV);
      }
    }

    // Measurements (slice-anchored: 2D markers are tied to the axial
    // slice they were placed on; we render them at full opacity on
    // their slice, faded ±range slices, and hidden beyond. The draw
    // loop temporarily mutates ctx.globalAlpha — we restore it at the
    // end of the for-body so subsequent strokes are unaffected).
    const currentAxialZ = Math.round(propsRef.current.crosshair[2]);
    for (const m of measurements) {
      // Skip 2D measurements not on the active axial slice (when
      // sliceIndex is known). Sagittal / coronal viewports always
      // skip 2D markers — they're pinned to the axial frame.
      if (m.sliceIndex !== undefined) {
        if (axis !== "axial") {
          continue;
        }
        const delta = Math.abs(currentAxialZ - m.sliceIndex);
        if (markerFade.enabled) {
          if (delta === 0) {
            ctx.globalAlpha = 1.0;
          } else if (delta <= markerFade.range) {
            ctx.globalAlpha = markerFade.opacity;
          } else {
            continue;
          }
        } else if (delta !== 0) {
          continue;
        }
      } else {
        ctx.globalAlpha = 1.0;
      }
      ctx.lineWidth = 1.5;
      ctx.font = "11px monospace";
      if (m.tool === "dist" && m.points.length === 2) {
        ctx.strokeStyle = "#0f0";
        ctx.fillStyle = "#0f0";
        ctx.beginPath();
        ctx.moveTo(m.points[0].x, m.points[0].y);
        ctx.lineTo(m.points[1].x, m.points[1].y);
        ctx.stroke();
        for (const p of m.points) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillText(
          m.value,
          (m.points[0].x + m.points[1].x) / 2,
          (m.points[0].y + m.points[1].y) / 2 - 6,
        );
      } else if (m.tool === "angle" && m.points.length === 3) {
        ctx.strokeStyle = "#ff0";
        ctx.fillStyle = "#ff0";
        ctx.beginPath();
        ctx.moveTo(m.points[0].x, m.points[0].y);
        ctx.lineTo(m.points[1].x, m.points[1].y);
        ctx.lineTo(m.points[2].x, m.points[2].y);
        ctx.stroke();
        for (const p of m.points) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillText(m.value, m.points[1].x + 8, m.points[1].y - 6);
      } else if (m.tool === "area" && m.points.length >= 3) {
        ctx.strokeStyle = "#0cf";
        ctx.fillStyle = "rgba(0,200,255,0.15)";
        ctx.beginPath();
        ctx.moveTo(m.points[0].x, m.points[0].y);
        for (let i = 1; i < m.points.length; i++) ctx.lineTo(m.points[i].x, m.points[i].y);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#0cf";
        const cx = m.points.reduce((s, p) => s + p.x, 0) / m.points.length;
        const cy = m.points.reduce((s, p) => s + p.y, 0) / m.points.length;
        ctx.fillText(m.value, cx, cy);
      } else if (m.tool === "ellipse" && m.points.length === 2) {
        const cx = (m.points[0].x + m.points[1].x) / 2;
        const cy = (m.points[0].y + m.points[1].y) / 2;
        const rx = Math.abs(m.points[1].x - m.points[0].x) / 2;
        const ry = Math.abs(m.points[1].y - m.points[0].y) / 2;
        ctx.strokeStyle = "#f6c";
        ctx.fillStyle = "rgba(255,102,204,0.12)";
        ctx.beginPath();
        ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(cx - rx, cy);
        ctx.lineTo(cx + rx, cy);
        ctx.moveTo(cx, cy - ry);
        ctx.lineTo(cx, cy + ry);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#f6c";
        ctx.fillText(m.value, cx, cy + ry + 12);
      } else if (m.tool === "freehand" && m.points.length >= 2) {
        ctx.strokeStyle = "#fa0";
        ctx.fillStyle = "#fa0";
        ctx.beginPath();
        ctx.moveTo(m.points[0].x, m.points[0].y);
        for (let i = 1; i < m.points.length; i++) ctx.lineTo(m.points[i].x, m.points[i].y);
        ctx.stroke();
        const last = m.points[m.points.length - 1];
        ctx.fillText(m.value, last.x + 6, last.y - 6);
      } else if (m.tool === "arrow" && m.points.length === 2) {
        const a = m.points[0];
        const b = m.points[1];
        ctx.strokeStyle = "#f44";
        ctx.fillStyle = "#f44";
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
        const theta = Math.atan2(b.y - a.y, b.x - a.x);
        const ah = 8;
        ctx.beginPath();
        ctx.moveTo(b.x, b.y);
        ctx.lineTo(
          b.x - ah * Math.cos(theta - Math.PI / 7),
          b.y - ah * Math.sin(theta - Math.PI / 7),
        );
        ctx.lineTo(
          b.x - ah * Math.cos(theta + Math.PI / 7),
          b.y - ah * Math.sin(theta + Math.PI / 7),
        );
        ctx.closePath();
        ctx.fill();
        if (m.value) ctx.fillText(m.value, b.x + 8, b.y - 6);
      } else if (m.tool === "text" && m.points.length === 1) {
        ctx.fillStyle = "#fff";
        ctx.fillText(m.value, m.points[0].x, m.points[0].y);
      } else if (m.tool === "probe" && m.points.length === 1) {
        const p = m.points[0];
        ctx.strokeStyle = "#9ef";
        ctx.fillStyle = "#9ef";
        ctx.beginPath();
        ctx.moveTo(p.x - 6, p.y);
        ctx.lineTo(p.x + 6, p.y);
        ctx.moveTo(p.x, p.y - 6);
        ctx.lineTo(p.x, p.y + 6);
        ctx.stroke();
        ctx.fillText(m.value, p.x + 8, p.y - 8);
      }
    }
    // Reset the alpha mutated by the slice-fade branch above so
    // pending strokes / overlays render at full opacity.
    ctx.globalAlpha = 1.0;

    // Pending path (polygon / freehand)
    if (
      pendingPts.length > 1 &&
      (activeTool === "measure-area" ||
        activeTool === "measure-freehand" ||
        activeTool === "measure-angle")
    ) {
      ctx.strokeStyle = "#fff";
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pendingPts[0].x, pendingPts[0].y);
      for (let i = 1; i < pendingPts.length; i++) ctx.lineTo(pendingPts[i].x, pendingPts[i].y);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    for (const p of pendingPts) {
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
      ctx.fill();
    }

    // Overlay info
    if (showOverlay) {
      ctx.font = "11px monospace";
      ctx.fillStyle = AXIS_COLORS[axis];
      ctx.fillText(`${axis.toUpperCase()} #${sliceIndex + 1}`, 6, 14);
      ctx.fillStyle = "#aaa";
      ctx.fillText(`WC:${Math.round(windowCenter)} WW:${Math.round(windowWidth)}`, 6, 28);
      ctx.fillText(`Zoom:${Math.round(zoom * 100)}%`, 6, 42);
      if (seriesDescription) ctx.fillText(seriesDescription, 6, ch - 8);
      // Cursor value goes bottom-right (above the spacing line) so the
      // top-right corner stays free for the rotate/flip overlay buttons
      // that MPRLayout positions there. Painting in the same area made
      // the readout unreadable behind the button glyphs.
      if (cursorVal) {
        ctx.fillStyle = "#fff";
        ctx.fillText(cursorVal, cw - 180, ch - 22);
      }
      // Spacing info bottom-right
      ctx.fillStyle = "#666";
      ctx.fillText(`${w}×${h} px · ${spU.toFixed(2)}×${spV.toFixed(2)} mm`, cw - 180, ch - 8);
    }
  }, [
    volume,
    axis,
    sliceIndex,
    crosshair,
    windowCenter,
    windowWidth,
    rotation,
    flipH,
    flipV,
    invert,
    getAxisGeom,
    nx,
    ny,
    nz,
    scalars,
    zoom,
    pan,
    showCrosshair,
    showOverlay,
    measurements,
    pendingPts,
    cursorVal,
    seriesDescription,
    onRotateOther,
    sx,
    sy,
    sz,
    displayMeta,
    activeTool,
  ]);

  // Canvas sizing
  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    const el = containerRef.current;
    const canvas = canvasRef.current;
    if (!el || !canvas) return;
    const measure = () => {
      const cw = el.clientWidth;
      const ch = el.clientHeight;
      if (cw < 1 || ch < 1) return;
      canvas.width = cw * window.devicePixelRatio;
      canvas.height = ch * window.devicePixelRatio;
      sizeRef.current = { cw, ch };
      drawContent();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    drawContent();
  }, [drawContent]);

  // Wheel: slice navigation + zoom (shift+wheel)
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.ctrlKey || e.metaKey) {
        // Zoom
        const factor = e.deltaY > 0 ? 0.9 : 1.1;
        setZoom((z) => Math.max(0.1, Math.min(10, z * factor)));
      } else {
        // Slice navigation
        const delta = e.deltaY > 0 ? 1 : -1;
        const c = propsRef.current.crosshair;
        const newPos: [number, number, number] = [c[0], c[1], c[2]];
        if (axis === "axial") newPos[2] = Math.max(0, Math.min(nz - 1, newPos[2] + delta));
        else if (axis === "sagittal") newPos[0] = Math.max(0, Math.min(nx - 1, newPos[0] + delta));
        else newPos[1] = Math.max(0, Math.min(ny - 1, newPos[1] + delta));
        onCrosshairChange(newPos);
      }
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, [axis, nx, ny, nz, onCrosshairChange]);

  // Hotkey-driven zoom / reset-view (see lib/hotkeys.ts).
  useEffect(() => {
    const onZoom = (e: Event) => {
      const detail = (e as CustomEvent<ViewportZoomDetail>).detail;
      if (!detail || !Number.isFinite(detail.factor)) return;
      setZoom((z) => {
        const next = Math.max(0.1, Math.min(10, z * detail.factor));
        return next === z ? z : next;
      });
    };
    const onReset = () => {
      setZoom((z) => (z === 1 ? z : 1));
      setPan((p) => (p.x === 0 && p.y === 0 ? p : { x: 0, y: 0 }));
    };
    window.addEventListener(VIEWPORT_ZOOM_EVENT, onZoom);
    window.addEventListener(VIEWPORT_RESET_VIEW_EVENT, onReset);
    return () => {
      window.removeEventListener(VIEWPORT_ZOOM_EVENT, onZoom);
      window.removeEventListener(VIEWPORT_RESET_VIEW_EVENT, onReset);
    };
  }, []);

  // Mouse: W/L drag (left) + pan (middle) + crosshair (click) + HU readout
  // biome-ignore lint/correctness/useExhaustiveDependencies: explanation in surrounding comments
  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button === 1) {
        e.preventDefault();
        dragRef.current = {
          mode: "pan",
          startX: e.clientX,
          startY: e.clientY,
          startWC: 0,
          startWW: 0,
          startPanX: pan.x,
          startPanY: pan.y,
          dragged: false,
        };
      } else if (e.button === 0) {
        // Crosshair drag intercept (only when the W/L tool is the
        // active mode — measurement tools take priority over the
        // crosshair gesture). Three hit zones, in order of priority:
        //   1. T-handle (≤10 px)  → start crosshair-rotate
        //   2. Crosshair line    (≤6 px)  → start crosshair-translate
        //   3. anywhere else     → fall through to existing behaviour
        if (activeTool === "wl" && showCrosshair) {
          const rect = containerRef.current?.getBoundingClientRect();
          if (rect) {
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;
            // Map the crosshair voxel to canvas coords for this axis.
            let crossU: number;
            let crossV: number;
            if (axis === "axial") {
              crossU = crosshair[0];
              crossV = crosshair[1];
            } else if (axis === "sagittal") {
              crossU = crosshair[1];
              crossV = crosshair[2];
            } else {
              crossU = crosshair[0];
              crossV = crosshair[2];
            }
            const cross = voxelToMouse(crossU, crossV);
            if (cross) {
              const { cw, ch } = sizeRef.current;
              const HANDLE_OFFSET = 16;
              const HANDLE_GRAB = 10;
              const LINE_GRAB = 6;
              const otherAxes = (["axial", "sagittal", "coronal"] as Axis[]).filter(
                (a) => a !== axis,
              );
              // T-handles for the vertical line (rotates otherAxes[0]).
              const vHandles = [
                { x: cross.x, y: HANDLE_OFFSET },
                { x: cross.x, y: ch - HANDLE_OFFSET },
              ];
              const hHandles = [
                { x: HANDLE_OFFSET, y: cross.y },
                { x: cw - HANDLE_OFFSET, y: cross.y },
              ];
              const isNear = (a: { x: number; y: number }, r: number) =>
                Math.hypot(a.x - mx, a.y - my) <= r;

              if (onRotateOther) {
                const targetVert = otherAxes[0];
                const targetHoriz = otherAxes[1];
                const startDegFor = (a: Axis) => otherRotations?.[a] ?? 0;
                const angleFromCross = () =>
                  Math.atan2(my - cross.y, mx - cross.x) * (180 / Math.PI);

                if (vHandles.some((h) => isNear(h, HANDLE_GRAB))) {
                  e.preventDefault();
                  dragRef.current = {
                    mode: "crosshair-rotate",
                    startX: e.clientX,
                    startY: e.clientY,
                    startWC: 0,
                    startWW: 0,
                    startPanX: 0,
                    startPanY: 0,
                    dragged: false,
                    rotTarget: targetVert,
                    rotStartAngle: angleFromCross(),
                    rotStartDeg: startDegFor(targetVert),
                  };
                  return;
                }
                if (hHandles.some((h) => isNear(h, HANDLE_GRAB))) {
                  e.preventDefault();
                  dragRef.current = {
                    mode: "crosshair-rotate",
                    startX: e.clientX,
                    startY: e.clientY,
                    startWC: 0,
                    startWW: 0,
                    startPanX: 0,
                    startPanY: 0,
                    dragged: false,
                    rotTarget: targetHoriz,
                    rotStartAngle: angleFromCross(),
                    rotStartDeg: startDegFor(targetHoriz),
                  };
                  return;
                }
              }

              // Crosshair lines: a near-vertical or near-horizontal
              // hit kicks off a continuous translate. ``"both"`` when
              // the click lands on the intersection (≤ LINE_GRAB on
              // both lines simultaneously).
              const nearVLine = Math.abs(mx - cross.x) <= LINE_GRAB;
              const nearHLine = Math.abs(my - cross.y) <= LINE_GRAB;
              if (nearVLine || nearHLine) {
                e.preventDefault();
                dragRef.current = {
                  mode: "crosshair-translate",
                  startX: e.clientX,
                  startY: e.clientY,
                  startWC: 0,
                  startWW: 0,
                  startPanX: 0,
                  startPanY: 0,
                  dragged: false,
                  crossLine: nearVLine && nearHLine ? "both" : nearVLine ? "u" : "v",
                };
                return;
              }
            }
          }
        }
        if (activeTool === "wl") {
          dragRef.current = {
            mode: "wl",
            startX: e.clientX,
            startY: e.clientY,
            startWC: propsRef.current.windowCenter,
            startWW: propsRef.current.windowWidth,
            startPanX: 0,
            startPanY: 0,
            dragged: false,
          };
        } else {
          // Measurement click
          const rect = containerRef.current?.getBoundingClientRect();
          if (rect) {
            const pt = { x: e.clientX - rect.left, y: e.clientY - rect.top };

            // Freehand: start drag-to-trace session.
            if (activeTool === "measure-freehand") {
              dragRef.current = {
                mode: "measure",
                startX: e.clientX,
                startY: e.clientY,
                startWC: 0,
                startWW: 0,
                startPanX: 0,
                startPanY: 0,
                dragged: false,
              };
              setPendingPts([pt]);
              return;
            }

            // Single-click tools (probe / text) complete immediately.
            if (activeTool === "measure-probe") {
              const vox = mouseToVoxel(pt.x, pt.y);
              const val = vox ? getVoxelAt(vox.u, vox.v) : null;
              finishMeasurement("probe", [pt], val == null ? "—" : `${val.toFixed(0)} HU`);
              return;
            }
            if (activeTool === "measure-text") {
              // Async dialog; we kick it off and return — finishMeasurement
              // runs from the resolution callback so the surrounding
              // measurement state machine isn't blocked.
              modal
                .prompt({
                  title: tm("textAnnotationTitle"),
                  label: tm("textAnnotationLabel"),
                  defaultValue: tm("textAnnotationDefault"),
                })
                .then((txt) => {
                  if (txt?.trim()) finishMeasurement("text", [pt], txt.trim());
                });
              return;
            }

            const newPts = [...pendingPts, pt];
            if (activeTool === "measure-dist" && newPts.length === 2) {
              finishMeasurement("dist", newPts);
            } else if (activeTool === "measure-angle" && newPts.length === 3) {
              finishMeasurement("angle", newPts);
            } else if (activeTool === "measure-ellipse" && newPts.length === 2) {
              finishMeasurement("ellipse", newPts);
            } else if (activeTool === "measure-arrow" && newPts.length === 2) {
              finishMeasurement("arrow", newPts);
            } else {
              setPendingPts(newPts);
            }
          }
        }
      }
    },
    [activeTool, pan, pendingPts],
  );

  const finishMeasurement = useCallback(
    (
      tool: "dist" | "angle" | "area" | "ellipse" | "freehand" | "arrow" | "text" | "probe",
      pts: Point[],
      explicitValue?: string,
    ) => {
      const { spU, spV } = getAxisGeom();
      let value = explicitValue ?? "";
      if (tool === "dist") {
        const dx = (pts[1].x - pts[0].x) * spU;
        const dy = (pts[1].y - pts[0].y) * spV;
        value = `${Math.sqrt(dx * dx + dy * dy).toFixed(1)} mm`;
      } else if (tool === "angle") {
        const v1 = { x: pts[0].x - pts[1].x, y: pts[0].y - pts[1].y };
        const v2 = { x: pts[2].x - pts[1].x, y: pts[2].y - pts[1].y };
        const dot = v1.x * v2.x + v1.y * v2.y;
        const m1 = Math.sqrt(v1.x ** 2 + v1.y ** 2);
        const m2 = Math.sqrt(v2.x ** 2 + v2.y ** 2);
        value = `${((Math.acos(Math.max(-1, Math.min(1, dot / (m1 * m2)))) * 180) / Math.PI).toFixed(1)}°`;
      } else if (tool === "area") {
        let a = 0;
        for (let i = 0; i < pts.length; i++) {
          const j = (i + 1) % pts.length;
          a += pts[i].x * spU * (pts[j].y * spV);
          a -= pts[j].x * spU * (pts[i].y * spV);
        }
        value = `${Math.abs(a / 2).toFixed(1)} mm²`;
      } else if (tool === "ellipse") {
        const axX = Math.abs(pts[1].x - pts[0].x) * spU;
        const axY = Math.abs(pts[1].y - pts[0].y) * spV;
        const major = Math.max(axX, axY);
        const minor = Math.min(axX, axY);
        const area = Math.PI * (axX / 2) * (axY / 2);
        value = `${major.toFixed(1)}×${minor.toFixed(1)} mm · ${area.toFixed(1)} mm²`;
      } else if (tool === "freehand") {
        let perim = 0;
        for (let i = 1; i < pts.length; i++) {
          const dx = (pts[i].x - pts[i - 1].x) * spU;
          const dy = (pts[i].y - pts[i - 1].y) * spV;
          perim += Math.sqrt(dx * dx + dy * dy);
        }
        value = `${perim.toFixed(1)} mm`;
      } else if (tool === "arrow") {
        value = explicitValue ?? "";
      }
      // Capture the current axial slice index so the slice-fade
      // renderer (and the markers API sync upstream) knows which
      // slice this measurement was placed on. Only meaningful when
      // the user is reading on the axial plane; sagittal/coronal
      // viewports leave it undefined (the measurement won't fade).
      const sliceIndex = axis === "axial" ? Math.round(propsRef.current.crosshair[2]) : undefined;
      updateMeasurements([...measurements, { id: _mid++, tool, points: pts, value, sliceIndex }]);
      setPendingPts([]);
    },
    [axis, getAxisGeom, measurements, updateMeasurements],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      // HU readout
      const rect = containerRef.current?.getBoundingClientRect();
      if (rect) {
        const vox = mouseToVoxel(e.clientX - rect.left, e.clientY - rect.top);
        if (vox) {
          const val = getVoxelAt(vox.u, vox.v);
          setCursorVal(`Val: ${val.toFixed(1)}`);
        } else {
          setCursorVal(null);
        }
      }

      const d = dragRef.current;
      if (!d) return;
      const dx = e.clientX - d.startX;
      const dy = e.clientY - d.startY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) d.dragged = true;
      if (d.mode === "wl" && d.dragged) {
        onWindowChange?.(d.startWC + dy * 2, Math.max(1, d.startWW + dx * 4));
      } else if (d.mode === "pan") {
        setPan({ x: d.startPanX + dx, y: d.startPanY + dy });
      } else if (d.mode === "measure" && activeTool === "measure-freehand") {
        const r = containerRef.current?.getBoundingClientRect();
        if (!r) return;
        const next = { x: e.clientX - r.left, y: e.clientY - r.top };
        setPendingPts((prev) => {
          const last = prev[prev.length - 1];
          if (last && Math.hypot(next.x - last.x, next.y - last.y) < 2) return prev;
          return [...prev, next];
        });
      } else if (d.mode === "crosshair-translate") {
        // Continuously update the crosshair voxel based on the
        // current mouse position. ``"u"`` = vertical line drag (only
        // U component changes), ``"v"`` = horizontal line, ``"both"``
        // = grabbing the intersection (both components track).
        const r = containerRef.current?.getBoundingClientRect();
        if (!r) return;
        const vox = mouseToVoxel(e.clientX - r.left, e.clientY - r.top);
        if (!vox) return;
        const c = propsRef.current.crosshair;
        const newPos: [number, number, number] = [c[0], c[1], c[2]];
        const updateU = d.crossLine === "u" || d.crossLine === "both";
        const updateV = d.crossLine === "v" || d.crossLine === "both";
        if (axis === "axial") {
          if (updateU) newPos[0] = vox.u;
          if (updateV) newPos[1] = vox.v;
        } else if (axis === "sagittal") {
          if (updateU) newPos[1] = vox.u;
          if (updateV) newPos[2] = vox.v;
        } else {
          if (updateU) newPos[0] = vox.u;
          if (updateV) newPos[2] = vox.v;
        }
        onCrosshairChange(newPos);
      } else if (d.mode === "crosshair-rotate" && d.rotTarget && onRotateOther) {
        // Compute angle from cross center to current mouse position;
        // delta vs ``rotStartAngle`` is added to the rotation that
        // existed when the drag began. Pure 2D screen-space rotation
        // — display rotation only, see component-level docstring.
        const r = containerRef.current?.getBoundingClientRect();
        if (!r) return;
        const c = propsRef.current.crosshair;
        let crossU: number;
        let crossV: number;
        if (axis === "axial") {
          crossU = c[0];
          crossV = c[1];
        } else if (axis === "sagittal") {
          crossU = c[1];
          crossV = c[2];
        } else {
          crossU = c[0];
          crossV = c[2];
        }
        const cross = voxelToMouse(crossU, crossV);
        if (!cross) return;
        const mx = e.clientX - r.left;
        const my = e.clientY - r.top;
        const angleNow = Math.atan2(my - cross.y, mx - cross.x) * (180 / Math.PI);
        const delta = angleNow - (d.rotStartAngle ?? 0);
        const next = (d.rotStartDeg ?? 0) + delta;
        onRotateOther(d.rotTarget, next);
      }
    },
    [
      onWindowChange,
      mouseToVoxel,
      getVoxelAt,
      activeTool,
      axis,
      onCrosshairChange,
      voxelToMouse,
      onRotateOther,
    ],
  );

  const onDoubleClick = useCallback(() => {
    if (activeTool === "measure-area" && pendingPts.length >= 3) {
      finishMeasurement("area", pendingPts);
    } else if (activeTool === "measure-freehand" && pendingPts.length >= 2) {
      finishMeasurement("freehand", pendingPts);
    }
  }, [activeTool, pendingPts, finishMeasurement]);

  const onMouseUp = useCallback(
    (e: React.MouseEvent) => {
      const d = dragRef.current;
      dragRef.current = null;
      // Freehand: finalize on mouseup regardless of drag state.
      if (d?.mode === "measure" && activeTool === "measure-freehand") {
        if (pendingPts.length >= 2) finishMeasurement("freehand", pendingPts);
        else setPendingPts([]);
        return;
      }
      if (!d || d.dragged) return;
      if (d.mode !== "wl") return;
      // Click → crosshair
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const vox = mouseToVoxel(e.clientX - rect.left, e.clientY - rect.top);
      if (!vox) return;
      const c = propsRef.current.crosshair;
      const newPos: [number, number, number] = [c[0], c[1], c[2]];
      if (axis === "axial") {
        newPos[0] = vox.u;
        newPos[1] = vox.v;
      } else if (axis === "sagittal") {
        newPos[1] = vox.u;
        newPos[2] = vox.v;
      } else {
        newPos[0] = vox.u;
        newPos[2] = vox.v;
      }
      onCrosshairChange(newPos);
    },
    [axis, mouseToVoxel, onCrosshairChange, activeTool, pendingPts, finishMeasurement],
  );

  // Pointer-type guarded inside the hook (touch/pen only), so mouse handlers
  // above stay authoritative for desktop.
  const touchHandlers = useTouchGestures({
    enabled: true,
    onPinch: (factor) => {
      setZoom((z) => Math.max(0.1, Math.min(10, z * factor)));
    },
    onPan: (dx, dy) => {
      setPan((p) => ({ x: p.x + dx, y: p.y + dy }));
    },
    onDoubleTap: () => {
      setZoom(1);
      setPan({ x: 0, y: 0 });
    },
  });

  return (
    <div
      ref={containerRef}
      role="img"
      aria-label={`DICOM slice viewer (${axis} view)`}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onDoubleClick={onDoubleClick}
      onMouseLeave={() => {
        dragRef.current = null;
        setCursorVal(null);
      }}
      onPointerDown={touchHandlers.onPointerDown}
      onPointerMove={touchHandlers.onPointerMove}
      onPointerUp={touchHandlers.onPointerUp}
      onPointerCancel={touchHandlers.onPointerCancel}
      onContextMenu={(e) => e.preventDefault()}
      style={{
        width: "100%",
        height: "100%",
        background: "#000",
        cursor: activeTool === "wl" ? "crosshair" : "cell",
        position: "relative",
        overflow: "hidden",
        touchAction: "none",
        borderRight: `2px solid ${AXIS_COLORS[axis]}33`,
        borderBottom: `2px solid ${AXIS_COLORS[axis]}33`,
      }}
    >
      <canvas
        ref={canvasRef}
        style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%" }}
      />
    </div>
  );
}
