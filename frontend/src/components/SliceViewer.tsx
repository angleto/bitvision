"use client";

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";

import { API_BASE_URL, getStoredToken } from "@/lib/api";

interface Props {
  seriesId: string;
  sliceCount: number;
  onIndexChange?: (index: number) => void;
  className?: string;
}

export interface SliceViewerHandle {
  index: number;
  zoom: number;
  invert: boolean;
  rotation: number;
  flipH: boolean;
  flipV: boolean;
  setIndex: (i: number) => void;
  setZoom: (z: number) => void;
  setInvert: (v: boolean) => void;
  setRotation: (r: number) => void;
  setFlipH: (v: boolean) => void;
  setFlipV: (v: boolean) => void;
  resetWL: () => void;
}

const SliceViewer = forwardRef<SliceViewerHandle, Props>(function SliceViewer(
  { seriesId, sliceCount, onIndexChange, className },
  ref,
) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [index, setIndexRaw] = useState(Math.floor(sliceCount / 2));
  const [wlDelta, setWlDelta] = useState({ wc: 0, ww: 0 });
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [invert, setInvert] = useState(false);
  const [rotation, setRotation] = useState(0);
  const [flipH, setFlipH] = useState(false);
  const [flipV, setFlipV] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const dragRef = useRef<{
    mode: "wl" | "pan";
    startX: number;
    startY: number;
    startWc: number;
    startWw: number;
    startPanX: number;
    startPanY: number;
  } | null>(null);

  const setIndex = useCallback(
    (i: number) => {
      setIndexRaw(i);
      onIndexChange?.(i);
    },
    [onIndexChange],
  );

  useImperativeHandle(ref, () => ({
    index,
    zoom,
    invert,
    rotation,
    flipH,
    flipV,
    setIndex,
    setZoom,
    setInvert,
    setRotation,
    setFlipH,
    setFlipV,
    resetWL: () => setWlDelta({ wc: 0, ww: 0 }),
  }));

  const fetchSlice = useCallback(
    async (idx: number): Promise<HTMLImageElement> => {
      const token = getStoredToken();
      const headers: Record<string, string> = {};
      if (token) headers.authorization = `Bearer ${token}`;

      const params = new URLSearchParams({ index: String(idx) });
      if (wlDelta.wc !== 0 || wlDelta.ww !== 0) {
        params.set("wc_delta", String(wlDelta.wc));
        params.set("ww_delta", String(wlDelta.ww));
      }

      const resp = await fetch(`${API_BASE_URL}/api/series/${seriesId}/thumbnail?${params}`, {
        credentials: "include",
        headers,
      });
      if (resp.status === 422) {
        throw new Error("no pixel data");
      }
      if (!resp.ok) throw new Error(`${resp.status}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
          URL.revokeObjectURL(url);
          resolve(img);
        };
        img.onerror = reject;
        img.src = url;
      });
    },
    [seriesId, wlDelta],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchSlice(index)
      .then((img) => {
        if (cancelled) return;
        imgRef.current = img;
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setLoading(false);
        if (e instanceof Error && e.message === "no pixel data") {
          setError("This series has no pixel data (structured report or key object).");
        } else {
          setError("Failed to load slice.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [index, fetchSlice]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  const drawCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img) return;
    const container = containerRef.current;
    if (!container) return;
    const { width, height } = container.getBoundingClientRect();
    const dpr = window.devicePixelRatio;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, width, height);

    const baseScale = Math.min(width / img.width, height / img.height) * 0.92;
    const s = baseScale * zoom;
    const dw = img.width * s;
    const dh = img.height * s;

    ctx.save();
    ctx.translate(width / 2 + pan.x, height / 2 + pan.y);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.scale(flipH ? -1 : 1, flipV ? -1 : 1);
    if (invert) ctx.filter = "invert(1)";
    ctx.drawImage(img, -dw / 2, -dh / 2, dw, dh);
    ctx.restore();
  }, [zoom, pan, invert, rotation, flipH, flipV, loading]);

  useEffect(() => {
    drawCanvas();
  }, [drawCanvas]);

  useEffect(() => {
    const onResize = () => drawCanvas();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [drawCanvas]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const delta = e.deltaY > 0 ? 1 : -1;
      setIndex(Math.max(0, Math.min(sliceCount - 1, index + delta)));
    };
    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
  }, [sliceCount, index, setIndex]);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button === 0) {
        dragRef.current = {
          mode: "wl",
          startX: e.clientX,
          startY: e.clientY,
          startWc: wlDelta.wc,
          startWw: wlDelta.ww,
          startPanX: pan.x,
          startPanY: pan.y,
        };
      } else if (e.button === 1) {
        e.preventDefault();
        dragRef.current = {
          mode: "pan",
          startX: e.clientX,
          startY: e.clientY,
          startWc: wlDelta.wc,
          startWw: wlDelta.ww,
          startPanX: pan.x,
          startPanY: pan.y,
        };
      }
    },
    [wlDelta, pan],
  );

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    if (!dragRef.current) return;
    const dx = e.clientX - dragRef.current.startX;
    const dy = e.clientY - dragRef.current.startY;
    if (dragRef.current.mode === "wl") {
      setWlDelta({
        wc: dragRef.current.startWc + Math.round(dy),
        ww: dragRef.current.startWw + Math.round(dx * 2),
      });
    } else {
      setPan({
        x: dragRef.current.startPanX + dx,
        y: dragRef.current.startPanY + dy,
      });
    }
  }, []);

  const onMouseUp = useCallback(() => {
    dragRef.current = null;
  }, []);

  if (error) {
    return (
      <div
        className={className}
        style={{
          width: "100%",
          height: "100%",
          background: "#000",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#888",
          fontSize: "0.9rem",
          textAlign: "center",
          padding: "2rem",
        }}
      >
        {error}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className={className}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={onMouseUp}
      onContextMenu={(e) => e.preventDefault()}
      style={{
        width: "100%",
        height: "100%",
        background: "#000",
        cursor: "crosshair",
        position: "relative",
        userSelect: "none",
        overflow: "hidden",
      }}
    >
      <canvas ref={canvasRef} style={{ display: "block" }} />

      <div className="slice-hud">
        <span>
          Slice {index + 1}/{sliceCount}
        </span>
        <span>
          WC{wlDelta.wc >= 0 ? "+" : ""}
          {wlDelta.wc} WW{wlDelta.ww >= 0 ? "+" : ""}
          {wlDelta.ww}
        </span>
        <span>Zoom {Math.round(zoom * 100)}%</span>
        {invert && <span>INV</span>}
      </div>

      {loading && (
        <div
          style={{
            position: "absolute",
            top: "50%",
            left: "50%",
            transform: "translate(-50%,-50%)",
            color: "#888",
            fontSize: "0.85rem",
          }}
        >
          loading…
        </div>
      )}
    </div>
  );
});

export default SliceViewer;
