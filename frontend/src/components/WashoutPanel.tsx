"use client";

// Wash-out side panel for the multiphase contrast viewer. Renders the
// HU-vs-phase time-attenuation curve and the wash-out indices (APW/RPW)
// computed by POST /studies/{id}/phase-roi-stats for one ROI, plus a Save
// button that persists the measurement. The threshold flags are factual
// literature references (Korobkin 1998; Szolar 1998), NOT a diagnosis.

import type { PhaseRoiStats } from "@/lib/api";
import { useTranslations } from "next-intl";

export interface WashoutPanelProps {
  result: PhaseRoiStats | null;
  busy: boolean;
  error: string | null;
  saved: boolean;
  onSave: () => void;
  onClose: () => void;
}

function fmt(n: number | null | undefined, digits = 0): string {
  return n == null || Number.isNaN(n) ? "—" : n.toFixed(digits);
}

export default function WashoutPanel({
  result,
  busy,
  error,
  saved,
  onSave,
  onClose,
}: WashoutPanelProps) {
  const t = useTranslations("contrast");

  return (
    <aside
      style={{
        width: 280,
        flex: "0 0 auto",
        background: "#0b0e13",
        color: "#e6ecf3",
        borderLeft: "1px solid #1a1f2b",
        padding: "0.6rem 0.7rem",
        overflowY: "auto",
        fontSize: "0.8rem",
      }}
      aria-label={t("washoutTitle")}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <strong>{t("washoutTitle")}</strong>
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

      {busy && <p className="meta">{t("computing")}</p>}
      {error && <p style={{ color: "var(--bv-danger, #f87171)" }}>{error}</p>}
      {!busy && !error && !result && <p className="meta">{t("washoutHint")}</p>}

      {result && <WashoutBody result={result} />}

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
    </aside>
  );
}

function WashoutBody({ result }: { result: PhaseRoiStats }) {
  const t = useTranslations("contrast");
  const w = result.washout;
  const curve = w.curve;

  return (
    <div>
      <WashoutCurve curve={curve} />

      <table style={{ width: "100%", borderCollapse: "collapse", margin: "6px 0" }}>
        <tbody>
          {result.samples.map((s) => (
            <tr key={s.series_id}>
              <td style={{ color: "#94a3b8", padding: "1px 0" }}>
                {s.acquisition_phase ? t(`phase.${s.acquisition_phase}`) : t("unclassified")}
              </td>
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {fmt(s.hu_mean, 0)} ± {fmt(s.hu_std, 0)} HU
              </td>
            </tr>
          ))}
        </tbody>
      </table>

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
              {w.apw_ge_60 && <span style={{ color: "var(--bv-success, #34d399)" }}>≥60</span>}
            </dd>
          </>
        )}
        {w.rpw != null && (
          <>
            <dt style={{ color: "#94a3b8" }}>RPW</dt>
            <dd style={{ margin: 0, textAlign: "right" }}>
              {fmt(w.rpw, 1)}%{" "}
              {w.rpw_ge_40 && <span style={{ color: "var(--bv-success, #34d399)" }}>≥40</span>}
            </dd>
          </>
        )}
      </dl>

      {w.unenhanced_below_10hu && (
        <p style={{ color: "var(--bv-success, #34d399)", fontSize: "0.74rem" }}>{t("lipidRich")}</p>
      )}
      {result.skipped.length > 0 && (
        <p className="meta" style={{ fontSize: "0.72rem" }}>
          {t("skippedPhases", { count: result.skipped.length })}
        </p>
      )}
      <p className="meta" style={{ fontSize: "0.68rem", marginTop: 6, opacity: 0.8 }}>
        {t("notDiagnosis")}
      </p>
    </div>
  );
}

function WashoutCurve({ curve }: { curve: Array<{ acquisition_phase: string; hu_mean: number }> }) {
  const t = useTranslations("contrast");
  if (curve.length === 0) return null;
  const W = 250;
  const H = 90;
  const padX = 8;
  const padY = 10;
  const hus = curve.map((p) => p.hu_mean);
  const lo = Math.min(...hus);
  const hi = Math.max(...hus);
  const span = hi - lo || 1;
  const n = curve.length;
  const x = (i: number) => (n === 1 ? W / 2 : padX + (i * (W - 2 * padX)) / (n - 1));
  const y = (hu: number) => H - padY - ((hu - lo) / span) * (H - 2 * padY);
  const points = curve.map((p, i) => `${x(i)},${y(p.hu_mean)}`).join(" ");

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      height={H}
      role="img"
      aria-label={t("curveLabel")}
      style={{ display: "block", background: "#11151c", borderRadius: 6 }}
    >
      <polyline points={points} fill="none" stroke="#e96b1f" strokeWidth={1.5} />
      {curve.map((p, i) => (
        <g key={p.acquisition_phase}>
          <circle cx={x(i)} cy={y(p.hu_mean)} r={2.5} fill="#e96b1f" />
          <text x={x(i)} y={H - 1} fontSize={7} fill="#94a3b8" textAnchor="middle">
            {t(`phase.${p.acquisition_phase}`).slice(0, 4)}
          </text>
        </g>
      ))}
    </svg>
  );
}
