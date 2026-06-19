"use client";

// Follow-up comparison viewer: baseline + follow-up side by side with a
// registration-aware synchronised crosshair. When the two studies share a
// frame of reference, the crosshair maps directly (world→world); otherwise
// the user runs "Align" (a rigid registration) and the stored 4x4 LPS
// matrix maps the world point across frames so both panes track the same
// anatomy. Reuses ``CornerstoneMPRLayout`` (one ref per pane).

import ComparePicker from "@/components/ComparePicker";
import RecistTargetsPanel, { type DrawnMeasurement } from "@/components/RecistTargetsPanel";
import type { VolumeData } from "@/components/VolumeViewer";
import { useWorldSyncGrid } from "@/hooks/useWorldSyncGrid";
import { type Mat4, isMat4 } from "@/lib/affine";
import { ApiError, type Series, fetchVolume, registrationsApi, studiesApi } from "@/lib/api";
import {
  type MatchReason,
  type PrimaryPlane,
  matchConfidence,
  seriesOptionLabel,
} from "@/lib/seriesMatch";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
});

interface PaneState {
  seriesId: string;
  series: Series | null;
  volume: VolumeData | null;
  err: string | null;
}

function useSeriesPane(seriesId: string | null, enabled: boolean): PaneState {
  const [pane, setPane] = useState<PaneState>({
    seriesId: seriesId ?? "",
    series: null,
    volume: null,
    err: null,
  });
  useEffect(() => {
    // Selection-first: never fetch the (multi-GB) volume until the comparison
    // is confirmed — either a confident medical default auto-loaded, or an
    // explicit user pick. On the throttled prod egress an eager fetch of a
    // wrongly-guessed series is minutes of wasted transfer.
    if (!seriesId || !enabled) return;
    let cancelled = false;
    setPane({ seriesId, series: null, volume: null, err: null });
    void (async () => {
      try {
        const series = await studiesApi.series(seriesId);
        const { header, scalars } = await fetchVolume(seriesId);
        if (cancelled) return;
        setPane({
          seriesId,
          series,
          err: null,
          volume: {
            dimensions: [header.nx, header.ny, header.nz],
            spacing: header.spacing,
            scalars,
            range: header.valueRange,
            origin: header.origin,
            direction: header.direction,
            frameOfReferenceUid: header.frameOfReferenceUid,
          },
        });
      } catch (e) {
        if (!cancelled) {
          setPane((p) => ({ ...p, err: e instanceof ApiError ? e.message : String(e) }));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [seriesId, enabled]);
  return pane;
}

// Resolves the SIBLING series of the study a pane is showing, so the pane can
// offer a series selector. Keyed on the study (not the series) so switching
// series within the same study does not refetch the list. Acquisition plane is
// resolved per series for medical labeling/matching (progressive; a per-series
// failure degrades to "unknown" rather than blocking the picker).
function useStudySeries(seriesId: string | null): {
  list: Series[];
  planeOf: (id: string) => PrimaryPlane | null;
  ready: boolean;
  study: { study_date: string | null; study_description: string | null } | null;
  studyId: string | null;
  patientId: string | null;
} {
  const [studyId, setStudyId] = useState<string | null>(null);
  const [patientId, setPatientId] = useState<string | null>(null);
  const [list, setList] = useState<Series[]>([]);
  const [planes, setPlanes] = useState<Record<string, PrimaryPlane>>({});
  const [ready, setReady] = useState(false);
  const [study, setStudy] = useState<{
    study_date: string | null;
    study_description: string | null;
  } | null>(null);

  useEffect(() => {
    if (!seriesId) return;
    let cancelled = false;
    void (async () => {
      try {
        const s = await studiesApi.series(seriesId);
        if (!cancelled) setStudyId(s.study_id);
      } catch {
        /* the pane surfaces its own load error */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [seriesId]);

  useEffect(() => {
    if (!studyId) return;
    let cancelled = false;
    setReady(false);
    void (async () => {
      try {
        const detail = await studiesApi.detail(studyId);
        if (cancelled) return;
        setList(detail.series);
        setPatientId(detail.patient_id);
        setStudy({
          study_date: detail.study_date,
          study_description: detail.study_description,
        });
        const entries = await Promise.all(
          detail.series.map(async (s) => {
            try {
              const dm = await studiesApi.displayMetadata(s.id);
              return [s.id, dm.primary_plane] as const;
            } catch {
              return [s.id, "unknown" as PrimaryPlane] as const;
            }
          }),
        );
        if (!cancelled) setPlanes(Object.fromEntries(entries));
      } catch {
        /* selector just won't populate */
      } finally {
        // "ready" == we resolved this study's series + attempted planes, so
        // the auto-decision can run on the full list rather than a partial one.
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [studyId]);

  return { list, planeOf: (id) => planes[id] ?? null, ready, study, studyId, patientId };
}

export default function FollowupComparePage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <FollowupCompareInner />
    </Suspense>
  );
}

function FollowupCompareInner() {
  const t = useTranslations("compare");
  const search = useSearchParams();
  const [baselineId, setBaselineId] = useState<string | null>(
    () => search.get("baseline") ?? search.getAll("s")[0] ?? null,
  );
  const [followupId, setFollowupId] = useState<string | null>(
    () => search.get("followup") ?? search.getAll("s")[1] ?? null,
  );

  // Selection-first gate: the panes load only once `confirmed` (a confident
  // medical default auto-confirms; otherwise the user confirms in the picker).
  const [confirmed, setConfirmed] = useState(false);
  const [autoDecided, setAutoDecided] = useState(false);
  const [suggestedBaselineId, setSuggestedBaselineId] = useState<string | null>(null);
  const [suggestedFollowupId, setSuggestedFollowupId] = useState<string | null>(null);
  const [pickReason, setPickReason] = useState<MatchReason | null>(null);

  const base = useSeriesPane(baselineId, confirmed);
  const follow = useSeriesPane(followupId, confirmed);
  const baseStudy = useStudySeries(baselineId);
  const followStudy = useStudySeries(followupId);

  const grid = useWorldSyncGrid();
  const [matrix, setMatrix] = useState<Mat4 | null>(null);
  const [registrationId, setRegistrationId] = useState<string | null>(null);
  const [aligning, setAligning] = useState(false);
  const [alignErr, setAlignErr] = useState<string | null>(null);

  // RECIST authoring: bidirectional measure mode + the target panel, plus
  // the completed measurements emitted by each pane.
  const [measureMode, setMeasureMode] = useState(false);
  const [recistOpen, setRecistOpen] = useState(false);
  const [baseMeas, setBaseMeas] = useState<DrawnMeasurement[]>([]);
  const [followMeas, setFollowMeas] = useState<DrawnMeasurement[]>([]);
  // RECIST is per-patient: only offer it when both panes are the same patient.
  const sharedPatientId =
    baseStudy.patientId && baseStudy.patientId === followStudy.patientId
      ? baseStudy.patientId
      : null;

  // Keep the URL in sync (shareable / survives refresh) without remounting:
  // after load, component state — not the URL — is the source of truth.
  function syncUrl(b: string | null, f: string | null) {
    if (typeof window === "undefined") return;
    const qs = new URLSearchParams();
    if (b) qs.set("baseline", b);
    if (f) qs.set("followup", f);
    window.history.replaceState(null, "", `${window.location.pathname}?${qs.toString()}`);
  }
  // Picking a different series on either side invalidates the previous rigid
  // registration, so drop the matrix; the pane reloads and sameFoR / Align
  // recompute for the new pair.
  function pickBaseline(id: string) {
    if (!id || id === baselineId) return;
    setBaselineId(id);
    setMatrix(null);
    setRegistrationId(null);
    setBaseMeas([]);
    setAlignErr(null);
    syncUrl(id, followupId);
  }
  function pickFollowup(id: string) {
    if (!id || id === followupId) return;
    setFollowupId(id);
    setMatrix(null);
    setRegistrationId(null);
    setFollowMeas([]);
    setAlignErr(null);
    syncUrl(baselineId, id);
  }
  function planeLabel(p: PrimaryPlane | null): string | null {
    return p && p !== "unknown" ? t(`plane.${p}`) : null;
  }

  // Auto-decide once both studies' series + acquisition planes are resolved.
  // The follow-up's incoming series (what the user was viewing) is the medical
  // reference; the baseline is matched against it. If there is a confident
  // default — a single comparable series, or one clear plane/phrasing winner —
  // load that pair immediately. Otherwise pre-select the best guess and show
  // the picker, but DO NOT fetch volumes until the user confirms: an ambiguous
  // choice (e.g. two axial CTs differing only by contrast phase) is a medical
  // decision, not something to guess. Runs once (autoDecided latch).
  // biome-ignore lint/correctness/useExhaustiveDependencies: latch on autoDecided; run when both sides ready
  useEffect(() => {
    if (autoDecided || confirmed) return;
    if (!baselineId || !followupId) return;
    if (!baseStudy.ready || !followStudy.ready) return;
    setSuggestedFollowupId(followupId);
    const reference = followStudy.list.find((s) => s.id === followupId) ?? null;
    if (!reference) {
      // No reference series to match against — let the user choose.
      setAutoDecided(true);
      return;
    }
    const conf = matchConfidence(baseStudy.list, reference, baseStudy.planeOf);
    setSuggestedBaselineId(conf.best?.id ?? null);
    setPickReason(conf.reason);
    const followIsVolume = (reference.received_instance_count ?? 0) > 3;
    if (conf.confident && conf.best && followIsVolume) {
      if (conf.best.id !== baselineId) {
        setBaselineId(conf.best.id);
        syncUrl(conf.best.id, followupId);
      }
      setConfirmed(true);
    } else if (conf.best && conf.best.id !== baselineId) {
      // Pre-select the best guess in the picker; still no load.
      setBaselineId(conf.best.id);
      syncUrl(conf.best.id, followupId);
    }
    setAutoDecided(true);
  }, [autoDecided, confirmed, baselineId, followupId, baseStudy.ready, followStudy.ready]);

  const sameFoR = useMemo(() => {
    const a = base.volume?.frameOfReferenceUid;
    const b = follow.volume?.frameOfReferenceUid;
    return !!a && !!b && a === b;
  }, [base.volume, follow.volume]);

  // Pane 0 (baseline) is the reference; pane 1 (follow-up) maps
  // reference-world -> pane-world via the registration matrix
  // (baseline=fixed -> follow-up=moving), or identity when the two share a
  // frame of reference. Until aligned, a differing-frame pair keeps the
  // prior identity-sync behaviour (matrix ?? null). grid is stable.
  // biome-ignore lint/correctness/useExhaustiveDependencies: grid is stable; key on sameFoR/matrix
  useEffect(() => {
    grid.setTransform(0, null);
    grid.setTransform(1, sameFoR ? null : (matrix ?? null));
  }, [sameFoR, matrix]);

  async function align() {
    if (!baselineId || !followupId) return;
    setAligning(true);
    setAlignErr(null);
    try {
      const reg = await registrationsApi.create({
        fixed_series_id: baselineId,
        moving_series_id: followupId,
        kind: "rigid",
      });
      // Poll until terminal (registration runs on a worker).
      for (let i = 0; i < 60; i++) {
        const cur = await registrationsApi.get(reg.id);
        if (cur.status === "succeeded") {
          const m = cur.result_meta?.lps_matrix;
          if (isMat4(m)) {
            setMatrix(m);
            setRegistrationId(reg.id);
          } else setAlignErr(t("noMatrix"));
          return;
        }
        if (cur.status === "failed" || cur.status === "cancelled") {
          setAlignErr(cur.error ?? t("alignFailed"));
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      setAlignErr(t("alignTimeout"));
    } catch (e) {
      setAlignErr(e instanceof ApiError ? e.message : t("alignFailed"));
    } finally {
      setAligning(false);
    }
  }

  if (!baselineId || !followupId) {
    return (
      <main style={{ padding: "2rem" }}>
        <Link href="/studies">← studies</Link>
        <h1>{t("title")}</h1>
        <p className="meta">{t("pickHint")}</p>
      </main>
    );
  }

  // Selection-first gate: until the comparison is confirmed, show the picker
  // (or an "analyzing" placeholder while the series lists resolve). No volume
  // is fetched here — useSeriesPane stays disabled while `confirmed` is false.
  if (!confirmed) {
    const ready = baseStudy.ready && followStudy.ready;
    return (
      <div
        style={{
          height: "calc(100vh - 56px)",
          background: "#000",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          style={{
            flex: "0 0 auto",
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            padding: "0.4rem 0.75rem",
            background: "var(--bv-card-bg, #11151c)",
            color: "var(--bv-fg, #e6ecf3)",
            borderBottom: "1px solid var(--bv-card-border, #1a1f2b)",
          }}
        >
          <strong style={{ fontSize: "0.9rem" }}>{t("title")}</strong>
        </div>
        {ready ? (
          <ComparePicker
            baseline={baseStudy}
            followup={followStudy}
            baselineId={baselineId}
            followupId={followupId}
            suggestedBaselineId={suggestedBaselineId}
            suggestedFollowupId={suggestedFollowupId}
            reason={pickReason}
            onPickBaseline={pickBaseline}
            onPickFollowup={pickFollowup}
            onConfirm={() => setConfirmed(true)}
          />
        ) : (
          <div
            style={{
              flex: "1 1 auto",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#94a3b8",
              fontSize: "0.85rem",
            }}
          >
            {t("pick.analyzing")}
          </div>
        )}
      </div>
    );
  }

  const alignState = sameFoR ? "sameFrame" : matrix ? "registered" : "unaligned";
  const alignColor =
    alignState === "unaligned" ? "var(--bv-warning, #b45309)" : "var(--bv-success, #047857)";

  return (
    <div
      style={{
        height: "calc(100vh - 56px)",
        background: "#000",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          flex: "0 0 auto",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          padding: "0.4rem 0.75rem",
          background: "var(--bv-card-bg, #11151c)",
          color: "var(--bv-fg, #e6ecf3)",
          borderBottom: "1px solid var(--bv-card-border, #1a1f2b)",
          flexWrap: "wrap",
        }}
      >
        <strong style={{ fontSize: "0.9rem" }}>{t("title")}</strong>
        <label
          style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.82rem" }}
        >
          <input
            type="checkbox"
            checked={grid.sync}
            onChange={(e) => grid.setSync(e.target.checked)}
          />
          {t("sync")}
        </label>
        <span
          className="badge"
          style={{ color: alignColor, borderColor: alignColor, whiteSpace: "nowrap" }}
        >
          {t(`align.${alignState}`)}
        </span>
        {!sameFoR && (
          <button type="button" className="ghost" disabled={aligning} onClick={align}>
            {aligning ? t("aligning") : t("alignButton")}
          </button>
        )}
        {alignErr && <span style={{ color: "var(--bv-danger, #f87171)" }}>{alignErr}</span>}
        <span style={{ flex: "1 1 auto" }} />
        <button
          type="button"
          className="ghost"
          aria-pressed={measureMode}
          style={measureMode ? { borderColor: "var(--bv-info, #1e40af)" } : undefined}
          onClick={() => setMeasureMode((v) => !v)}
        >
          {measureMode ? t("recist.measuring") : t("recist.measure")}
        </button>
        <button
          type="button"
          className="ghost"
          aria-pressed={recistOpen}
          disabled={!sharedPatientId}
          title={!sharedPatientId ? t("recist.differentPatients") : undefined}
          onClick={() => setRecistOpen((v) => !v)}
        >
          {t("recist.panelToggle")}
        </button>
      </div>

      <div style={{ flex: "1 1 auto", display: "flex", minHeight: 0 }}>
        <div
          style={{
            flex: "1 1 auto",
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 2,
            minWidth: 0,
          }}
        >
          {[
            {
              pane: base,
              idx: 0,
              label: t("baseline"),
              study: baseStudy,
              selectedId: baselineId,
              onPick: pickBaseline,
            },
            {
              pane: follow,
              idx: 1,
              label: t("followup"),
              study: followStudy,
              selectedId: followupId,
              onPick: pickFollowup,
            },
          ].map(({ pane, idx, label, study, selectedId, onPick }) => (
            <div
              key={label}
              style={{
                position: "relative",
                background: "#000",
                border: "1px solid #1a1f2b",
                display: "grid",
                gridTemplateRows: "auto 1fr",
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "0.25rem 0.5rem",
                  fontSize: "0.78rem",
                  color: "#cbd5e1",
                  background: "#0b0e13",
                  borderBottom: "1px solid #1a1f2b",
                }}
              >
                <span style={{ color: "#94a3b8", whiteSpace: "nowrap" }}>{label}</span>
                {study.list.length > 0 ? (
                  <select
                    aria-label={t("selectSeries")}
                    value={selectedId ?? ""}
                    onChange={(e) => onPick(e.target.value)}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      background: "#0b0e13",
                      color: "#e6ecf3",
                      border: "1px solid #1a1f2b",
                      borderRadius: 6,
                      padding: "2px 6px",
                      fontSize: "0.76rem",
                    }}
                  >
                    {study.list.map((s) => (
                      <option key={s.id} value={s.id}>
                        {seriesOptionLabel(s, planeLabel(study.planeOf(s.id)))}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span
                    style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  >
                    {pane.series?.series_description ?? "…"}
                  </span>
                )}
              </div>
              {pane.err ? (
                <div className="error" style={{ padding: "1rem" }}>
                  {pane.err}
                </div>
              ) : pane.volume ? (
                <CornerstoneMPRLayout
                  ref={grid.registerPane(idx)}
                  volume={pane.volume}
                  showAxial
                  showSagittal
                  showCoronal
                  show3D={false}
                  layout="2x2"
                  seriesId={pane.seriesId}
                  modality={pane.series?.modality ?? null}
                  seriesDescription={pane.series?.series_description ?? undefined}
                  activeTool={measureMode ? "measure-bidirectional" : "wl"}
                  onMeasurementsChange={(m) => (idx === 0 ? setBaseMeas(m) : setFollowMeas(m))}
                  onCrosshairChange={(pos) => grid.onCrosshairChange(idx, pos)}
                />
              ) : (
                <div style={{ color: "#6b7280", padding: "1rem" }}>loading…</div>
              )}
            </div>
          ))}
        </div>
        {recistOpen && sharedPatientId && baseStudy.studyId && followStudy.studyId && (
          <RecistTargetsPanel
            patientId={sharedPatientId}
            baseline={{
              studyId: baseStudy.studyId,
              seriesId: base.seriesId,
              frameOfReferenceUID: base.volume?.frameOfReferenceUid,
              studyDate: baseStudy.study?.study_date,
              measurements: baseMeas,
            }}
            followup={{
              studyId: followStudy.studyId,
              seriesId: follow.seriesId,
              frameOfReferenceUID: follow.volume?.frameOfReferenceUid,
              studyDate: followStudy.study?.study_date,
              measurements: followMeas,
            }}
            registrationId={registrationId}
            onClose={() => setRecistOpen(false)}
          />
        )}
      </div>
    </div>
  );
}
