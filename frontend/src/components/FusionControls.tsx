"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, type Series, fetchVolume, studiesApi } from "@/lib/api";

import type { FusionColormap, FusionVolume } from "./VolumeViewer";

const FUSION_COLORMAPS: FusionColormap[] = ["hot", "pet", "jet", "green", "magenta", "cyan"];

interface Props {
  studyId: string;
  primarySeriesId: string;
  /** When set (e.g. by a hanging protocol like PT-CT), the panel auto-
   *  selects and loads the first candidate matching this modality on
   *  mount. Subsequent prop changes do not re-trigger the auto-load to
   *  avoid clobbering manual user selection. */
  autoFuseModality?: string | null;
  onFusionChange: (fusion: FusionVolume | null) => void;
  onOpacityChange: (opacity: number) => void;
  onColormapChange: (colormap: FusionColormap) => void;
  /** Optional callback for the parent to track which sibling series
   *  is currently fused. Useful when a separate consumer (e.g. the
   *  Cornerstone MPR layout) wants to load the same volume by id
   *  rather than by ``FusionVolume`` payload. */
  onActiveSeriesIdChange?: (id: string | null) => void;
  /** Notifies the parent when a fusion load starts / progresses /
   *  finishes, so a viewport-level overlay can show "loading fusion
   *  overlay" while the volume downloads. ``percent`` is null when
   *  the server doesn't advertise Content-Length (indeterminate). */
  onLoadingChange?: (
    info:
      | {
          state: "idle";
        }
      | {
          state: "downloading" | "decoding";
          loaded: number;
          total: number;
          percent: number | null;
        },
  ) => void;
}

