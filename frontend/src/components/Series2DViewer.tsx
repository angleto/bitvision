"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE_URL, getStoredToken } from "@/lib/api";

interface Props {
  seriesId: string;
  sliceCount: number;
  /** Optional caption shown in the corner overlay (modality, description). */
  caption?: string;
}

/**
 * Full-screen 2D fallback for series the 3D pack worker can't volumize:
 * radiographs (CR / DX, single frame), mammograms, mixed-SOP test data.
 * Drives the same ``/api/series/{id}/thumbnail`` endpoint used by the
 * preview card but at ``max_side=2048`` so it renders sharp on the
 * viewer canvas.
 *
 * Interactions mirror a minimal PACS:
 *   - Mouse wheel  → previous / next slice (no-op when sliceCount=1).
 *   - Drag (LMB)   → window/level: horizontal = WW, vertical = WC.
 *   - Double-click → reset window/level to the DICOM defaults.
 *
 * Each interaction triggers a re-fetch (the backend rasterises the
 * JPEG with the requested W/L deltas), so we debounce drag updates
 * to keep the network usage sane.
 */
type DocFallback =
  | { kind: "pdf"; url: string; title: string | null }
  | { kind: "text"; body: string; title: string | null };

export default function Series2DViewer({ seriesId, sliceCount, caption }: Props) {
  const t = useTranslations("viewer2d");
  const [index, setIndex] = useState(Math.floor(sliceCount / 2));
  const [wcDelta, setWcDelta] = useState(0);
  const [wwDelta, setWwDelta] = useState(0);
  const [src, setSrc] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // When the active series can't render as pixel data (Encapsulated
  // PDF, Structured Report) we hit ``/document`` and stash the result
  // here so the canvas can swap in a PDF iframe or a text panel
  // instead of the "no pixel data" error.
  const [docFallback, setDocFallback] = useState<DocFallback | null>(null);

  const draggingRef = useRef(false);
  const lastPosRef = useRef<{ x: number; y: number } | null>(null);
  const debounceRef = useRef<number | null>(null);

  // Pending W/L deltas while dragging — committed to state on release
  // (or via a 60ms debounce so the image follows the cursor without
  // hammering the backend).
  const pendingRef = useRef<{ wc: number; ww: number }>({ wc: 0, ww: 0 });

  const fetchSlice = useCallback(
    async (idx: number, wc: number, ww: number, signal: AbortSignal) => {
      const token = getStoredToken();
      const headers: Record<string, string> = {};
      if (token) headers.authorization = `Bearer ${token}`;
      // ``API_BASE_URL`` is the empty string in same-origin production
      // builds, which makes ``new URL("/api/...")`` throw "Invalid URL"
      // because URL needs an absolute base. Fall back to
      // ``window.location.origin`` when no base is configured so the
      // viewer works in both dev (with an explicit API host) and prod.
      const base = API_BASE_URL || window.location.origin;
      const url = new URL(`/api/series/${seriesId}/thumbnail`, base);
      url.searchParams.set("index", String(idx));
      url.searchParams.set("max_side", "2048");
      if (wc !== 0) url.searchParams.set("wc_delta", String(wc));
      if (ww !== 0) url.searchParams.set("ww_delta", String(ww));
      const r = await fetch(url, { credentials: "include", headers, signal });
      if (r.status === 422) throw new Error("no pixel data");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.blob();
    },
    [seriesId],
  );

  // Same pattern as ``SeriesPreview``: AbortController + 60ms debounce
  // so dragging the slider / window-level doesn't issue a request per
  // pixel of cursor movement. Previous frame stays visible while the
  // new one loads — perceived as smooth crossfade.
  useEffect(() => {
    const ac = new AbortController();
    const t = window.setTimeout(() => {
      setLoading(true);
      setErr(null);
      fetchSlice(index, wcDelta, wwDelta, ac.signal)
        .then((blob) => {
          // A pixel-data response wins — drop any prior document
          // fallback (the user may have scrolled past an SR onto an
          // image) and revoke the PDF blob URL to free memory.
          setDocFallback((prev) => {
            if (prev?.kind === "pdf") URL.revokeObjectURL(prev.url);
            return null;
          });
          const url = URL.createObjectURL(blob);
          setSrc((prev) => {
            if (prev) URL.revokeObjectURL(prev);
            return url;
          });
        })
        .catch(async (e) => {
          if (ac.signal.aborted) return;
          if (!(e instanceof Error) || e.message !== "no pixel data") {
            setErr(e instanceof Error ? e.message : "load failed");
            return;
          }
          // Fallback: try the document endpoint. Encapsulated PDF
          // (Italian discharge summaries, lab reports) and SR text
          // both render usefully here; everything else (PR / KO /
          // CDA) returns 415 and we surface the original error.
          try {
            const token = getStoredToken();
            const headers: Record<string, string> = {};
            if (token) headers.authorization = `Bearer ${token}`;
            const base2 = API_BASE_URL || window.location.origin;
            const url = new URL(`/api/series/${seriesId}/document`, base2);
            url.searchParams.set("index", String(index));
            const r = await fetch(url, { credentials: "include", headers, signal: ac.signal });
            if (r.status === 415) {
              setErr("no pixel data");
              return;
            }
            if (!r.ok) {
              setErr(`document HTTP ${r.status}`);
              return;
            }
            const kind = r.headers.get("x-document-kind");
            const title = r.headers.get("x-document-title");
            if (kind === "pdf") {
              const blob = await r.blob();
              const objectUrl = URL.createObjectURL(blob);
              setDocFallback((prev) => {
                if (prev?.kind === "pdf") URL.revokeObjectURL(prev.url);
                return { kind: "pdf", url: objectUrl, title };
              });
            } else if (kind === "text") {
              const body = await r.text();
              setDocFallback((prev) => {
                if (prev?.kind === "pdf") URL.revokeObjectURL(prev.url);
                return { kind: "text", body, title };
              });
            } else {
              setErr("no pixel data");
            }
          } catch (innerErr) {
            if (ac.signal.aborted) return;
            setErr(innerErr instanceof Error ? innerErr.message : "load failed");
          }
        })
        .finally(() => {
          if (!ac.signal.aborted) setLoading(false);
        });
    }, 60);
    return () => {
      window.clearTimeout(t);
      ac.abort();
    };
  }, [fetchSlice, index, wcDelta, wwDelta, seriesId]);

  // Release the PDF blob URL on unmount so the browser can reclaim
  // the memory the iframe was holding.
  useEffect(() => {
    return () => {
      setDocFallback((prev) => {
        if (prev?.kind === "pdf") URL.revokeObjectURL(prev.url);
        return null;
      });
    };
  }, []);

  // Wheel: change slice. Attached as a non-passive native listener so
  // ``preventDefault()`` actually stops the page from scrolling beneath
  // the cursor — React's synthetic onWheel is passive in React 17+.
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = containerRef.current;
    if (!el || sliceCount <= 1) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const delta = e.deltaY > 0 ? 1 : -1;
      setIndex((prev) => Math.max(0, Math.min(sliceCount - 1, prev + delta)));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [sliceCount]);

  const flushPending = useCallback(() => {
    setWcDelta(pendingRef.current.wc);
    setWwDelta(pendingRef.current.ww);
  }, []);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      draggingRef.current = true;
      lastPosRef.current = { x: e.clientX, y: e.clientY };
      pendingRef.current = { wc: wcDelta, ww: wwDelta };
    },
    [wcDelta, wwDelta],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!draggingRef.current || !lastPosRef.current) return;
      const dx = e.clientX - lastPosRef.current.x;
      const dy = e.clientY - lastPosRef.current.y;
      lastPosRef.current = { x: e.clientX, y: e.clientY };
      // Sensitivity: ~4 HU per pixel feels right for CT, fine for XR too.
      pendingRef.current.ww += dx * 4;
      pendingRef.current.wc -= dy * 4;
      if (debounceRef.current !== null) {
        window.clearTimeout(debounceRef.current);
      }
      debounceRef.current = window.setTimeout(flushPending, 60);
    },
    [flushPending],
  );

  const onMouseUp = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    if (debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    flushPending();
  }, [flushPending]);

  const reset = useCallback(() => {
    pendingRef.current = { wc: 0, ww: 0 };
    setWcDelta(0);
    setWwDelta(0);
  }, []);

  // ESC to reset; arrow keys for slices.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") reset();
      if (sliceCount > 1) {
        if (e.key === "ArrowDown" || e.key === "ArrowRight") {
          setIndex((prev) => Math.min(sliceCount - 1, prev + 1));
        } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
          setIndex((prev) => Math.max(0, prev - 1));
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reset, sliceCount]);

  return (
    <div
      ref={containerRef}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={onMouseUp}
      onDoubleClick={reset}
      style={{
        position: "relative",
        width: "100%",
        height: "100%",
        background: "#000",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        cursor: draggingRef.current ? "grabbing" : sliceCount > 1 ? "ns-resize" : "grab",
        userSelect: "none",
        overflow: "hidden",
        overscrollBehavior: "contain",
      }}
    >
      {docFallback?.kind === "pdf" ? (
        <div style={{ width: "100%", height: "100%", background: "#1a1d24" }}>
          {docFallback.title && (
            <div
              style={{
                background: "rgba(0,0,0,0.65)",
                color: "#e6ecf3",
                font: "11px/1.4 ui-monospace, monospace",
                padding: "6px 10px",
                borderBottom: "1px solid #2a2f3b",
              }}
            >
              {docFallback.title}
            </div>
          )}
          <iframe
            src={docFallback.url}
            title={docFallback.title ?? t("encapsulatedPdf")}
            style={{
              width: "100%",
              // Subtract the title strip if present so the iframe
              // never overflows the viewer canvas.
              height: docFallback.title ? "calc(100% - 28px)" : "100%",
              border: "none",
              background: "#fff",
            }}
          />
        </div>
      ) : docFallback?.kind === "text" ? (
        <div
          style={{
            width: "100%",
            height: "100%",
            background: "#0f1218",
            color: "#dde3ee",
            overflow: "auto",
            padding: "16px 22px",
            font: "13px/1.55 ui-monospace, 'Menlo', monospace",
          }}
        >
          {docFallback.title && (
            <h3
              style={{
                font: "600 13px/1.4 'Inter', system-ui, sans-serif",
                letterSpacing: "0.02em",
                color: "#f5f7fb",
                margin: "0 0 12px",
                paddingBottom: 8,
                borderBottom: "1px solid #2a2f3b",
              }}
            >
              {docFallback.title}
            </h3>
          )}
          <pre
            style={{
              margin: 0,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {docFallback.body}
          </pre>
        </div>
      ) : err ? (
        <div style={{ color: "#f88", padding: "1.5rem", textAlign: "center" }}>
          {err === "no pixel data" ? t("noPixelData") : t("loadError", { detail: err })}
        </div>
      ) : src ? (
        <img
          src={src}
          alt={caption ?? "DICOM slice"}
          draggable={false}
          style={{
            maxWidth: "100%",
            maxHeight: "100%",
            objectFit: "contain",
            pointerEvents: "none",
            opacity: loading ? 0.7 : 1,
            transition: "opacity 0.1s ease",
          }}
        />
      ) : (
        <div style={{ color: "#aaa", fontSize: "0.9rem" }}>{t("loading")}</div>
      )}

      {/* HUD: slice counter (only when relevant) */}
      {sliceCount > 1 && (
        <div
          style={{
            position: "absolute",
            top: 12,
            right: 12,
            background: "rgba(0,0,0,0.65)",
            color: "#e6ecf3",
            font: "11px/1.4 ui-monospace, monospace",
            padding: "4px 8px",
            borderRadius: 4,
            letterSpacing: "0.04em",
          }}
        >
          slice {index + 1} / {sliceCount}
        </div>
      )}

      {/* Slider overlay along the bottom edge — explicit control that
          works on touch and never collides with page scroll. Hidden on
          single-slice series to keep the canvas clean. */}
      {sliceCount > 1 && (
        <div
          onMouseDown={(e) => e.stopPropagation()}
          onMouseMove={(e) => e.stopPropagation()}
          onMouseUp={(e) => e.stopPropagation()}
          onWheel={(e) => e.stopPropagation()}
          style={{
            position: "absolute",
            left: 12,
            right: 12,
            bottom: 48,
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            background: "rgba(20,24,32,0.78)",
            backdropFilter: "blur(6px)",
            border: "1px solid #2a2f3b",
            borderRadius: 8,
            padding: "6px 10px",
          }}
        >
          <button
            type="button"
            className="viewer-chip-btn"
            onClick={() => setIndex((p) => Math.max(0, p - 1))}
            title={t("prevSliceTitle")}
            style={{ width: 24, height: 24 }}
          >
            ◀
          </button>
          <input
            type="range"
            min={0}
            max={sliceCount - 1}
            step={1}
            value={index}
            onChange={(e) => setIndex(Number.parseInt(e.target.value, 10))}
            aria-label={`Slice ${index + 1} di ${sliceCount}`}
            style={{
              flex: 1,
              accentColor: "#e96b1f",
              minWidth: 0,
            }}
          />
          <button
            type="button"
            className="viewer-chip-btn"
            onClick={() => setIndex((p) => Math.min(sliceCount - 1, p + 1))}
            title={t("nextSliceTitle")}
            style={{ width: 24, height: 24 }}
          >
            ▶
          </button>
        </div>
      )}

      {/* HUD: window/level deltas */}
      {(wcDelta !== 0 || wwDelta !== 0) && (
        <div
          style={{
            position: "absolute",
            bottom: 12,
            left: 12,
            background: "rgba(0,0,0,0.65)",
            color: "#e6ecf3",
            font: "11px/1.4 ui-monospace, monospace",
            padding: "4px 8px",
            borderRadius: 4,
          }}
        >
          WC {wcDelta > 0 ? "+" : ""}
          {wcDelta.toFixed(0)} · WW {wwDelta > 0 ? "+" : ""}
          {wwDelta.toFixed(0)}
        </div>
      )}

      {/* Caption + reset */}
      <div
        style={{
          position: "absolute",
          top: 12,
          left: 12,
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
        }}
      >
        {caption && (
          <span
            style={{
              background: "rgba(0,0,0,0.65)",
              color: "#e6ecf3",
              font: "11px/1.4 ui-monospace, monospace",
              padding: "4px 8px",
              borderRadius: 4,
              maxWidth: 320,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {caption}
          </span>
        )}
        {(wcDelta !== 0 || wwDelta !== 0) && (
          <button
            type="button"
            onClick={reset}
            className="viewer-chip-btn"
            title={t("resetWlTitle")}
            style={{ width: "auto", padding: "0 8px", fontSize: "0.72rem" }}
          >
            {t("resetWl")}
          </button>
        )}
      </div>

      {/* Hint overlay (first 4s) */}
      <FadeHint sliceCount={sliceCount} />
    </div>
  );
}

function FadeHint({ sliceCount }: { sliceCount: number }) {
  const t = useTranslations("viewer2d");
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const timer = window.setTimeout(() => setVisible(false), 4000);
    return () => window.clearTimeout(timer);
  }, []);
  if (!visible) return null;
  return (
    <div
      style={{
        position: "absolute",
        bottom: 12,
        right: 12,
        background: "rgba(0,0,0,0.65)",
        color: "#a8b2c5",
        font: "11px/1.5 ui-monospace, monospace",
        padding: "6px 10px",
        borderRadius: 4,
        textAlign: "right",
        pointerEvents: "none",
        opacity: 0.9,
        transition: "opacity 0.6s ease",
      }}
    >
      {sliceCount > 1 ? `${t("hintWheel")} · ` : ""}
      {t("hintDragWl")}
    </div>
  );
}
