"use client";

import {
  ApiError,
  type ResponseAssessment,
  type ResponseCategory,
  responseAssessmentsApi,
} from "@/lib/api";
import { type RecistLesion, readRecistBasis, recistRationale } from "@/lib/recist";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

interface Props {
  patientId: string;
  /** When set, enables computing a fresh assessment at this follow-up study. */
  currentStudyId?: string;
  baselineStudyId?: string;
  /** Bump to re-list after a target lesion is added/linked elsewhere. */
  refreshKey?: number;
}

const CATEGORY_COLOR: Record<ResponseCategory, string> = {
  CR: "var(--bv-success, #047857)",
  PR: "var(--bv-info, #1e40af)",
  SD: "var(--bv-muted, #64748b)",
  PD: "var(--bv-danger, #b91c1c)",
  NE: "var(--bv-muted, #64748b)",
};

function fmtPct(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toFixed(1)}%`;
}

function fmtMm(v: number | null): string {
  return v === null || !Number.isFinite(v) ? "—" : `${v.toFixed(1)}`;
}

function lesionPct(l: RecistLesion): number | null {
  if (l.baseline_mm === null || l.current_mm === null || l.baseline_mm === 0) return null;
  return ((l.current_mm - l.baseline_mm) / l.baseline_mm) * 100;
}

export default function ResponseAssessmentCard({
  patientId,
  currentStudyId,
  baselineStudyId,
  refreshKey,
}: Props) {
  const t = useTranslations("response");
  const [latest, setLatest] = useState<ResponseAssessment | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is a deliberate re-list trigger (bumped when a target lesion is added elsewhere), not read in the effect body.
  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    responseAssessmentsApi
      // Scoped to THIS study: an empty result means "no assessment for this
      // study yet", not the patient's latest assessment from another study.
      .list(
        patientId,
        currentStudyId ? { limit: 1, current_study_id: currentStudyId } : { limit: 1 },
      )
      .then((list) => {
        if (cancelled) return;
        setLatest(list[0] ?? null);
        setLoaded(true);
      })
      .catch((e) => {
        if (!cancelled) {
          setErr(e instanceof ApiError ? e.message : "load failed");
          setLoaded(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, currentStudyId, refreshKey]);

  async function compute() {
    if (!currentStudyId) return;
    setBusy(true);
    setErr(null);
    try {
      const ra = await responseAssessmentsApi.create(patientId, {
        current_study_id: currentStudyId,
        baseline_study_id: baselineStudyId,
        criterion: "recist_1_1",
      });
      setLatest(ra);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "compute failed");
    } finally {
      setBusy(false);
    }
  }

  async function recompute() {
    if (!latest) return;
    setBusy(true);
    setErr(null);
    try {
      setLatest(await responseAssessmentsApi.recompute(latest.id));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "recompute failed");
    } finally {
      setBusy(false);
    }
  }

  if (!loaded) return null;
  if (!latest && !currentStudyId) return null;

  const basis = latest ? readRecistBasis(latest.basis) : null;
  const hasNodal = basis?.lesions.some((l) => l.is_nodal) ?? false;

  return (
    <section className="card" aria-label={t("title")}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--bv-s-3, 0.75rem)",
        }}
      >
        <h3 style={{ margin: 0 }}>{t("title")}</h3>
        {latest && (
          <span
            className="badge"
            style={{
              color: CATEGORY_COLOR[latest.category],
              borderColor: CATEGORY_COLOR[latest.category],
              fontWeight: 700,
            }}
            title={t(`category.${latest.category}`)}
          >
            {latest.category} · {t(`category.${latest.category}`)}
          </span>
        )}
      </div>

      {err && <p className="error">{err}</p>}

      {!latest ? (
        // No assessment for this study yet.
        <>
          <p className="meta" style={{ marginTop: "var(--bv-s-2, 0.5rem)" }}>
            {t("explainer")}
          </p>
          <button type="button" className="ghost" disabled={busy} onClick={compute}>
            {busy ? t("working") : t("compute")}
          </button>
        </>
      ) : (
        <>
          {latest.category === "NE" ? (
            <p className="meta" style={{ marginTop: "var(--bv-s-2, 0.5rem)" }}>
              {t(`ne.${basis?.ne_reason ?? "unknown"}`)}
            </p>
          ) : (
            <p className="meta" style={{ marginTop: "var(--bv-s-2, 0.5rem)" }}>
              {(() => {
                const r = recistRationale(latest.category, {
                  targetSumMm: latest.target_sum_mm,
                  baselineSumMm: latest.baseline_sum_mm,
                  nadirSumMm: latest.nadir_sum_mm,
                  pctChange: latest.target_sum_pct_change,
                  newLesions: latest.new_lesions,
                });
                return t(`rationale.${r.key}`, { pct: fmtPct(r.pct) });
              })()}
            </p>
          )}

          {basis && basis.lesions.length > 0 && (
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: "0.78rem",
                marginTop: "var(--bv-s-2, 0.5rem)",
              }}
            >
              <thead>
                <tr style={{ textAlign: "left", color: "var(--bv-muted, #64748b)" }}>
                  <th style={cellLeft}>{t("targets.colTarget")}</th>
                  <th style={cellRight}>{t("targets.colBaseline")}</th>
                  <th style={cellRight}>{t("targets.colCurrent")}</th>
                  <th style={cellRight}>{t("targets.colPct")}</th>
                </tr>
              </thead>
              <tbody>
                {basis.lesions.map((l) => (
                  <tr
                    key={l.track_id}
                    style={{ borderTop: "1px solid var(--bv-card-border, #1a1f2b)" }}
                  >
                    <td style={cellLeft}>
                      {l.label}
                      {l.is_nodal && " *"}
                    </td>
                    <td style={cellRight}>{fmtMm(l.baseline_mm)}</td>
                    <td style={cellRight}>{fmtMm(l.current_mm)}</td>
                    <td style={cellRight}>{fmtPct(lesionPct(l))}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr
                  style={{ borderTop: "1px solid var(--bv-card-border, #1a1f2b)", fontWeight: 600 }}
                >
                  <td style={cellLeft}>{t("targetSum")}</td>
                  <td style={cellRight}>{fmtMm(latest.baseline_sum_mm)}</td>
                  <td style={cellRight}>{fmtMm(latest.target_sum_mm)}</td>
                  <td style={cellRight}>{fmtPct(latest.target_sum_pct_change)}</td>
                </tr>
              </tfoot>
            </table>
          )}

          {hasNodal && (
            <p className="meta" style={{ margin: "0.25rem 0 0", fontSize: "0.72rem" }}>
              {t("nodalFootnote")}
            </p>
          )}

          {latest.new_lesions && (
            <p
              className="meta"
              style={{ margin: "0.25rem 0 0", color: "var(--bv-danger, #b91c1c)" }}
            >
              {t("newLesions")}
            </p>
          )}

          {basis?.caps?.over_limit && (
            <p
              className="meta"
              style={{ margin: "0.25rem 0 0", color: "var(--bv-warning, #b45309)" }}
            >
              {t("caps.overLimit")}
            </p>
          )}

          <button
            type="button"
            className="ghost"
            disabled={busy}
            onClick={recompute}
            style={{ marginTop: "var(--bv-s-2, 0.5rem)" }}
          >
            {busy ? t("working") : t("recompute")}
          </button>
        </>
      )}
    </section>
  );
}

const cellLeft: React.CSSProperties = { padding: "2px 4px", textAlign: "left" };
const cellRight: React.CSSProperties = { padding: "2px 4px", textAlign: "right" };
