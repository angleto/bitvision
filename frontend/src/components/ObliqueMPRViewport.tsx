"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type Vec3,
  normalize,
  planeBasis,
  reformatSlice,
  rotateAround,
  volumeDiagonalMm,
} from "@/lib/obliquePlane";

import type { VolumeData } from "./VolumeViewer";

interface Props {
  volume: VolumeData;
  /** Initial plane origin (voxel coords). Read once on mount / volume change. */
  initialOrigin?: Vec3;
  /** Initial plane normal (voxel-axis space). Defaults to (1,1,1) normalized. */
  initialNormal?: Vec3;
  /** Initial in-plane "up" hint. Defaults to +Z. */
  initialUp?: Vec3;
  windowCenter: number;
  windowWidth: number;
  invert?: boolean;
  onWindowChange?: (wc: number, ww: number) => void;
  activeTool?: string;
  showOverlay?: boolean;
  seriesDescription?: string;
}

// Arbitrary-plane reformat viewport. The plane is defined by a normal + up
// vector in voxel space. Left-drag (wl tool) rotates the normal around the
// current "up" axis (horizontal drag) and around the "u" axis (vertical drag).
// Shift+drag adjusts W/L (so window/level is still reachable).
// Wheel pushes the origin along the normal (slab scrolling).
export default function ObliqueMPRViewport({
  volume,
  initialOrigin,
  initialNormal,
  initialUp,
  windowCenter,
  windowWidth,
  invert = false,
  onWindowChange,
  activeTool = "wl",
  showOverlay = true,
  seriesDescription,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const sizeRef = useRef({ cw: 0, ch: 0 });

  const {
    dimensions: [nx, ny, nz],
    scalars,
    spacing,
  } = volume;
  const [sx, sy, sz] = spacing;

  const [planeOrigin, setPlaneOrigin] = useState<Vec3>(
    () => initialOrigin ?? [nx / 2, ny / 2, nz / 2],
  );
  const [normal, setNormal] = useState<Vec3>(() => normalize(initialNormal ?? [1, 1, 1]));
  const [upHint, setUpHint] = useState<Vec3>(() => normalize(initialUp ?? [0, 0, 1]));
  const [zoom, setZoom] = useState(1);

  // Re-centre when the underlying volume changes (different series loaded).
  // The crosshair is intentionally NOT a dependency — user-driven slab scroll
  // must not be clobbered each time the parent re-renders.
  useEffect(() => {
    setPlaneOrigin([nx / 2, ny / 2, nz / 2]);
  }, [nx, ny, nz]);

  const basis = useMemo(() => planeBasis(normal, upHint), [normal, upHint]);

  // Output image size: pick a square canvas large enough to cover the volume
  // at the smallest voxel spacing, regardless of plane orientation.
  const diagMm = useMemo(() => volumeDiagonalMm([nx, ny, nz], spacing), [nx, ny, nz, spacing]);
  const outPixelSize = Math.min(sx, sy, sz); // mm per pixel (isotropic)
  const outSize = Math.max(64, Math.min(768, Math.ceil(diagMm / outPixelSize)));

  const dragRef = useRef<{
    mode: "rotate" | "wl";
    startX: number;
    startY: number;
    startNormal: Vec3;
    startUp: Vec3;
    startWC: number;
    startWW: number;
  } | null>(null);

  // --- Draw ---
  const drawContent = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { cw, ch } = sizeRef.current;
    if (cw < 1 || ch < 1) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, cw, ch);

    // Resample.
    const slice = reformatSlice({
      scalars,
      dims: [nx, ny, nz],
      spacing,
      origin: planeOrigin,
      u: basis.u,
      v: basis.v,
      outW: outSize,
      outH: outSize,
      outPixelSize,
    });

    // Window/level to 8-bit.
    const wl = windowWidth || 1;
    const lo = windowCenter - wl / 2;
    const hi = windowCenter + wl / 2;
    const imgData = ctx.createImageData(outSize, outSize);
    for (let i = 0; i < slice.length; i++) {
      const n = Math.max(0, Math.min(255, ((slice[i] - lo) / (hi - lo)) * 255));
      const idx = i * 4;
      imgData.data[idx] = n;
      imgData.data[idx + 1] = n;
      imgData.data[idx + 2] = n;
      imgData.data[idx + 3] = 255;
    }

    const tmp = document.createElement("canvas");
    tmp.width = outSize;
    tmp.height = outSize;
    tmp.getContext("2d")?.putImageData(imgData, 0, 0);

    // Fit to canvas.
    const physMm = outSize * outPixelSize;
    const fitScale = (Math.min(cw, ch) / physMm) * zoom;
    const drawSize = physMm * fitScale;

    ctx.save();
    ctx.translate(cw / 2, ch / 2);
    if (invert) ctx.filter = "invert(1)";
    ctx.drawImage(tmp, -drawSize / 2, -drawSize / 2, drawSize, drawSize);
    ctx.filter = "none";
    ctx.restore();

    // Normal indicator (small compass showing the current plane normal).
    if (showOverlay) {
      ctx.font = "11px monospace";
      ctx.fillStyle = "#c08bff";
      ctx.fillText("OBLIQUE", 6, 14);
      ctx.fillStyle = "#aaa";
      ctx.fillText(`WC:${Math.round(windowCenter)} WW:${Math.round(windowWidth)}`, 6, 28);
      ctx.fillText(
        `n=(${normal[0].toFixed(2)},${normal[1].toFixed(2)},${normal[2].toFixed(2)})`,
        6,
        42,
      );
      ctx.fillText(`Zoom:${Math.round(zoom * 100)}%`, 6, 56);
      ctx.fillStyle = "#666";
      ctx.fillText(`${outSize}×${outSize} px · ${outPixelSize.toFixed(2)} mm/px`, cw - 200, ch - 8);
      if (seriesDescription) ctx.fillText(seriesDescription, 6, ch - 8);

      // Gizmo: draw the in-plane axes at canvas centre.
      ctx.save();
      ctx.translate(cw / 2, ch / 2);
      const gz = Math.min(cw, ch) * 0.08;
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#c08bff";
      ctx.beginPath();
      ctx.moveTo(-gz, 0);
      ctx.lineTo(gz, 0);
      ctx.moveTo(0, -gz);
      ctx.lineTo(0, gz);
      ctx.stroke();
      ctx.restore();
    }
  }, [
    scalars,
    nx,
    ny,
    nz,
    spacing,
    planeOrigin,
    basis,
    outSize,
    outPixelSize,
    windowCenter,
    windowWidth,
    invert,
    zoom,
    showOverlay,
    seriesDescription,
    normal,
  ]);

  // Canvas sizing.
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

  // Wheel: slab scroll along normal (+ctrl/meta = zoom).
  // Use a ref so rotating the plane doesn't rebind the listener every frame.
  const normalRef = useRef(normal);
  normalRef.current = normal;
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.ctrlKey || e.metaKey) {
        const factor = e.deltaY > 0 ? 0.9 : 1.1;
        setZoom((z) => Math.max(0.1, Math.min(10, z * factor)));
        return;
      }
      const stepMm = e.deltaY > 0 ? 1 : -1;
      const n = normalRef.current;
      setPlaneOrigin((o) => [
        o[0] + (n[0] / sx) * stepMm,
        o[1] + (n[1] / sy) * stepMm,
        o[2] + (n[2] / sz) * stepMm,
      ]);
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, [sx, sy, sz]);

  // Mouse: drag-to-rotate. Horizontal drag rotates around `up`; vertical drag
  // rotates around `u` (in-plane right). Shift+drag adjusts W/L. Middle click
  // resets the normal to (1,1,1).
  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button === 1) {
        e.preventDefault();
        setNormal(normalize([1, 1, 1]));
        setUpHint([0, 0, 1]);
        return;
      }
      if (e.button !== 0) return;
      // Shift → W/L so users can still tune intensity without leaving this viewport.
      const mode: "wl" | "rotate" = e.shiftKey || activeTool !== "wl" ? "wl" : "rotate";
      dragRef.current = {
        mode,
        startX: e.clientX,
        startY: e.clientY,
        startNormal: normal,
        startUp: upHint,
        startWC: windowCenter,
        startWW: windowWidth,
      };
    },
    [activeTool, normal, upHint, windowCenter, windowWidth],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const dx = e.clientX - d.startX;
      const dy = e.clientY - d.startY;
      if (d.mode === "wl") {
        onWindowChange?.(d.startWC + dy * 2, Math.max(1, d.startWW + dx * 4));
        return;
      }
      // Rotate. Sensitivity: ~180° over 400px.
      const sens = Math.PI / 400;
      const { u: uAxis } = planeBasis(d.startNormal, d.startUp);
      // Yaw around up, pitch around the in-plane right axis.
      let n = rotateAround(d.startNormal, d.startUp, dx * sens);
      n = rotateAround(n, uAxis, dy * sens);
      setNormal(normalize(n));
    },
    [onWindowChange],
  );

  const onMouseUp = useCallback(() => {
    dragRef.current = null;
  }, []);

  return (
    <div
      ref={containerRef}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={() => {
        dragRef.current = null;
      }}
      onContextMenu={(e) => e.preventDefault()}
      style={{
        width: "100%",
        height: "100%",
        background: "#000",
        cursor: "grab",
        position: "relative",
        overflow: "hidden",
        touchAction: "none",
        borderRight: "2px solid #c08bff33",
        borderBottom: "2px solid #c08bff33",
      }}
    >
      <canvas
        ref={canvasRef}
        style={{ position: "absolute", top: 0, left: 0, width: "100%", height: "100%" }}
      />
    </div>
  );
}
