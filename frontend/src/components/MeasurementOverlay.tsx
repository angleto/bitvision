"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo, useRef, useState } from "react";

import { useModal } from "@/components/ModalHost";
import {
  type Measurement,
  type MeasurementKind,
  type Pt,
  appendPoint,
  createProbe,
  createText,
  ellipseFromBBox,
  finalize,
} from "@/lib/measurements";

// Legacy tool strings kept for backward compatibility with the toolbar rendered
// below. Extended with the new radiology-expected tools.
type Tool =
  | "none"
  | "distance"
  | "angle"
  | "area"
  | "ellipse"
  | "freehand"
  | "arrow"
  | "text"
  | "probe";

interface Props {
  spacingX: number;
  spacingY: number;
  /** Optional pixel sampler used by the probe tool to read a HU/intensity value. */
  sampleAt?: (x: number, y: number) => number | null;
  onSave?: (m: Measurement) => void;
}

const DEFAULT_COLOR: Record<MeasurementKind, string> = {
  distance: "#0f0",
  angle: "#ff0",
  area: "#0cf",
  ellipse: "#f6c",
  freehand: "#fa0",
  arrow: "#f44",
  text: "#fff",
  probe: "#9ef",
};

export default function MeasurementOverlay({ spacingX, spacingY, sampleAt, onSave }: Props) {
  const modal = useModal();
  const tm = useTranslations("viewer");
  const [tool, setTool] = useState<Tool>("none");
  const [pending, setPending] = useState<Pt[]>([]);
  const [freehandActive, setFreehandActive] = useState(false);
  const [measurements, setMeasurements] = useState<Measurement[]>([]);
  const svgRef = useRef<SVGSVGElement>(null);

  // Memoised spacing pair: hooks below reference ``sp`` directly, so
  // a stable identity keeps their dep arrays compact (only ``sp``
  // instead of both ``spacingX`` and ``spacingY``).
  const sp = useMemo(() => ({ spU: spacingX, spV: spacingY }), [spacingX, spacingY]);

  const toSvgCoords = useCallback((e: React.MouseEvent | React.PointerEvent): Pt => {
    const svg = svgRef.current;
    if (!svg) return [0, 0];
    const rect = svg.getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }, []);

  const commit = useCallback(
    (m: Measurement) => {
      setMeasurements((prev) => [...prev, m]);
      onSave?.(m);
    },
    [onSave],
  );

  const onClick = useCallback(
    (e: React.MouseEvent) => {
      if (tool === "none" || tool === "freehand") return;
      const p = toSvgCoords(e);
      const kind = tool as MeasurementKind;

      // Probe needs a sampled value at the moment of creation.
      if (kind === "probe") {
        const val = sampleAt?.(p[0], p[1]);
        commit(createProbe(p, val ?? undefined));
        setTool("none");
        setPending([]);
        return;
      }

      if (kind === "text") {
        modal
          .prompt({
            title: tm("textAnnotationTitle"),
            label: tm("textAnnotationLabel"),
            defaultValue: tm("textAnnotationDefault"),
          })
          .then((txt) => {
            if (txt?.trim()) commit(createText(p, txt.trim()));
          });
        setTool("none");
        setPending([]);
        return;
      }

      const step = appendPoint(kind, pending, p, sp);
      if (step.status === "complete") {
        commit(step.measurement);
        setPending([]);
        setTool("none");
      } else {
        setPending(step.points);
      }
    },
    [tool, pending, toSvgCoords, commit, sampleAt, sp, modal, tm],
  );

  const onDblClick = useCallback(() => {
    if (tool !== "area" && tool !== "freehand") return;
    const m = finalize(tool, pending, sp);
    if (m) commit(m);
    setPending([]);
    setFreehandActive(false);
    setTool("none");
  }, [tool, pending, commit, sp]);

  // --- Freehand: drag to draw a path ---
  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (tool !== "freehand") return;
      e.preventDefault();
      (e.currentTarget as SVGSVGElement).setPointerCapture(e.pointerId);
      setFreehandActive(true);
      setPending([toSvgCoords(e)]);
    },
    [tool, toSvgCoords],
  );
  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!freehandActive) return;
      const next = toSvgCoords(e);
      // Coalesce: skip samples closer than 2px to the previous point to avoid
      // pathologically long point arrays on high-DPI touchpads.
      setPending((prev) => {
        const last = prev[prev.length - 1];
        if (last && Math.hypot(next[0] - last[0], next[1] - last[1]) < 2) return prev;
        return [...prev, next];
      });
    },
    [freehandActive, toSvgCoords],
  );
  const onPointerUp = useCallback(() => {
    if (tool !== "freehand" || !freehandActive) return;
    const m = finalize("freehand", pending, sp);
    if (m) commit(m);
    setPending([]);
    setFreehandActive(false);
    setTool("none");
  }, [tool, freehandActive, pending, commit, sp]);

  const removeMeasurement = useCallback((id: string) => {
    setMeasurements((prev) => prev.filter((m) => m.id !== id));
  }, []);

  return (
    <>
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: drawing surface; measurements are placed via pointer (mouse / touch / pen). Keyboard equivalents (Tab between landmarks, Enter to delete) live on the per-marker chrome below, not on the canvas itself. */}
      <svg
        ref={svgRef}
        role="img"
        aria-label="Measurements overlay"
        onClick={onClick}
        onDoubleClick={onDblClick}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          pointerEvents: tool !== "none" ? "all" : "none",
          cursor: tool !== "none" ? "crosshair" : "default",
          zIndex: 5,
        }}
      >
        {measurements.map((m) => (
          <MeasurementNode key={m.id} m={m} sp={sp} />
        ))}

        {/* Pending (in-progress) shape */}
        {pending.length > 0 && (
          <PendingNode kind={tool === "none" ? null : (tool as MeasurementKind)} points={pending} />
        )}
      </svg>

      <h2>Measurements</h2>
      <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
        {(
          [
            ["distance", "Distance"],
            ["angle", "Angle"],
            ["area", "Area"],
            ["ellipse", "Ellipse"],
            ["freehand", "Freehand"],
            ["arrow", "Arrow"],
            ["text", "Text"],
            ["probe", "Probe"],
          ] as [Tool, string][]
        ).map(([t, label]) => (
          <button
            key={t}
            type="button"
            className={tool === t ? "viewer-btn viewer-btn--active" : "viewer-btn"}
            onClick={() => {
              setTool(tool === t ? "none" : t);
              setPending([]);
              setFreehandActive(false);
            }}
          >
            {label}
          </button>
        ))}
        {measurements.length > 0 && (
          <button type="button" className="viewer-btn" onClick={() => setMeasurements([])}>
            Clear all
          </button>
        )}
      </div>
      {tool !== "none" && (
        <p className="meta" style={{ fontSize: "0.7rem", marginTop: "0.3rem" }}>
          {tool === "distance" && "Click two points to measure distance."}
          {tool === "angle" && "Click three points: first arm, vertex, second arm."}
          {tool === "area" && "Click polygon vertices. Double-click to close."}
          {tool === "ellipse" && "Click two opposite corners of the ellipse bounding box."}
          {tool === "freehand" && "Drag to trace a freehand path. Release to finish."}
          {tool === "arrow" && "Click tail, then head."}
          {tool === "text" && "Click to place a text annotation."}
          {tool === "probe" && "Click a pixel to read its value."}
        </p>
      )}
      {measurements.length > 0 && (
        <div style={{ marginTop: "0.5rem" }}>
          {measurements.map((m) => (
            <div
              key={m.id}
              className="meta"
              style={{ fontSize: "0.75rem", display: "flex", justifyContent: "space-between" }}
            >
              <span>
                {m.kind}: {m.label ?? ""}
              </span>
              <button
                type="button"
                onClick={() => removeMeasurement(m.id)}
                style={{
                  background: "none",
                  border: "none",
                  color: "#f66",
                  cursor: "pointer",
                  fontSize: "0.7rem",
                  padding: 0,
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Rendering pieces (exported so other overlays can reuse them).
// ---------------------------------------------------------------------------

interface NodeProps {
  m: Measurement;
  sp: { spU: number; spV: number };
}

export function MeasurementNode({ m, sp }: NodeProps) {
  const color = m.color ?? DEFAULT_COLOR[m.kind];

  switch (m.kind) {
    case "distance": {
      const [a, b] = m.points;
      return (
        <g>
          <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={color} strokeWidth={1.5} />
          {m.points.map((p) => (
            <circle key={`${p[0]},${p[1]}`} cx={p[0]} cy={p[1]} r={3} fill={color} />
          ))}
          <text
            x={(a[0] + b[0]) / 2}
            y={(a[1] + b[1]) / 2 - 6}
            fill={color}
            fontSize={12}
            fontFamily="monospace"
            textAnchor="middle"
          >
            {m.label ?? ""}
          </text>
        </g>
      );
    }
    case "angle": {
      const [a, v, c] = m.points;
      return (
        <g>
          <line x1={a[0]} y1={a[1]} x2={v[0]} y2={v[1]} stroke={color} strokeWidth={1.5} />
          <line x1={v[0]} y1={v[1]} x2={c[0]} y2={c[1]} stroke={color} strokeWidth={1.5} />
          {m.points.map((p) => (
            <circle key={`${p[0]},${p[1]}`} cx={p[0]} cy={p[1]} r={3} fill={color} />
          ))}
          <text x={v[0] + 10} y={v[1] - 6} fill={color} fontSize={12} fontFamily="monospace">
            {m.label ?? ""}
          </text>
        </g>
      );
    }
    case "area": {
      const cx = m.points.reduce((s, p) => s + p[0], 0) / m.points.length;
      const cy = m.points.reduce((s, p) => s + p[1], 0) / m.points.length;
      return (
        <g>
          <polygon
            points={m.points.map((p) => `${p[0]},${p[1]}`).join(" ")}
            fill={`${color}26`}
            stroke={color}
            strokeWidth={1.5}
          />
          {m.points.map((p) => (
            <circle key={`${p[0]},${p[1]}`} cx={p[0]} cy={p[1]} r={3} fill={color} />
          ))}
          <text x={cx} y={cy} fill={color} fontSize={12} fontFamily="monospace" textAnchor="middle">
            {m.label ?? ""}
          </text>
        </g>
      );
    }
    case "ellipse": {
      const [p0, p1] = m.points;
      const g = ellipseFromBBox(p0, p1, sp);
      return (
        <g>
          <ellipse
            cx={g.cx}
            cy={g.cy}
            rx={g.rx}
            ry={g.ry}
            fill={`${color}22`}
            stroke={color}
            strokeWidth={1.5}
          />
          {/* Major/minor axis crosshair */}
          <line
            x1={g.cx - g.rx}
            y1={g.cy}
            x2={g.cx + g.rx}
            y2={g.cy}
            stroke={color}
            strokeWidth={0.75}
            strokeDasharray="3,3"
          />
          <line
            x1={g.cx}
            y1={g.cy - g.ry}
            x2={g.cx}
            y2={g.cy + g.ry}
            stroke={color}
            strokeWidth={0.75}
            strokeDasharray="3,3"
          />
          <text
            x={g.cx}
            y={g.cy + g.ry + 14}
            fill={color}
            fontSize={11}
            fontFamily="monospace"
            textAnchor="middle"
          >
            {m.label ?? ""}
          </text>
        </g>
      );
    }
    case "freehand": {
      const d = m.points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");
      const last = m.points[m.points.length - 1];
      return (
        <g>
          <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
          <text x={last[0] + 6} y={last[1] - 6} fill={color} fontSize={11} fontFamily="monospace">
            {m.label ?? ""}
          </text>
        </g>
      );
    }
    case "arrow": {
      const [a, b] = m.points;
      const id = `arrow-${m.id}`;
      return (
        <g>
          <defs>
            <marker
              id={id}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M0,0 L10,5 L0,10 z" fill={color} />
            </marker>
          </defs>
          <line
            x1={a[0]}
            y1={a[1]}
            x2={b[0]}
            y2={b[1]}
            stroke={color}
            strokeWidth={1.5}
            markerEnd={`url(#${id})`}
          />
          {m.label && (
            <text x={b[0] + 8} y={b[1] - 6} fill={color} fontSize={11} fontFamily="monospace">
              {m.label}
            </text>
          )}
        </g>
      );
    }
    case "text": {
      const [p] = m.points;
      return (
        <g>
          <text x={p[0]} y={p[1]} fill={color} fontSize={12} fontFamily="monospace">
            {m.label ?? ""}
          </text>
        </g>
      );
    }
    case "probe": {
      const [p] = m.points;
      return (
        <g>
          <line x1={p[0] - 6} y1={p[1]} x2={p[0] + 6} y2={p[1]} stroke={color} strokeWidth={1} />
          <line x1={p[0]} y1={p[1] - 6} x2={p[0]} y2={p[1] + 6} stroke={color} strokeWidth={1} />
          <text x={p[0] + 8} y={p[1] - 8} fill={color} fontSize={11} fontFamily="monospace">
            {m.label ?? ""}
          </text>
        </g>
      );
    }
  }
}

function PendingNode({ kind, points }: { kind: MeasurementKind | null; points: Pt[] }) {
  if (!kind) return null;
  const color = DEFAULT_COLOR[kind];

  if (kind === "freehand" && points.length > 1) {
    const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");
    return <path d={d} fill="none" stroke={color} strokeWidth={1.5} />;
  }
  if ((kind === "area" || kind === "angle" || kind === "distance") && points.length > 1) {
    const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");
    return (
      <>
        <path d={d} fill="none" stroke={color} strokeWidth={1} strokeDasharray="4,4" />
        {points.map((p) => (
          <circle key={`${p[0]},${p[1]}`} cx={p[0]} cy={p[1]} r={3} fill="#fff" />
        ))}
      </>
    );
  }
  return (
    <>
      {points.map((p) => (
        <circle key={`${p[0]},${p[1]}`} cx={p[0]} cy={p[1]} r={3} fill="#fff" />
      ))}
    </>
  );
}
