"use client";

// M6c review-UI box-labeling. Renders a staged instance at native pixel
// resolution and lets the reviewer draw ground-truth burned-in-PHI boxes over
// it (the answer key the automatic redaction's recall is scored against). The
// SVG overlay uses viewBox = intrinsic pixel space, so every box is authored
// directly in the GtBox coordinate system (top-left origin, XYWH) — no scaling
// math leaks into the stored labels.

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import {
  type DetectedBoxesResult,
  type GtBox,
  type GtScoreResult,
  PHI_CATEGORIES,
  type PhiCategory,
  contributionsApi,
} from "@/lib/contributions_api";

const CATEGORY_COLOR: Record<string, string> = {
  name: "#e6194b",
  codice_fiscale: "#f58231",
  date: "#3cb44b",
  address: "#4363d8",
  phone: "#911eb4",
  email: "#42d4f4",
  mrn: "#bfa100",
  other: "#a9a9a9",
  unknown: "#a9a9a9",
};

interface Props {
  submissionId: string;
  instanceId: string;
  etag: string;
  onEtag: (etag: string) => void;
  labelSave: string;
  labelScore: string;
}

export default function GtBoxEditor({
  submissionId,
  instanceId,
  etag,
  onEtag,
  labelSave,
  labelScore,
}: Props): React.JSX.Element {
  const [detected, setDetected] = useState<DetectedBoxesResult | null>(null);
  const [boxes, setBoxes] = useState<GtBox[]>([]);
  const [variant, setVariant] = useState<"original" | "redacted">("original");
  const [category, setCategory] = useState<PhiCategory>("name");
  const [score, setScore] = useState<GtScoreResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<GtBox | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [det, gt] = await Promise.all([
          contributionsApi.detectedBoxes(submissionId, instanceId),
          contributionsApi.getGtBoxes(submissionId, instanceId),
        ]);
        if (cancelled) return;
        setDetected(det);
        setBoxes(gt.boxes);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [submissionId, instanceId]);

  // Pointer (screen) → intrinsic image pixel, via the SVG's own CTM so it holds
  // under any CSS scaling of the overlay.
  const toImage = useCallback((e: React.PointerEvent): { x: number; y: number } | null => {
    const svg = svgRef.current;
    if (!svg) return null;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const pt = svg.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    const p = pt.matrixTransform(ctm.inverse());
    return { x: Math.round(p.x), y: Math.round(p.y) };
  }, []);

  const onDown = useCallback(
    (e: React.PointerEvent) => {
      const p = toImage(e);
      if (!p) return;
      dragStart.current = p;
      setDraft({ x: p.x, y: p.y, w: 0, h: 0, text: "", category });
      (e.target as Element).setPointerCapture?.(e.pointerId);
    },
    [toImage, category],
  );

  const onMove = useCallback(
    (e: React.PointerEvent) => {
      const s = dragStart.current;
      if (!s) return;
      const p = toImage(e);
      if (!p) return;
      setDraft({
        x: Math.min(s.x, p.x),
        y: Math.min(s.y, p.y),
        w: Math.abs(p.x - s.x),
        h: Math.abs(p.y - s.y),
        text: "",
        category,
      });
    },
    [toImage, category],
  );

  const onUp = useCallback(() => {
    const d = draft;
    dragStart.current = null;
    setDraft(null);
    if (d && d.w >= 3 && d.h >= 3) setBoxes((b) => [...b, d]);
  }, [draft]);

  const save = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await contributionsApi.saveGtBoxes(submissionId, instanceId, boxes, etag);
      setBoxes(res.boxes);
      onEtag(res.etag);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  }, [submissionId, instanceId, boxes, etag, onEtag]);

  const runScore = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      setScore(await contributionsApi.gtScore(submissionId, instanceId));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "score failed");
    } finally {
      setBusy(false);
    }
  }, [submissionId, instanceId]);

  const w = detected?.width ?? 0;
  const h = detected?.height ?? 0;

  return (
    <div style={{ display: "flex", gap: 16, marginTop: 8, flexWrap: "wrap" }}>
      <div style={{ maxWidth: 520 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 4, fontSize: "0.8rem" }}>
          <label>
            <input
              type="radio"
              checked={variant === "original"}
              onChange={() => setVariant("original")}
            />{" "}
            original
          </label>
          <label>
            <input
              type="radio"
              checked={variant === "redacted"}
              onChange={() => setVariant("redacted")}
            />{" "}
            redacted
          </label>
        </div>
        <div style={{ position: "relative", background: "#000", lineHeight: 0 }}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={contributionsApi.renderUrl(submissionId, instanceId, variant)}
            alt="staged instance"
            style={{ width: "100%", height: "auto", display: "block" }}
          />
          {w > 0 && h > 0 && (
            <svg
              ref={svgRef}
              viewBox={`0 0 ${w} ${h}`}
              preserveAspectRatio="none"
              onPointerDown={onDown}
              onPointerMove={onMove}
              onPointerUp={onUp}
              style={{
                position: "absolute",
                inset: 0,
                width: "100%",
                height: "100%",
                cursor: "crosshair",
                touchAction: "none",
              }}
            >
              <title>ground-truth PHI boxes</title>
              {/* Auto-detected redaction boxes (dashed, informational). */}
              {(detected?.boxes ?? []).map((b, i) => (
                <rect
                  key={`d-${i}-${b.x}-${b.y}`}
                  x={b.x}
                  y={b.y}
                  width={b.w}
                  height={b.h}
                  fill="none"
                  stroke="#00e0ff"
                  strokeWidth={Math.max(1, w / 400)}
                  strokeDasharray={`${Math.max(2, w / 100)} ${Math.max(2, w / 100)}`}
                />
              ))}
              {/* Reviewer GT boxes (solid, category colour). */}
              {boxes.map((b, i) => (
                <rect
                  key={`g-${i}-${b.x}-${b.y}`}
                  x={b.x}
                  y={b.y}
                  width={b.w}
                  height={b.h}
                  fill={`${CATEGORY_COLOR[b.category] ?? "#a9a9a9"}22`}
                  stroke={CATEGORY_COLOR[b.category] ?? "#a9a9a9"}
                  strokeWidth={Math.max(1, w / 300)}
                />
              ))}
              {draft && (
                <rect
                  x={draft.x}
                  y={draft.y}
                  width={draft.w}
                  height={draft.h}
                  fill="none"
                  stroke={CATEGORY_COLOR[category]}
                  strokeWidth={Math.max(1, w / 300)}
                />
              )}
            </svg>
          )}
        </div>
        <p style={{ fontSize: "0.72rem", color: "#666", margin: "4px 0" }}>
          risk: {detected?.risk_level ?? "?"} · {w}×{h}px · dashed = auto-redaction, solid = GT
        </p>
      </div>

      <div style={{ minWidth: 260, fontSize: "0.8rem" }}>
        <label>
          new-box category{" "}
          <select value={category} onChange={(e) => setCategory(e.target.value as PhiCategory)}>
            {PHI_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: "8px 0",
            maxHeight: 220,
            overflow: "auto",
          }}
        >
          {boxes.map((b, i) => (
            <li
              key={`row-${i}-${b.x}-${b.y}`}
              style={{ display: "flex", gap: 4, alignItems: "center", marginBottom: 4 }}
            >
              <span
                aria-hidden="true"
                style={{
                  width: 10,
                  height: 10,
                  background: CATEGORY_COLOR[b.category] ?? "#a9a9a9",
                  display: "inline-block",
                }}
              />
              <select
                value={b.category}
                onChange={(e) =>
                  setBoxes((arr) =>
                    arr.map((x, j) => (j === i ? { ...x, category: e.target.value } : x)),
                  )
                }
              >
                {PHI_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <input
                aria-label="box text"
                placeholder="text"
                value={b.text}
                onChange={(e) =>
                  setBoxes((arr) =>
                    arr.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)),
                  )
                }
                style={{ width: 90 }}
              />
              <button
                type="button"
                onClick={() => setBoxes((arr) => arr.filter((_, j) => j !== i))}
                aria-label="delete box"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" disabled={busy} onClick={save}>
            {labelSave}
          </button>
          <button type="button" disabled={busy} onClick={runScore}>
            {labelScore}
          </button>
        </div>
        {score && (
          <p style={{ marginTop: 6 }}>
            recall {(score.recall * 100).toFixed(0)}% ({score.covered}/{score.total})
            {score.missed.length > 0 && (
              <span style={{ color: "crimson" }}> · missed: {score.missed.join(", ")}</span>
            )}
          </p>
        )}
        {err && (
          <p role="alert" style={{ color: "crimson" }}>
            {err}
          </p>
        )}
      </div>
    </div>
  );
}
