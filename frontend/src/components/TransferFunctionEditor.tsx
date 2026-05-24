"use client";

// Inline transfer-function editor for the 3D viewer side panel.
//
// Shows a log-scaled histogram of the volume's non-zero scalars and
// overlays draggable opacity control points. Each point has a t in
// [0, 1] (position along the histogram) and an alpha in [0, 1]
// (opacity at that scalar value). Drag a point to retune; the parent
// receives the updated stops and threads them into the VolumeViewer
// as ``customOpacityStops``.
//
// Why log-scale histogram: medical volumes have a heavy zero/low-end
// peak (air, noise floor) that crushes a linear histogram into a
// single tall bar with everything else flat. Log compresses the
// dynamic range so the user can see where the diagnostic signal
// sits and place opacity stops there.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

interface Stop {
  t: number;
  alpha: number;
}

interface Props {
  scalars: Float32Array;
  /** Current stops; ``null`` means the parent is using the preset's
   *  built-in curve. We initialise the editor with the preset's
   *  default ramp on first interaction. */
  stops: Stop[] | null;
  onChange: (stops: Stop[]) => void;
  onReset: () => void;
  /** Editor pixel height. Width inherits from container. */
  height?: number;
}

const DEFAULT_STOPS: Stop[] = [
  { t: 0.0, alpha: 0.0 },
  { t: 0.5, alpha: 0.0 },
  { t: 0.7, alpha: 0.4 },
  { t: 0.9, alpha: 0.85 },
  { t: 1.0, alpha: 1.0 },
];

const POINT_RADIUS = 6;
const HIST_BINS = 128;

