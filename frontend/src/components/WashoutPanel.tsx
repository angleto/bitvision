"use client";

// Wash-out side panel for the multiphase contrast viewer. Region-aware:
//
//  * adrenal — the APW/RPW model the thresholds were validated on (Korobkin
//    1998; Szolar 1998). Shows the indices + adenoma flags (factual, NOT a
//    diagnosis).
//  * liver   — APW/RPW do NOT apply; liver wash-out is qualitative, relative
//    to parenchyma (LI-RADS). Shows the lesion-vs-parenchyma ΔHU per phase
//    from a second reference ROI, and withholds the adrenal flags.
//  * other   — raw indices, clearly labelled as adrenal-derived, no verdict.
//
// Plus reading-safety guardrails: a warning when the enhanced (arterial /
// portal) phase is missing (APW/RPW not computable) and a per-phase warning
// when an ROI's HU std-dev is high (the ROI likely straddles a boundary /
// fat / air rather than sitting on homogeneous tissue).

import type { PhaseRoiStats, PhaseWashout } from "@/lib/api";
import { useTranslations } from "next-intl";

type Region = "adrenal" | "liver" | "other";
type RoiTarget = "lesion" | "parenchyma";

// HU std-dev above which a soft-tissue ROI is flagged as heterogeneous: a
// clean parenchyma / lesion ROI sits around 10-20 HU; ~90 HU means the sphere
// is spanning fat / air / vessel / a boundary and the mean is meaningless.
const HIGH_SD_HU = 25;

export interface WashoutPanelProps {
  result: PhaseRoiStats | null;
  busy: boolean;
  error: string | null;
  saved: boolean;
  region: Region;
  onRegionChange: (r: Region) => void;
  roiTarget: RoiTarget;
  onRoiTargetChange: (t: RoiTarget) => void;
  hasLesion: boolean;
  hasParenchyma: boolean;
  onSave: () => void;
  onClose: () => void;
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
  hasLesion,
  hasParenchyma,
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

      {/* Region selector — drives the interpretation, server-side. */}
      <div style={{ marginTop: 6 }}>
        <div style={{ color: "#94a3b8", fontSize: "0.72rem", marginBottom: 2 }}>
          {t("regionLabel")}
        </div>
        <fieldset
          aria-label={t("regionLabel")}
          style={{ display: "flex", gap: 4, border: 0, padding: 0, margin: 0 }}
        >
          {REGIONS.map((r) => (
            <button
              key={r}
              type="button"
              className="ghost"
              aria-pressed={region === r}
              onClick={() => onRegionChange(r)}
              style={{
                flex: 1,
                fontSize: "0.72rem",
                padding: "2px 4px",
                border: region === r ? "1px solid #e96b1f" : "1px solid #1a1f2b",
                color: region === r ? "#e96b1f" : "#cbd5e1",
                borderRadius: 4,
              }}
            >
              {t(`region.${r}`)}
            </button>
          ))}
        </fieldset>
      </div>

      {/* Liver workflow: choose which ROI the next drawn circle fills. */}
      {region === "liver" && (
        <div style={{ marginTop: 6 }}>
          <div style={{ color: "#94a3b8", fontSize: "0.72rem", marginBottom: 2 }}>
            {t("roiTargetLabel")}
          </div>
          <fieldset style={{ display: "flex", gap: 4, border: 0, padding: 0, margin: 0 }}>
            {(["lesion", "parenchyma"] as RoiTarget[]).map((target) => {
              const filled = target === "lesion" ? hasLesion : hasParenchyma;
              return (
                <button
                  key={target}
                  type="button"
                  className="ghost"
                  aria-pressed={roiTarget === target}
                  onClick={() => onRoiTargetChange(target)}
                  style={{
                    flex: 1,
                    fontSize: "0.72rem",
                    padding: "2px 4px",
                    border: roiTarget === target ? "1px solid #e96b1f" : "1px solid #1a1f2b",
                    color: roiTarget === target ? "#e96b1f" : "#cbd5e1",
                    borderRadius: 4,
                  }}
                >
                  {filled ? "✓ " : ""}
                  {t(`roiTarget.${target}`)}
                </button>
              );
            })}
          </fieldset>
          {!hasParenchyma && (
            <p className="meta" style={{ fontSize: "0.7rem", marginTop: 4 }}>
              {t("drawParenchymaHint")}
            </p>
          )}
        </div>
      )}

      {busy && <p className="meta">{t("computing")}</p>}
      {error && <p style={{ color: "var(--bv-danger, #f87171)" }}>{error}</p>}
      {!busy && !error && !result && <p className="meta">{t("washoutHint")}</p>}

