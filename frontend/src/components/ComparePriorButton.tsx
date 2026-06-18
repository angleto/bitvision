"use client";

import { ApiError, patientsApi, studiesApi } from "@/lib/api";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

interface Props {
  patientId: string;
  currentStudyId: string;
  /** A CT series of the current study — the follow-up side of the compare. */
  currentSeriesId?: string;
}

interface Prior {
  studyId: string;
  date: string;
  desc: string;
}

function asStr(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export default function ComparePriorButton({ patientId, currentStudyId, currentSeriesId }: Props) {
  const t = useTranslations("lesionTracks");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [priors, setPriors] = useState<Prior[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && priors === null) {
      setErr(null);
      try {
        // Scope to studies: the unscoped timeline mixes in markers /
        // documents / reports, sorts all by date and truncates to the
        // default page, so a data-rich patient with OLD studies has its
        // priors crowded out (every prior dropped -> false "no priors").
        const items = await patientsApi.timeline(patientId, {
          section: "studies",
          limit: 200,
        });
        const list: Prior[] = items
          .filter((it) => it.type === "study")
          .map((it) => ({
            studyId: asStr(it.data.id) || asStr(it.data.study_id),
            date: it.date,
            desc: asStr(it.data.study_description) || asStr(it.data.description),
          }))
          .filter((p) => p.studyId && p.studyId !== currentStudyId)
          .sort((a, b) => b.date.localeCompare(a.date));
        setPriors(list);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "load failed");
        setPriors([]);
      }
    }
  }

  async function openCompare(priorStudyId: string) {
    if (!currentSeriesId) return;
    setResolving(true);
    setErr(null);
    try {
      const detail = await studiesApi.detail(priorStudyId);
      const ct =
        detail.series.find((s) => (s.modality ?? "").toUpperCase() === "CT") ?? detail.series[0];
      if (!ct) {
        setErr(t("noPriors"));
        return;
      }
      router.push(`/viewer/followup?baseline=${ct.id}&followup=${currentSeriesId}`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    } finally {
      setResolving(false);
    }
  }

  if (!currentSeriesId) return null;

  return (
    <span style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        className="ghost"
        style={{ fontSize: "0.82rem" }}
        aria-expanded={open}
        onClick={() => void toggle()}
      >
        {t("comparePrior")} ▾
      </button>
      {open && (
        <div
          style={{
            position: "absolute",
            right: 0,
            top: "100%",
            zIndex: 10,
            minWidth: 240,
            maxHeight: 320,
            overflow: "auto",
            background: "var(--bv-card-bg, #fff)",
            border: "1px solid var(--bv-card-border, #e5e7eb)",
            borderRadius: "var(--bv-r-md, 10px)",
            boxShadow: "var(--bv-shadow-2)",
            padding: "var(--bv-s-2, 0.5rem)",
          }}
        >
          {err && <p className="error">{err}</p>}
          {priors === null && <p className="meta">{t("loading")}</p>}
          {priors && priors.length === 0 && <p className="meta">{t("noPriors")}</p>}
          {priors?.map((p) => (
            <button
              key={p.studyId}
              type="button"
              className="ghost"
              disabled={resolving}
              onClick={() => void openCompare(p.studyId)}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "var(--bv-s-1, 0.25rem) var(--bv-s-2, 0.5rem)",
              }}
            >
              {t("priorOf", { date: (p.date ?? "").slice(0, 10), desc: p.desc || "—" })}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
