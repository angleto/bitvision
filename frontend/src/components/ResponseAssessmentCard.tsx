"use client";

import {
  ApiError,
  type ResponseAssessment,
  type ResponseCategory,
  responseAssessmentsApi,
} from "@/lib/api";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

interface Props {
  patientId: string;
  /** When set, enables computing a fresh assessment at this follow-up study. */
  currentStudyId?: string;
  baselineStudyId?: string;
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
  return v === null || !Number.isFinite(v) ? "—" : `${v.toFixed(1)} mm`;
}

export default function ResponseAssessmentCard({
  patientId,
  currentStudyId,
  baselineStudyId,
}: Props) {
  const t = useTranslations("response");
  const [latest, setLatest] = useState<ResponseAssessment | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    responseAssessmentsApi
      .list(patientId, { limit: 1 })
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
  }, [patientId]);

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
            {latest.category}
          </span>
        )}
      </div>

      {err && <p className="error">{err}</p>}

      {latest ? (
        <>
          <p className="meta" style={{ marginTop: "var(--bv-s-2, 0.5rem)" }}>
            {t("criterion")}: {latest.criterion} · {t("targetSum")}: {fmtMm(latest.target_sum_mm)} (
            {t("baseline")}: {fmtMm(latest.baseline_sum_mm)}, {fmtPct(latest.target_sum_pct_change)}
            )
            {latest.new_lesions && (
              <>
                {" · "}
                <span style={{ color: "var(--bv-danger, #b91c1c)" }}>{t("newLesions")}</span>
              </>
            )}
          </p>
          <button type="button" className="ghost" disabled={busy} onClick={recompute}>
            {busy ? t("working") : t("recompute")}
          </button>
        </>
      ) : (
        <>
          <p className="meta" style={{ marginTop: "var(--bv-s-2, 0.5rem)" }}>
            {t("noneYet")}
          </p>
          <button type="button" className="ghost" disabled={busy} onClick={compute}>
            {busy ? t("working") : t("compute")}
          </button>
        </>
      )}
    </section>
  );
}