export default function FusionControls({
  studyId,
  primarySeriesId,
  autoFuseModality,
  onFusionChange,
  onOpacityChange,
  onColormapChange,
  onActiveSeriesIdChange,
  onLoadingChange,
}: Props) {
  const [candidates, setCandidates] = useState<Series[] | null>(null);
  const [activeSeriesId, setActiveSeriesId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Progress for the active fetch: ``phase`` distinguishes the network
  // download from the short post-fetch decode (Float32Array view +
  // colormap / opacity wiring on the volume actor); ``loaded`` /
  // ``total`` are bytes for the download phase. ``percent`` is null
  // when the server didn't advertise Content-Length so the bar
  // renders as indeterminate stripes instead of a fill ratio.
  const [progress, setProgress] = useState<{
    phase: "download" | "decode";
    loaded: number;
    total: number;
    percent: number | null;
  } | null>(null);
  // Default to 0.05 — empirically the fusion overlay reads as
  // a "halo" of metabolic activity at very low alphas; values above
  // 0.1 saturate quickly into the silhouette-cylinder territory.
  const [opacity, setOpacity] = useState(0.05);
  const [colormap, setColormap] = useState<FusionColormap>("hot");

  useEffect(() => {
    let cancelled = false;
    studiesApi
      .fusionCandidates(studyId, primarySeriesId)
      .then((list) => {
        if (!cancelled) setCandidates(list);
      })
      .catch((e) => {
        if (!cancelled)
          setErr(e instanceof ApiError ? e.message : "failed to load fusion candidates");
      });
    return () => {
      cancelled = true;
    };
  }, [studyId, primarySeriesId]);

  const clear = useCallback(() => {
    setActiveSeriesId(null);
    onActiveSeriesIdChange?.(null);
    onFusionChange(null);
    setErr(null);
  }, [onFusionChange, onActiveSeriesIdChange]);

  const loadSeries = useCallback(
    async (seriesId: string) => {
      setLoading(true);
      setErr(null);
      // Pre-emptively place the row in the active slot so the spinner
      // attaches to the correct button while the download is in
      // flight. Cleared on error.
      setActiveSeriesId(seriesId);
      onActiveSeriesIdChange?.(seriesId);
      onLoadingChange?.({
        state: "downloading",
        loaded: 0,
        total: 0,
        percent: null,
      });
      try {
        const { header, scalars } = await fetchVolume(seriesId, {
          onProgress: ({ loaded, total, phase }) => {
            const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : null;
            const next = {
              phase,
              loaded,
              total,
              percent,
            };
            setProgress(next);
            onLoadingChange?.({
              state: phase === "decode" ? "decoding" : "downloading",
              loaded,
              total,
              percent,
            });
          },
        });
        onFusionChange({
          dimensions: [header.nx, header.ny, header.nz],
          spacing: header.spacing,
          scalars,
          range: header.valueRange,
          colormap,
          opacity,
        });
        setProgress(null);
        onLoadingChange?.({ state: "idle" });
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "failed to load fusion volume");
        setActiveSeriesId(null);
        onActiveSeriesIdChange?.(null);
        setProgress(null);
        onLoadingChange?.({ state: "idle" });
      } finally {
        setLoading(false);
      }
    },
    [colormap, opacity, onFusionChange, onActiveSeriesIdChange, onLoadingChange],
  );

  // Auto-fuse on first candidate match. Guarded against re-firing once
  // the user makes any explicit choice (activeSeriesId set non-null) so
  // we don't fight manual interaction.
  const autoTriedRef = useRef(false);
  useEffect(() => {
    if (autoTriedRef.current) return;
    if (!autoFuseModality || candidates === null || activeSeriesId !== null) return;
    const target = candidates.find(
      (c) => (c.modality ?? "").toUpperCase() === autoFuseModality.toUpperCase(),
    );
    if (target) {
      autoTriedRef.current = true;
      void loadSeries(target.id);
    } else if (candidates.length === 0) {
      // No candidates at all: nothing to do, but mark tried so future
      // prop churn doesn't re-evaluate.
      autoTriedRef.current = true;
    }
  }, [autoFuseModality, candidates, activeSeriesId, loadSeries]);

  const siblings = candidates ?? [];
  const empty = candidates !== null && siblings.length === 0;

  return (
    <>
      <h2>Fusion overlay</h2>
      <div className="card">
        {err && <div style={{ color: "#f88", fontSize: "0.7rem" }}>{err}</div>}
        {candidates === null && !err && (
          <div className="meta" style={{ fontSize: "0.7rem" }}>
            loading candidate series…
          </div>
        )}
        {empty && (
          <div className="meta" style={{ fontSize: "0.7rem" }}>
            no sibling series in this study to overlay
          </div>
        )}
        {siblings.length > 0 && (
          <>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.3rem" }}>
              {siblings.map((s) => {
                const isActive = s.id === activeSeriesId;
                const isLoadingThis = loading && s.id === activeSeriesId;
                return (
                  <button
                    key={s.id}
                    type="button"
                    className={isActive ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                    style={{ fontSize: "0.7rem", textAlign: "left" }}
                    onClick={() => {
                      if (isActive && !isLoadingThis) clear();
                      else if (!loading) loadSeries(s.id);
                    }}
                    disabled={loading}
                  >
                    {isLoadingThis ? "⟳ " : ""}
                    {s.modality ?? "?"} · #{s.series_number ?? "?"} ·{" "}
                    {s.series_description ?? s.body_part_examined ?? "(no description)"}
                  </button>
                );
              })}
            </div>
            {loading && progress && (
              <div
                aria-live="polite"
                style={{
                  marginTop: "0.4rem",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.2rem",
                  fontSize: "0.65rem",
                  color: "var(--bv-fg-soft, #94a3b8)",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>
                    {progress.phase === "download" ? "Scaricamento volume" : "Elaborazione"}
                    {progress.percent != null && progress.phase === "download"
                      ? ` · ${progress.percent}%`
                      : ""}
                  </span>
                  {progress.phase === "download" && progress.total > 0 && (
                    <span>
                      {(progress.loaded / 1024 / 1024).toFixed(1)} /{" "}
                      {(progress.total / 1024 / 1024).toFixed(1)} MB
                    </span>
                  )}
                </div>
                <div
                  role="progressbar"
                  tabIndex={-1}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={progress.percent ?? undefined}
                  style={{
                    height: 4,
                    borderRadius: 2,
                    background: "rgba(148,163,184,0.2)",
                    overflow: "hidden",
                    position: "relative",
                  }}
                >
                  {progress.percent != null ? (
                    <div
                      style={{
                        width: `${progress.percent}%`,
                        height: "100%",
                        background: "var(--bv-accent, #e96b1f)",
                        transition: "width 0.15s linear",
                      }}
                    />
                  ) : (
                    // Indeterminate bar: a 30 %-wide strip slides
                    // edge-to-edge while we wait for Content-Length-
                    // less responses. CSS keyframes are cheap enough
                    // to inline here without a stylesheet round-trip.
                    <div
                      style={{
                        position: "absolute",
                        top: 0,
                        height: "100%",
                        width: "30%",
                        background: "var(--bv-accent, #e96b1f)",
                        animation: "bvFusionIndeterminate 1.1s ease-in-out infinite",
                      }}
                    />
                  )}
                </div>
                <style>{`@keyframes bvFusionIndeterminate {
                  0% { left: -30%; }
                  100% { left: 100%; }
                }`}</style>
              </div>
            )}
            {activeSeriesId && (
              <>
                <label
                  className="meta"
                  style={{ display: "block", fontSize: "0.7rem", marginTop: "0.5rem" }}
                >
                  Blend opacity: {opacity.toFixed(3)}
                  <input
                    type="range"
                    min={0}
                    max={0.2}
                    step={0.005}
                    value={opacity}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      setOpacity(v);
                      onOpacityChange(v);
                    }}
                  />
                </label>
                <div
                  style={{
                    display: "flex",
                    gap: "0.3rem",
                    flexWrap: "wrap",
                    marginTop: "0.3rem",
                  }}
                >
                  {FUSION_COLORMAPS.map((c) => (
                    <button
                      key={c}
                      type="button"
                      style={{ fontSize: "0.65rem", padding: "0.15rem 0.4rem" }}
                      className={colormap === c ? "viewer-btn viewer-btn--active" : "viewer-btn"}
                      onClick={() => {
                        setColormap(c);
                        onColormapChange(c);
                      }}
                    >
                      {c}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="viewer-btn"
                  style={{ fontSize: "0.7rem", marginTop: "0.4rem" }}
                  onClick={clear}
                >
                  Remove overlay
                </button>
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}