export default function TransferFunctionEditor({
  scalars,
  stops,
  onChange,
  onReset,
  height = 120,
}: Props) {
  const tv = useTranslations("viewer");
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [width, setWidth] = useState(280);
  const [draggingIndex, setDraggingIndex] = useState<number | null>(null);

  // Bin the histogram once per scalars input. Sampling at stride to
  // avoid stalling on huge volumes; 50k samples is enough for a
  // smooth distribution shape.
  const { bins, peakLog } = useMemo(() => {
    if (scalars.length === 0) return { bins: new Array(HIST_BINS).fill(0), peakLog: 1 };
    const N = scalars.length;
    const SAMPLE = Math.min(50000, N);
    const stride = Math.max(1, Math.floor(N / SAMPLE));
    let lo = Number.POSITIVE_INFINITY;
    let hi = Number.NEGATIVE_INFINITY;
    const samples: number[] = [];
    for (let i = 0; i < N; i += stride) {
      const v = scalars[i];
      if (Number.isFinite(v) && v !== 0) {
        samples.push(v);
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (samples.length === 0 || hi === lo) {
      return { bins: new Array(HIST_BINS).fill(0), peakLog: 1 };
    }
    // Robust upper bound: clip outliers via 99.5th percentile so a
    // single hot voxel doesn't crush the histogram into the bottom 1%.
    samples.sort((a, b) => a - b);
    const robustHi = samples[Math.min(samples.length - 1, Math.floor(samples.length * 0.995))];
    const robustLo = samples[Math.max(0, Math.floor(samples.length * 0.005))];
    const span = robustHi - robustLo || 1;
    const localBins = new Array(HIST_BINS).fill(0);
    for (const v of samples) {
      const t = Math.max(0, Math.min(1, (v - robustLo) / span));
      const b = Math.min(HIST_BINS - 1, Math.floor(t * HIST_BINS));
      localBins[b] += 1;
    }
    let peak = 0;
    for (const c of localBins) if (c > peak) peak = c;
    return { bins: localBins, peakLog: Math.log1p(peak) || 1 };
  }, [scalars]);

  // Resize observer so the SVG matches the parent's width.
  useEffect(() => {
    const el = svgRef.current?.parentElement;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      for (const ent of entries) {
        const w = ent.contentRect.width;
        if (w > 0) setWidth(Math.floor(w));
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const activeStops: Stop[] = stops ?? DEFAULT_STOPS;

  const tToX = useCallback(
    (t: number) => Math.max(POINT_RADIUS, Math.min(width - POINT_RADIUS, t * width)),
    [width],
  );
  const alphaToY = useCallback(
    (a: number) => Math.max(POINT_RADIUS, Math.min(height - POINT_RADIUS, (1 - a) * height)),
    [height],
  );
  const xToT = useCallback((x: number) => Math.max(0, Math.min(1, x / width)), [width]);
  const yToAlpha = useCallback((y: number) => Math.max(0, Math.min(1, 1 - y / height)), [height]);

  const handlePointerDown = (idx: number) => (e: React.PointerEvent) => {
    e.stopPropagation();
    setDraggingIndex(idx);
  };

  // Global pointermove / pointerup listeners while dragging. The
  // previous SVG-scoped handler missed events whenever the cursor
  // strayed off the SVG (which happens constantly when dragging
  // a point near the edge), so the curve appeared "frozen".
  // Hooking into the window guarantees we see every motion until
  // the button is released.
  useEffect(() => {
    if (draggingIndex === null) return;
    const onMove = (e: PointerEvent) => {
      const svg = svgRef.current;
      if (!svg) return;
      const rect = svg.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const next = [...activeStops];
      const prev = next[draggingIndex - 1];
      const after = next[draggingIndex + 1];
      let newT = xToT(x);
      // Monotonic-in-t guard so vtk's piecewise function stays valid.
      if (prev) newT = Math.max(newT, prev.t + 0.001);
      if (after) newT = Math.min(newT, after.t - 0.001);
      // Endpoints are pinned (only alpha is editable on first / last).
      if (draggingIndex === 0) newT = 0;
      if (draggingIndex === next.length - 1) newT = 1;
      next[draggingIndex] = { t: newT, alpha: yToAlpha(y) };
      onChange(next);
    };
    const onUp = () => setDraggingIndex(null);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [draggingIndex, activeStops, onChange, xToT, yToAlpha]);

  const handleDoubleClick = () => {
    onReset();
  };

  // Polyline points string for the OTF curve overlay.
  const linePoints = activeStops.map((s) => `${tToX(s.t)},${alphaToY(s.alpha)}`).join(" ");

  return (
    <div style={{ marginTop: "0.4rem" }}>
      <svg
        ref={svgRef}
        role="img"
        aria-label="Transfer function editor"
        width={width}
        height={height}
        onDoubleClick={handleDoubleClick}
        style={{
          width: "100%",
          background: "#0f1419",
          border: "1px solid #2a2f37",
          borderRadius: 4,
          touchAction: "none",
          cursor: draggingIndex !== null ? "grabbing" : "default",
        }}
      >
        {/* Histogram bars (log-scaled). */}
        {bins.map((c, i) => {
          const h = (Math.log1p(c) / peakLog) * (height - 4);
          const w = width / bins.length;
          return (
            <rect
              // Histogram bars: bin index IS the identity; the array is
              // a fixed-length pixel histogram rebuilt only when the
              // volume changes (which remounts the parent), so positional
              // key never causes a stale reorder.
              // biome-ignore lint/suspicious/noArrayIndexKey: bin index is the stable identity for a histogram.
              key={i}
              x={i * w}
              y={height - h}
              width={Math.max(1, w - 0.5)}
              height={h}
              fill="rgba(140,160,180,0.35)"
            />
          );
        })}
        {/* Filled area under the OTF curve, to show what's "visible". */}
        <polygon
          points={`0,${height} ${linePoints} ${width},${height}`}
          fill="rgba(233,107,31,0.18)"
        />
        {/* OTF curve. */}
        <polyline points={linePoints} fill="none" stroke="#e96b1f" strokeWidth={2} />
        {/* Draggable points. */}
        {activeStops.map((s, i) => (
          <circle
            // ``s.t`` is the position along the curve and is unique per
            // stop (the editor enforces strictly-increasing t-values).
            key={`stop-${s.t}`}
            cx={tToX(s.t)}
            cy={alphaToY(s.alpha)}
            r={POINT_RADIUS}
            fill="#fff"
            stroke="#e96b1f"
            strokeWidth={2}
            onPointerDown={handlePointerDown(i)}
            style={{ cursor: "grab" }}
          />
        ))}
      </svg>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: "0.65rem",
          color: "#94a3b8",
          marginTop: 2,
        }}
      >
        <span>{stops ? tv("vrTfCurveCustom") : tv("vrTfCurvePreset")}</span>
        <span>{tv("vrTfResetHint")}</span>
      </div>
    </div>
  );
}
