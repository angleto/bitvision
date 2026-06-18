"use client";

import { ApiError, type LesionTrack, type LesionTrajectory, lesionTracksApi } from "@/lib/api";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import Sparkline from "./Sparkline";

interface Props {
  patientId: string;
  /** When set, each track with a baseline offers a "propagate here" action. */
  followupSeriesId?: string;
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toFixed(1)}%`;
}

function growthColor(dir: string | undefined): string {
  if (dir === "increase") return "var(--bv-danger, #b91c1c)";
  if (dir === "decrease") return "var(--bv-success, #047857)";
  return "var(--bv-muted, #64748b)";
}

export default function LesionTracksPanel({ patientId, followupSeriesId }: Props) {
  const t = useTranslations("lesionTracks");
  const [tracks, setTracks] = useState<LesionTrack[] | null>(null);
  const [traj, setTraj] = useState<Record<string, LesionTrajectory>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [createBusy, setCreateBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    setTracks(null);
    lesionTracksApi
      .list(patientId)
      .then(async (list) => {
        if (cancelled) return;
        setTracks(list);
        const entries = await Promise.all(
          list.map(async (tr) => {
            try {
              return [tr.id, await lesionTracksApi.trajectory(tr.id)] as const;
            } catch {
              return null;
            }
          }),
        );
        if (cancelled) return;
        setTraj(Object.fromEntries(entries.filter((e): e is NonNullable<typeof e> => !!e)));
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : "load failed");
      });
    return () => {
      cancelled = true;
    };
  }, [patientId]);

  async function propagate(track: LesionTrack) {
    if (!followupSeriesId) return;
    setBusy(track.id);
    setNotice(null);
    try {
      await lesionTracksApi.propagate(track.id, {
        followup_series_id: followupSeriesId,
        refine: true,
      });
      setNotice(t("propagateQueued", { label: track.label }));
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "propagate failed");
    } finally {
      setBusy(null);
    }
  }

  async function createTrack() {
    const label = newLabel.trim();
    if (!label) return;
    setCreateBusy(true);
    try {
      const created = await lesionTracksApi.create(patientId, { label });
      setTracks((prev) => [created, ...(prev ?? [])]);
      setNewLabel("");
      setCreating(false);
    } catch (e) {
      setNotice(e instanceof ApiError ? e.message : "create failed");
    } finally {
      setCreateBusy(false);
    }
  }

  return (
    <section className="card" aria-label={t("title")}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "var(--bv-s-2, 0.5rem)",
          marginBottom: "var(--bv-s-2, 0.5rem)",
        }}
      >
        <h3 style={{ margin: 0 }}>{t("title")}</h3>
        <button type="button" className="ghost" onClick={() => setCreating((v) => !v)}>
          {creating ? t("cancel") : t("newLesion")}
        </button>
      </div>
      {creating && (
        <div
          style={{
            display: "flex",
            gap: "var(--bv-s-2, 0.5rem)",
            marginBottom: "var(--bv-s-2, 0.5rem)",
          }}
        >
          <input
            type="text"
            value={newLabel}
            placeholder={t("labelPlaceholder")}
            aria-label={t("labelPlaceholder")}
            onChange={(e) => setNewLabel(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void createTrack();
            }}
            style={{ flex: 1 }}
          />
          <button
            type="button"
            className="ghost"
            disabled={createBusy || !newLabel.trim()}
            onClick={() => void createTrack()}
          >
            {createBusy ? t("creating") : t("create")}
          </button>
        </div>
      )}
      {err && <p className="error">{err}</p>}
      {notice && <p className="meta">{notice}</p>}
      {!tracks && !err && <p className="meta">{t("loading")}</p>}
      {tracks && tracks.length === 0 && <p className="meta">{t("empty")}</p>}
      {tracks?.map((track) => {
        const tj = traj[track.id];
        const summary = tj?.summary ?? null;
        const volumes = (tj?.timepoints ?? [])
          .map((p) => p.volume_ml)
          .filter((v): v is number => v !== null && Number.isFinite(v));
        const dir = summary?.overall_direction;
        const hasBaseline = track.points.some((p) => p.is_baseline);
        return (
          <div
            key={track.id}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "var(--bv-s-3, 0.75rem)",
              padding: "var(--bv-s-2, 0.5rem) 0",
              borderTop: "1px solid var(--bv-divider, #eef0f3)",
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600 }}>{track.label}</div>
              <div className="meta" style={{ fontSize: "0.8rem" }}>
                {[track.type, track.anatomy, track.laterality].filter(Boolean).join(" · ") ||
                  t("uncoded")}
                {" · "}
                {t("timepoints", { n: track.points.length })}
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--bv-s-3, 0.75rem)" }}>
              {volumes.length > 0 && (
                <Sparkline
                  values={volumes}
                  stroke={growthColor(dir)}
                  ariaLabel={t("trendAria", { label: track.label })}
                />
              )}
              <span
                className="badge"
                title={t("volumeChange")}
                style={{
                  color: growthColor(dir),
                  borderColor: growthColor(dir),
                  whiteSpace: "nowrap",
                }}
              >
                {fmtPct(summary?.volume_pct_change_total)}
              </span>
              {followupSeriesId && hasBaseline && (
                <button
                  type="button"
                  className="ghost"
                  disabled={busy === track.id}
                  onClick={() => propagate(track)}
                >
                  {busy === track.id ? t("propagating") : t("propagate")}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </section>
  );
}
