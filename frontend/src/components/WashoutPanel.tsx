"use client";

// Wash-out side panel for the multiphase contrast viewer — a TRANSPARENT
// per-phase table: phases across the columns, Lesion / Parenchyma / Δ down the
// rows. Every number maps to a ROI the operator placed and can see; clicking a
// cell jumps to that phase to inspect / adjust its box. No hidden state machine:
// an explicit "placing Lesion / Parenchyma" toggle decides what a new draw fills.
//
// Region-aware interpretation:
//  * adrenal — APW/RPW (Korobkin 1998; Szolar 1998) + adenoma flags (factual).
//  * liver   — APW/RPW do NOT apply; relative lesion-vs-parenchyma ΔHU per
//    phase (LI-RADS) from the parenchyma reference row.
//  * other   — raw indices, labelled adrenal-derived, no verdict.

import type { PhaseMap, PhaseRoiStats, PhaseWashout } from "@/lib/api";
import { useTranslations } from "next-intl";
import { useState } from "react";

type Region = "adrenal" | "liver" | "other";
type RoiTarget = "lesion" | "parenchyma";

export type PhaseCol = {
  paneIndex: number;
  seriesId: string;
  acquisitionPhase: string | null;
};

// HU std-dev above which a soft-tissue ROI is flagged as heterogeneous: a clean
// parenchyma / lesion ROI sits around 10-20 HU; ~90 HU means the sphere spans
// fat / air / vessel / a boundary and the mean is meaningless.
const HIGH_SD_HU = 25;

const WASHOUT_COLOR = "#34d399";
const UPTAKE_COLOR = "#e96b1f";
const ACCENT = "#e96b1f";

export interface WashoutPanelProps {
  result: PhaseRoiStats | null;
  busy: boolean;
  error: string | null;
  saved: boolean;
  region: Region;
  onRegionChange: (r: Region) => void;
  roiTarget: RoiTarget;
  onRoiTargetChange: (t: RoiTarget) => void;
  /** The phases shown as table columns, in display order. */
  phases: PhaseCol[];
  /** series_ids that currently carry a placed lesion / parenchyma ROI. */
  placed: { lesion: string[]; parenchyma: string[] };
  /** Copy the current target's box from the first phase that has it to the
   *  phases that do not (a best-effort accelerator). */
  onCopyToAll: () => void;
  /** Jump to a phase (clicking a cell) to inspect / adjust its ROI. */
  onCellClick: (paneIndex: number) => void;
  onRequestMap: (metric: "washout" | "subtraction") => Promise<PhaseMap | null>;
  onSave: () => void;
  onClose: () => void;
  /** Delete one ROI (lesion or reference parenchyma) across all phases. */
  onDeleteRoi: (which: RoiTarget) => void;
  /** Clear every ROI + result. */
  onReset: () => void;
  /** Abort a half-drawn ROI (Esc) so the operator can start over. */
  onCancelDraw: () => void;
}

function fmt(n: number | null | undefined, digits = 0): string {
  return n == null || Number.isNaN(n) ? "—" : n.toFixed(digits);
}

const REGIONS: Region[] = ["adrenal", "liver", "other"];

