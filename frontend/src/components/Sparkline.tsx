"use client";

/**
 * Tiny dependency-free SVG sparkline for a lesion's measurement over time.
 * The coordinate math is a pure exported helper so it can be unit-tested.
 */

export interface SparkPoint {
  x: number;
  y: number;
}

/** Map a series of values to SVG points within (width x height), padded so
 * the stroke is not clipped. Non-finite values are dropped. */
export function sparklinePoints(
  values: number[],
  width: number,
  height: number,
  pad = 2,
): SparkPoint[] {
  const vals = values.filter((v) => Number.isFinite(v));
  if (vals.length === 0) return [];
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const innerH = Math.max(1, height - 2 * pad);
  const stepX = vals.length > 1 ? width / (vals.length - 1) : 0;
  return vals.map((v, i) => ({
    x: vals.length > 1 ? i * stepX : width / 2,
    y: height - pad - ((v - min) / range) * innerH,
  }));
}

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  stroke?: string;
  ariaLabel?: string;
}

export default function Sparkline({
  values,
  width = 110,
  height = 30,
  stroke = "var(--bv-accent, #e96b1f)",
  ariaLabel,
}: SparklineProps) {
  const pts = sparklinePoints(values, width, height);
  if (pts.length === 0) return null;
  const polyline = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel ?? "trend"}
      style={{ overflow: "visible", display: "block" }}
    >
      {pts.length > 1 && (
        <polyline
          points={polyline}
          fill="none"
          stroke={stroke}
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      )}
      <circle cx={last.x} cy={last.y} r={2.2} fill={stroke} />
    </svg>
  );
}