      {result && <WashoutBody result={result} region={region} />}

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

function WashoutBody({ result, region }: { result: PhaseRoiStats; region: Region }) {
  const t = useTranslations("contrast");
  const w: PhaseWashout = result.washout;

  const heterogeneous = result.samples.filter((s) => s.hu_std > HIGH_SD_HU);
  // The enhanced (arterial/portal) peak is what APW/RPW are measured against.
  // For adrenal/other its absence makes the indices uncomputable; for liver
  // the relative read still works, but it is worth flagging either way.
  const missingEnhanced = w.enhanced_phase == null;

  return (
    <div>
      <WashoutCurve curve={w.curve} parenchyma={region === "liver" ? w.parenchyma_curve : []} />

      <table style={{ width: "100%", borderCollapse: "collapse", margin: "6px 0" }}>
        <tbody>
          {result.samples.map((s) => {
            const hot = s.hu_std > HIGH_SD_HU;
            return (
              <tr key={s.series_id}>
                <td style={{ color: "#94a3b8", padding: "1px 0" }}>
                  {s.acquisition_phase ? t(`phase.${s.acquisition_phase}`) : t("unclassified")}
                </td>
                <td
                  style={{
                    textAlign: "right",
                    fontVariantNumeric: "tabular-nums",
                    color: hot ? "var(--bv-warning, #fbbf24)" : undefined,
                  }}
                >
                  {hot && (
                    <span title={t("highSdWarn")} style={{ marginRight: 3 }}>
                      ⚠
                    </span>
                  )}
                  {fmt(s.hu_mean, 0)} ± {fmt(s.hu_std, 0)} HU
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {heterogeneous.length > 0 && (
        <p style={{ color: "var(--bv-warning, #fbbf24)", fontSize: "0.72rem" }}>
          {t("highSdWarn")}
        </p>
      )}

      {/* Liver: lesion-vs-parenchyma relative wash-out (the LI-RADS signal). */}
      {region === "liver" ? <LiverRelative w={w} /> : <AdrenalIndices w={w} region={region} />}

      {missingEnhanced && (
        <p style={{ color: "var(--bv-warning, #fbbf24)", fontSize: "0.72rem", marginTop: 4 }}>
          {t("missingEnhanced")}
        </p>
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
      {/* The indices ARE the adrenal model; say so for any non-adrenal region. */}
      {!adrenal && (w.apw != null || w.rpw != null) && (
        <p className="meta" style={{ fontSize: "0.7rem" }}>
          {t("adrenalScopedNote")}
        </p>
      )}
    </>
  );
}

function LiverRelative({ w }: { w: PhaseWashout }) {
  const t = useTranslations("contrast");
  const rel = w.relative_curve;
  return (
    <div style={{ margin: "6px 0" }}>
      <div style={{ color: "#94a3b8", fontSize: "0.72rem", marginBottom: 2 }}>
        {t("liverRelativeTitle")}
      </div>
      {rel.length === 0 ? (
        <p className="meta" style={{ fontSize: "0.7rem" }}>
          {t("drawParenchymaHint")}
        </p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: "#64748b", fontSize: "0.68rem" }}>
              <th style={{ textAlign: "left", fontWeight: 400 }} />
              <th style={{ textAlign: "right", fontWeight: 400 }}>{t("colLesion")}</th>
              <th style={{ textAlign: "right", fontWeight: 400 }}>{t("colLiver")}</th>
              <th style={{ textAlign: "right", fontWeight: 400 }}>Δ</th>
            </tr>
          </thead>
          <tbody style={{ fontVariantNumeric: "tabular-nums" }}>
            {rel.map((r) => (
              <tr key={r.acquisition_phase}>
                <td style={{ color: "#94a3b8" }}>{t(`phase.${r.acquisition_phase}`)}</td>
                <td style={{ textAlign: "right" }}>{fmt(r.lesion_hu, 0)}</td>
                <td style={{ textAlign: "right" }}>{fmt(r.parenchyma_hu, 0)}</td>
                <td
                  style={{
                    textAlign: "right",
                    color: r.delta_hu < 0 ? "var(--bv-accent, #e96b1f)" : undefined,
                  }}
                >
                  {r.delta_hu > 0 ? "+" : ""}
                  {fmt(r.delta_hu, 0)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="meta" style={{ fontSize: "0.68rem", marginTop: 4 }}>
        {t("liverNote")}
      </p>
    </div>
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
  const H = 90;
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
      <polyline points={line(curve)} fill="none" stroke="#e96b1f" strokeWidth={1.5} />
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