export default function WashoutPanel({
  result,
  busy,
  error,
  saved,
  region,
  onRegionChange,
  roiTarget,
  onRoiTargetChange,
  phases,
  placed,
  onCopyToAll,
  onCellClick,
  onRequestMap,
  onSave,
  onClose,
  onDeleteRoi,
  onReset,
  onCancelDraw,
}: WashoutPanelProps) {
  const t = useTranslations("contrast");
  const liver = region === "liver";
  const w: PhaseWashout | null = result?.washout ?? null;

  const lesionSample = (p: PhaseCol) => result?.samples.find((s) => s.series_id === p.seriesId);
  const parenHu = (p: PhaseCol) =>
    p.acquisitionPhase
      ? w?.parenchyma_curve.find((x) => x.acquisition_phase === p.acquisitionPhase)?.hu_mean
      : undefined;
  const deltaHu = (p: PhaseCol) =>
    p.acquisitionPhase
      ? w?.relative_curve.find((x) => x.acquisition_phase === p.acquisitionPhase)?.delta_hu
      : undefined;

  const lesionPlaced = new Set(placed.lesion);
  const parenPlaced = new Set(placed.parenchyma);
  const anyPlaced = placed.lesion.length > 0 || placed.parenchyma.length > 0;

  return (
    <aside
      className="viewer-chrome"
      style={{
        width: Math.min(560, 120 + phases.length * 78),
        maxWidth: "92vw",
        maxHeight: "44vh",
        overflowY: "auto",
        background: "#0b0e13",
        color: "#e6ecf3",
        border: "1px solid #1a1f2b",
        borderRadius: 8,
        padding: "0.55rem 0.7rem",
        fontSize: "0.8rem",
      }}
      aria-label={t("washoutTitle")}
    >
      {/* Title + region + close */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <strong>{t("washoutTitle")}</strong>
        <fieldset
          aria-label={t("regionLabel")}
          style={{ display: "flex", gap: 4, border: 0, padding: 0, margin: "0 0 0 auto" }}
        >
          {REGIONS.map((r) => (
            <button
              key={r}
              type="button"
              className="ghost"
              aria-pressed={region === r}
              onClick={() => onRegionChange(r)}
              style={{
                fontSize: "0.7rem",
                padding: "1px 7px",
                border: region === r ? `1px solid ${ACCENT}` : "1px solid #1a1f2b",
                color: region === r ? ACCENT : "#cbd5e1",
                borderRadius: 4,
              }}
            >
              {t(`region.${r}`)}
            </button>
          ))}
        </fieldset>
        <button
          type="button"
          className="ghost"
          onClick={onClose}
          aria-label={t("close")}
          style={{ fontSize: "0.72rem", padding: "1px 6px" }}
        >
          ✕
        </button>
      </div>

      {/* The transparent per-phase table. */}
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          margin: "7px 0 4px",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        <thead>
          <tr style={{ color: "#94a3b8", fontSize: "0.7rem" }}>
            <th style={{ textAlign: "left", fontWeight: 400, padding: "2px 4px" }} />
            {phases.map((p) => (
              <th
                key={p.seriesId}
                style={{ textAlign: "center", fontWeight: 600, padding: "2px 4px" }}
              >
                {p.acquisitionPhase ? t(`phase.${p.acquisitionPhase}`) : t("unclassified")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <Row
            label={t("stepLesion")}
            isTarget={roiTarget === "lesion"}
            onPickTarget={() => onRoiTargetChange("lesion")}
            placedCount={placed.lesion.length}
            total={phases.length}
            onDelete={placed.lesion.length ? () => onDeleteRoi("lesion") : undefined}
            cells={phases.map((p) => {
              const s = lesionSample(p);
              return (
                <Cell
                  key={p.seriesId}
                  placed={lesionPlaced.has(p.seriesId)}
                  hu={s?.hu_mean}
                  sd={s?.hu_std}
                  onClick={() => onCellClick(p.paneIndex)}
                />
              );
            })}
          />
          {liver && (
            <Row
              label={t("stepParenchyma")}
              isTarget={roiTarget === "parenchyma"}
              onPickTarget={() => onRoiTargetChange("parenchyma")}
              placedCount={placed.parenchyma.length}
              total={phases.length}
              onDelete={placed.parenchyma.length ? () => onDeleteRoi("parenchyma") : undefined}
              cells={phases.map((p) => (
                <Cell
                  key={p.seriesId}
                  placed={parenPlaced.has(p.seriesId)}
                  hu={parenHu(p)}
                  onClick={() => onCellClick(p.paneIndex)}
                />
              ))}
            />
          )}
          {liver && (
            <tr>
              <td style={{ color: "#94a3b8", padding: "2px 4px" }}>{t("deltaRow")}</td>
              {phases.map((p) => {
                const d = deltaHu(p);
                return (
                  <td
                    key={p.seriesId}
                    style={{
                      textAlign: "center",
                      padding: "2px 4px",
                      color: d == null ? "#64748b" : d < 0 ? ACCENT : "#cbd5e1",
                    }}
                  >
                    {d == null ? "—" : `${d > 0 ? "+" : ""}${fmt(d, 0)}`}
                  </td>
                );
              })}
            </tr>
          )}
        </tbody>
      </table>

      {/* What a new draw fills + the copy accelerator. */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span style={{ color: "#94a3b8", fontSize: "0.72rem" }}>{t("placingLabel")}</span>
        <button
          type="button"
          className="ghost"
          aria-pressed={roiTarget === "lesion"}
          onClick={() => onRoiTargetChange("lesion")}
          style={targetBtn(roiTarget === "lesion")}
        >
          {t("stepLesion")}
        </button>
        {liver && (
          <button
            type="button"
            className="ghost"
            aria-pressed={roiTarget === "parenchyma"}
            onClick={() => onRoiTargetChange("parenchyma")}
            style={targetBtn(roiTarget === "parenchyma")}
          >
            {t("stepParenchyma")}
          </button>
        )}
        <button
          type="button"
          className="ghost"
          onClick={onCopyToAll}
          title={t("copyToAllHint")}
          style={{ marginLeft: "auto", fontSize: "0.72rem" }}
        >
          {t("copyToAll")}
        </button>
      </div>
      <p style={{ color: "#64748b", fontSize: "0.68rem", margin: "4px 0 0" }}>{t("tableHint")}</p>

      {anyPlaced && (
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <button
            type="button"
            className="ghost"
            onClick={onCancelDraw}
            style={{ flex: 1, fontSize: "0.72rem" }}
            title={t("cancelDrawHint")}
          >
            {t("cancelDraw")}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={onReset}
            style={{ flex: 1, fontSize: "0.72rem", color: "#f88" }}
          >
            {t("restart")}
          </button>
        </div>
      )}

      {busy && <p className="meta">{t("computing")}</p>}
      {error && <p style={{ color: "var(--bv-danger, #f87171)" }}>{error}</p>}

      {result && w && (
        <div style={{ marginTop: 6, borderTop: "1px solid #1a1f2b", paddingTop: 6 }}>
          <WashoutCurve curve={w.curve} parenchyma={liver ? w.parenchyma_curve : []} />
          <WashoutTrendChip w={w} />
          {region === "liver" ? (
            <p className="meta" style={{ fontSize: "0.7rem", margin: "2px 0 0" }}>
              {t("liverNote")}
            </p>
          ) : (
            <AdrenalIndices w={w} region={region} />
          )}
          {result.samples.some((s) => s.hu_std > HIGH_SD_HU) && (
            <p style={{ color: "var(--bv-warning, #fbbf24)", fontSize: "0.72rem" }}>
              {t("highSdWarn")}
            </p>
          )}
          {w.enhanced_phase == null && (
            <p style={{ color: "var(--bv-warning, #fbbf24)", fontSize: "0.72rem" }}>
              {t("missingEnhanced")}
            </p>
          )}
          {result.skipped.length > 0 && (
            <p className="meta" style={{ fontSize: "0.72rem" }}>
              {t("skippedPhases", { count: result.skipped.length })}
            </p>
          )}
        </div>
      )}

      {result && w && <WashoutMapSection onRequestMap={onRequestMap} />}

      {result && result.samples.length > 0 && (
        <button
          type="button"
          className="ghost"
          disabled={busy || saved}
          onClick={onSave}
          style={{ marginTop: 8, width: "100%", fontSize: "0.78rem" }}
        >
          {saved ? t("saved") : t("save")}
        </button>
      )}
      <p className="meta" style={{ fontSize: "0.66rem", marginTop: 6, opacity: 0.8 }}>
        {t("notDiagnosis")}
      </p>
    </aside>
  );
}

function targetBtn(active: boolean): React.CSSProperties {
  return {
    fontSize: "0.72rem",
    padding: "1px 9px",
    border: active ? `1px solid ${ACCENT}` : "1px solid #1a1f2b",
    color: active ? ACCENT : "#cbd5e1",
    background: active ? "rgba(233,107,31,0.10)" : "transparent",
    borderRadius: 4,
  };
}

// One table row: a row label that doubles as the "place this ROI next" toggle,
// the per-phase cells, a placed-count badge and a delete-all-phases button.
function Row({
  label,
  isTarget,
  onPickTarget,
  placedCount,
  total,
  onDelete,
  cells,
}: {
  label: string;
  isTarget: boolean;
  onPickTarget: () => void;
  placedCount: number;
  total: number;
  onDelete?: () => void;
  cells: React.ReactNode[];
}) {
  const t = useTranslations("contrast");
  return (
    <tr>
      <td style={{ padding: "2px 4px", whiteSpace: "nowrap" }}>
        <button
          type="button"
          className="ghost"
          onClick={onPickTarget}
          title={t("placingLabel")}
          style={{
            fontSize: "0.76rem",
            padding: 0,
            color: isTarget ? ACCENT : "#e6ecf3",
            fontWeight: isTarget ? 700 : 500,
          }}
        >
          {isTarget ? "▸ " : ""}
          {label}
        </button>{" "}
        <span style={{ color: "#64748b", fontSize: "0.64rem" }}>
          {placedCount}/{total}
        </span>
        {onDelete && (
          <button
            type="button"
            className="ghost"
            onClick={onDelete}
            title={t("roiDelete")}
            aria-label={t("roiDelete")}
            style={{ color: "#f66", padding: "0 4px", fontSize: "0.82rem", lineHeight: 1 }}
          >
            ×
          </button>
        )}
      </td>
      {cells}
    </tr>
  );
}

// One per-phase cell: "—" not placed, "•" placed but HU not computed yet, else
// the HU (with a ⚠ when the ROI is heterogeneous). Click jumps to the phase.
function Cell({
  placed,
  hu,
  sd,
  onClick,
}: {
  placed: boolean;
  hu: number | null | undefined;
  sd?: number | null;
  onClick: () => void;
}) {
  const t = useTranslations("contrast");
  const hot = sd != null && sd > HIGH_SD_HU;
  return (
    <td style={{ padding: "1px 2px" }}>
      <button
        type="button"
        onClick={onClick}
        title={t("adjustHint")}
        style={{
          width: "100%",
          textAlign: "center",
          padding: "2px 4px",
          cursor: "pointer",
          borderRadius: 4,
          border: "1px solid transparent",
          font: "inherit",
          fontVariantNumeric: "tabular-nums",
          color: !placed ? "#64748b" : hot ? "var(--bv-warning, #fbbf24)" : "#e6ecf3",
          background: placed ? "rgba(148,163,184,0.10)" : "transparent",
        }}
      >
        {!placed ? "—" : hu == null ? "•" : `${hot ? "⚠ " : ""}${fmt(hu, 0)}`}
      </button>
    </td>
  );
}

function WashoutMapSection({
  onRequestMap,
}: {
  onRequestMap: (metric: "washout" | "subtraction") => Promise<PhaseMap | null>;
}) {
  const t = useTranslations("contrast");
  const [map, setMap] = useState<PhaseMap | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = (metric: "washout" | "subtraction") => {
    setBusy(true);
    setErr(null);
    onRequestMap(metric)
      .then((m) => (m ? setMap(m) : setErr(t("mapNeedsRoi"))))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  return (
    <div style={{ marginTop: 8, borderTop: "1px solid #1a1f2b", paddingTop: 6 }}>
      <div style={{ color: "#94a3b8", fontSize: "0.72rem", marginBottom: 3 }}>{t("mapTitle")}</div>
      <div style={{ display: "flex", gap: 4 }}>
        <button
          type="button"
          className="ghost"
          disabled={busy}
          onClick={() => load("washout")}
          style={{ flex: 1, fontSize: "0.72rem" }}
        >
          {t("mapWashout")}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={busy}
          onClick={() => load("subtraction")}
          style={{ flex: 1, fontSize: "0.72rem" }}
        >
          {t("mapSubtraction")}
        </button>
      </div>
      {busy && (
        <p className="meta" style={{ fontSize: "0.7rem" }}>
          {t("computing")}
        </p>
      )}
      {err && <p style={{ color: "var(--bv-warning, #fbbf24)", fontSize: "0.7rem" }}>{err}</p>}
      {map && (
        <div style={{ marginTop: 4 }}>
          <img
            src={`data:image/png;base64,${map.png_base64}`}
            alt={t("mapTitle")}
            style={{
              width: "100%",
              imageRendering: "pixelated",
              borderRadius: 4,
              background: "#11151c",
            }}
          />
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: "0.66rem",
              color: "#94a3b8",
              marginTop: 2,
            }}
          >
            <span>
              <span style={{ color: WASHOUT_COLOR }}>■</span> {t("legendWashout")} ·{" "}
              <span style={{ color: UPTAKE_COLOR }}>■</span> {t("legendUptake")}
            </span>
            <span>±{fmt(map.vabs, 0)} HU</span>
          </div>
        </div>
      )}
    </div>
  );
}

function WashoutTrendChip({ w }: { w: PhaseWashout }) {
  const t = useTranslations("contrast");
  if (w.enhanced_hu == null || w.delayed_hu == null) return null;
  const drop = w.enhanced_hu - w.delayed_hu;
  const washes = drop > 0;
  return (
    <div
      style={{
        display: "inline-block",
        margin: "0 0 4px",
        padding: "1px 7px",
        borderRadius: 10,
        fontSize: "0.72rem",
        fontWeight: 600,
        color: "#0b0e13",
        background: washes ? WASHOUT_COLOR : UPTAKE_COLOR,
      }}
    >
      {washes ? t("trendWashout") : t("trendPersistent")} {drop > 0 ? "−" : "+"}
      {fmt(Math.abs(drop), 0)} HU
    </div>
  );
}

function AdrenalIndices({ w, region }: { w: PhaseWashout; region: Region }) {
  const t = useTranslations("contrast");
  const adrenal = region === "adrenal";
  return (
    <>
      <dl
        style={{
          margin: "6px 0",
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "2px 8px",
        }}
      >
        {w.absolute_enhancement_hu != null && (
          <>
            <dt style={{ color: "#94a3b8" }}>{t("enhancement")}</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>
              {fmt(w.absolute_enhancement_hu, 0)} HU
            </dd>
          </>
        )}
        {w.apw != null && (
          <>
            <dt style={{ color: "#94a3b8" }}>APW</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>
              {fmt(w.apw, 1)}%{" "}
              {adrenal && w.apw_ge_60 && (
                <span style={{ color: "var(--bv-success, #34d399)" }}>≥60</span>
              )}
            </dd>
          </>
        )}
        {w.rpw != null && (
          <>
            <dt style={{ color: "#94a3b8" }}>RPW</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>
              {fmt(w.rpw, 1)}%{" "}
              {adrenal && w.rpw_ge_40 && (
                <span style={{ color: "var(--bv-success, #34d399)" }}>≥40</span>
              )}
            </dd>
          </>
        )}
      </dl>
      {adrenal && w.unenhanced_below_10hu && (
        <p style={{ color: "var(--bv-success, #34d399)", fontSize: "0.74rem" }}>{t("lipidRich")}</p>
      )}
      {!adrenal && (w.apw != null || w.rpw != null) && (
        <p className="meta" style={{ fontSize: "0.7rem" }}>
          {t("adrenalScopedNote")}
        </p>
      )}
    </>
  );
}

function WashoutCurve({
  curve,
  parenchyma,
}: {
  curve: Array<{ acquisition_phase: string; hu_mean: number }>;
  parenchyma: Array<{ acquisition_phase: string; hu_mean: number }>;
}) {
  const t = useTranslations("contrast");
  if (curve.length === 0) return null;
  const W = 250;
  const H = 80;
  const padX = 8;
  const padY = 10;
  const allHus = [...curve, ...parenchyma].map((p) => p.hu_mean);
  const lo = Math.min(...allHus);
  const hi = Math.max(...allHus);
  const span = hi - lo || 1;
  const n = curve.length;
  const x = (i: number) => (n === 1 ? W / 2 : padX + (i * (W - 2 * padX)) / (n - 1));
  const y = (hu: number) => H - padY - ((hu - lo) / span) * (H - 2 * padY);
  const line = (pts: Array<{ acquisition_phase: string; hu_mean: number }>) =>
    pts.map((p, i) => `${x(i)},${y(p.hu_mean)}`).join(" ");

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height={H}
      role="img"
      aria-label={t("curveLabel")}
      style={{ display: "block", background: "#11151c", borderRadius: 6 }}
    >
      {parenchyma.length > 0 && (
        <polyline
          points={line(parenchyma)}
          fill="none"
          stroke="#38bdf8"
          strokeWidth={1.2}
          strokeDasharray="3 2"
        />
      )}
      {curve.slice(1).map((p, idx) => {
        const prev = curve[idx];
        const down = p.hu_mean < prev.hu_mean;
        return (
          <line
            key={`seg-${prev.acquisition_phase}-${p.acquisition_phase}`}
            x1={x(idx)}
            y1={y(prev.hu_mean)}
            x2={x(idx + 1)}
            y2={y(p.hu_mean)}
            stroke={down ? WASHOUT_COLOR : UPTAKE_COLOR}
            strokeWidth={1.8}
          />
        );
      })}
      {curve.map((p, i) => (
        <g key={p.acquisition_phase}>
          <circle cx={x(i)} cy={y(p.hu_mean)} r={2.5} fill="#cbd5e1" />
          <text x={x(i)} y={H - 1} fontSize={7} fill="#94a3b8" textAnchor="middle">
            {t(`phase.${p.acquisition_phase}`).slice(0, 4)}
          </text>
        </g>
      ))}
    </svg>
  );
}
