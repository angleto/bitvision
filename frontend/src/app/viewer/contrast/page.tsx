"use client";

// Multiphase contrast-CT viewer: the phases of one study (non-contrast,
// arterial, portal-venous, delayed, ...) side by side, crosshair-synced by
// anatomical position. Intra-study phases almost always share one
// FrameOfReferenceUID, so sync is exact and needs no registration; a phase
// in a different frame gets a per-pane "Align" (rigid registration) like
// the followup viewer. Each pane auto-windows to the preset matching its
// phase. Reuses CornerstoneMPRLayout (one axial viewport per phase) and the
// shared ``useWorldSyncGrid`` hook.

import ContrastPhaseTab from "@/components/ContrastPhaseTab";
import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import ViewerIdentityBanner from "@/components/ViewerIdentityBanner";
import type { VolumeData } from "@/components/VolumeViewer";
import WashoutPanel from "@/components/WashoutPanel";
import { useWorldSyncGrid } from "@/hooks/useWorldSyncGrid";
import { isMat4 } from "@/lib/affine";
import {
  ApiError,
  type Patient,
  type PhaseRoiStats,
  type SeriesPhase,
  type StudyDetail,
  fetchVolume,
  patientsApi,
  registrationsApi,
  studiesApi,
} from "@/lib/api";
import { presetForPhase } from "@/lib/windowing";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
});

const MAX_PANES = 6;
// Below this instance count a CT "series" is a scout / screenshot / dose
// report / bolus-prep — not a reviewable phase volume; hidden from the picker.
const MIN_VOLUME_INSTANCES = 16;
// Canonical left-to-right ordering of phases in the grid.
const PHASE_ORDER: Record<string, number> = {
  unenhanced: 0,
  arterial: 1,
  portal_venous: 2,
  delayed: 3,
  hepatobiliary: 4,
  corticomedullary: 5,
  nephrographic: 6,
  excretory: 7,
  dynamic: 8,
  other: 9,
};

type AlignState = "same" | "needs" | "aligning" | "aligned" | "error";

// Minimal shape of a CornerstoneMPRLayout measurement we consume: a circle
// ROI carries its handle points in world (LPS) space.
interface DrawnMeasurement {
  csToolName?: string;
  tool: string;
  worldPoints?: Array<[number, number, number]>;
  frameOfReferenceUID?: string;
}

function PhasePane({
  phase,
  registerHandle,
  onCrosshair,
  activeTool,
  onMeasurements,
}: {
  phase: SeriesPhase;
  registerHandle: (h: MPRLayoutHandle | null) => void;
  onCrosshair: (pos: [number, number, number]) => void;
  activeTool: string;
  onMeasurements?: (m: DrawnMeasurement[]) => void;
}) {
  const t = useTranslations("contrast");
  const [vol, setVol] = useState<VolumeData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const localRef = useRef<MPRLayoutHandle | null>(null);
  const lastPhaseRef = useRef<string | null | undefined>(undefined);
  const sid = phase.series_id;

  useEffect(() => {
    let cancelled = false;
    setVol(null);
    setErr(null);
    lastPhaseRef.current = undefined;
    void (async () => {
      try {
        const { header, scalars } = await fetchVolume(sid);
        if (cancelled) return;
        setVol({
          dimensions: [header.nx, header.ny, header.nz],
          spacing: header.spacing,
          scalars,
          range: header.valueRange,
          origin: header.origin,
          direction: header.direction,
          frameOfReferenceUid: header.frameOfReferenceUid,
        });
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sid]);

  // Auto-window to the preset matching the phase. Re-applies only when the
  // phase label changes (not on every render) so a manual W/L drag is not
  // clobbered until the radiologist re-classifies the series.
  useEffect(() => {
    if (!vol) return;
    if (lastPhaseRef.current === phase.acquisition_phase) return;
    lastPhaseRef.current = phase.acquisition_phase;
    const preset = presetForPhase(phase.acquisition_phase, phase.body_part_examined);
    if (!preset) return;
    const id = requestAnimationFrame(() => {
      const h = localRef.current;
      if (h?.setWC && h?.setWW) {
        h.setWC(preset.wc);
        h.setWW(preset.ww);
      }
    });
    return () => cancelAnimationFrame(id);
  }, [vol, phase.acquisition_phase, phase.body_part_examined]);

  const setRef = (h: MPRLayoutHandle | null) => {
    localRef.current = h;
    registerHandle(h);
  };

  if (err) {
    return (
      <div className="error" style={{ padding: "1rem" }}>
        {t("loadError", { detail: err })}
      </div>
    );
  }
  if (!vol) {
    return <div style={{ color: "#6b7280", padding: "1rem" }}>{t("loading")}</div>;
  }
  return (
    <CornerstoneMPRLayout
      ref={setRef}
      volume={vol}
      showAxial
      showSagittal={false}
      showCoronal={false}
      show3D={false}
      layout="1x1"
      seriesId={sid}
      modality={phase.modality ?? null}
      seriesDescription={phase.series_description ?? undefined}
      activeTool={activeTool}
      onCrosshairChange={onCrosshair}
      onMeasurementsChange={onMeasurements}
    />
  );
}

// Manual series picker: when the study has no auto-classified phases (or the
// user wants to override which series open), let them choose the volumetric
// series to load as phase panes — instead of blindly opening every CT series.
function PhaseSeriesPicker({
  series,
  initialSelected,
  onConfirm,
  onCancel,
}: {
  series: SeriesPhase[];
  initialSelected: string[];
  onConfirm: (ids: string[]) => void;
  onCancel?: () => void;
}) {
  const t = useTranslations("contrast");
  const [sel, setSel] = useState<Set<string>>(() => new Set(initialSelected));
  const toggle = (id: string) =>
    setSel((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const chosen = series.filter((p) => sel.has(p.series_id)).map((p) => p.series_id);

  return (
    <div
      style={{
        flex: "1 1 auto",
        overflowY: "auto",
        padding: "1rem 1.25rem",
        color: "#e6ecf3",
        minHeight: 0,
      }}
    >
      <h2 style={{ fontSize: "0.95rem", margin: "0 0 0.25rem" }}>{t("pickTitle")}</h2>
      <p className="meta" style={{ margin: "0 0 0.75rem", fontSize: "0.8rem" }}>
        {t("pickSubtitle")}
      </p>
      {series.length === 0 ? (
        <p className="meta">{t("noPhases")}</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
          {series.map((p) => (
            <li key={p.series_id}>
              <label
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "4px 8px",
                  background: "#11151c",
                  border: "1px solid #1a1f2b",
                  borderRadius: 6,
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={sel.has(p.series_id)}
                  onChange={() => toggle(p.series_id)}
                />
                <span style={{ flex: 1, minWidth: 0 }}>
                  {p.acquisition_phase && (
                    <span style={{ color: "var(--bv-accent, #e96b1f)", marginRight: 6 }}>
                      {t(`phase.${p.acquisition_phase}`)}
                    </span>
                  )}
                  {p.series_description || `series ${p.series_number ?? "?"}`}
                </span>
                <span className="meta" style={{ fontSize: "0.72rem", whiteSpace: "nowrap" }}>
                  {p.instance_count ?? 0} img
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button
          type="button"
          className="ghost"
          disabled={chosen.length === 0}
          onClick={() => onConfirm(chosen)}
        >
          {t("pickOpen", { count: chosen.length })}
        </button>
        {onCancel && (
          <button type="button" className="ghost" onClick={onCancel}>
            {t("close")}
          </button>
        )}
      </div>
    </div>
  );
}

export default function ContrastViewerPage() {
  return (
    <Suspense
      fallback={
        <main style={{ padding: "2rem" }}>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <ContrastViewerInner />
    </Suspense>
  );
}

function ContrastViewerInner() {
  const t = useTranslations("contrast");
  const search = useSearchParams();
  const studyId = search.get("study") ?? search.getAll("s")[0] ?? null;

  const grid = useWorldSyncGrid();
  const [phases, setPhases] = useState<SeriesPhase[]>([]);
  const [study, setStudy] = useState<StudyDetail | null>(null);
  const [patient, setPatient] = useState<Patient | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [busySeries, setBusySeries] = useState<string | null>(null);
  const [aligning, setAligning] = useState<Record<string, boolean>>({});
  const [alignError, setAlignError] = useState<Record<string, boolean>>({});
  const [alignErrMsg, setAlignErrMsg] = useState<Record<string, string | null>>({});
  const [alignStage, setAlignStage] = useState<Record<string, string | null>>({});
  const [activeReg, setActiveReg] = useState<Record<string, string | null>>({});
  const alignCancelRef = useRef<Record<string, boolean>>({});
  const [alignedSet, setAlignedSet] = useState<Record<string, boolean>>({});
  // Which series to open as panes: null = the auto-classified phases; a list
  // = an explicit manual selection from the picker.
  const [manualIds, setManualIds] = useState<string[] | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  // Wash-out measurement state.
  const [measureMode, setMeasureMode] = useState(false);
  const [washout, setWashout] = useState<PhaseRoiStats | null>(null);
  const [washoutBusy, setWashoutBusy] = useState(false);
  const [washoutErr, setWashoutErr] = useState<string | null>(null);
  const [washoutSaved, setWashoutSaved] = useState(false);
  const lastRoiRef = useRef<string | null>(null);
  const savedRoiRef = useRef<{
    center_lps: [number, number, number];
    radius_mm: number;
    frame_of_reference_uid: string | null;
  } | null>(null);

  useEffect(() => {
    if (!studyId) return;
    let cancelled = false;
    void (async () => {
      try {
        const detail = await studiesApi.detail(studyId);
        if (cancelled) return;
        setStudy(detail);
        if (detail.patient_id) {
          patientsApi
            .detail(detail.patient_id)
            .then((p) => {
              if (!cancelled) setPatient(p);
            })
            .catch(() => {});
        }
        let manifest = await studiesApi.phases(studyId);
        // Best-effort auto-detect when nothing is classified yet (idempotent;
        // a non-owner gets 403 and we just show the unclassified manifest).
        const ct = manifest.phases.filter((p) => (p.modality || "").toUpperCase() === "CT");
        if (ct.length > 0 && manifest.phases.every((p) => !p.acquisition_phase)) {
          try {
            manifest = await studiesApi.detectPhases(studyId);
          } catch {
            /* not owner / detect unavailable — manual button stays */
          }
        }
        if (!cancelled) setPhases(manifest.phases);
      } catch (e) {
        if (!cancelled) setLoadErr(e instanceof ApiError ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [studyId]);

  // The classifier-labelled phase series (one per phase, clinical order) are
  // the default panes. Scouts / recon kernels / MPR reformats / dose reports
  // never get a phase label, so they fall away on their own — no blind slice
  // of every CT series.
  const classified = useMemo(() => {
    const byPhase = new Map<string, SeriesPhase>();
    for (const p of phases) {
      if (p.acquisition_phase && !byPhase.has(p.acquisition_phase)) {
        byPhase.set(p.acquisition_phase, p);
      }
    }
    return [...byPhase.values()].sort(
      (a, b) =>
        (PHASE_ORDER[a.acquisition_phase ?? "other"] ?? 9) -
        (PHASE_ORDER[b.acquisition_phase ?? "other"] ?? 9),
    );
  }, [phases]);

  // Volumetric CT series the user can pick from manually (drop the scout /
  // screenshot / dose / bolus-prep clutter); fall back to all CT if the
  // instance-count signal leaves nothing.
  const selectable = useMemo(() => {
    const ct = phases.filter((p) => (p.modality || "").toUpperCase() === "CT");
    const volumetric = ct.filter((p) => (p.instance_count ?? 0) >= MIN_VOLUME_INSTANCES);
    return volumetric.length > 0 ? volumetric : ct;
  }, [phases]);

  // Panes = the manual pick if any, else the classified phases. Never every CT.
  const panes = useMemo(() => {
    const base =
      manualIds != null
        ? manualIds
            .map((id) => phases.find((p) => p.series_id === id))
            .filter((p): p is SeriesPhase => p != null)
        : classified;
    return base.slice(0, MAX_PANES);
  }, [manualIds, classified, phases]);
  const truncated = (manualIds?.length ?? classified.length) > MAX_PANES;
  const referenceFoR = panes[0]?.frame_of_reference_uid ?? null;

  // Nothing to show and no manual pick yet -> open the picker so the user
  // chooses the phase series rather than facing an empty viewer.
  useEffect(() => {
    if (phases.length > 0 && classified.length === 0 && manualIds == null) {
      setShowPicker(true);
    }
  }, [phases.length, classified.length, manualIds]);

  // Assign each pane's sync transform from its FrameOfReferenceUID: same FoR
  // as the reference pane => identity (null, syncable). Different FoR are
  // left unset in the hook (skipped in sync) until the user aligns them.
  // grid.setTransform is stable (useCallback); rerun only when the pane set
  // or reference frame changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: grid is stable; keying on panes/referenceFoR is intentional
  useEffect(() => {
    panes.forEach((p, i) => {
      const same = !!p.frame_of_reference_uid && p.frame_of_reference_uid === referenceFoR;
      if (i === 0 || same) grid.setTransform(i, null);
    });
  }, [panes, referenceFoR]);

  function alignStateOf(p: SeriesPhase, i: number): AlignState {
    const same =
      i === 0 || (!!p.frame_of_reference_uid && p.frame_of_reference_uid === referenceFoR);
    if (same) return "same";
    if (aligning[p.series_id]) return "aligning";
    if (alignedSet[p.series_id]) return "aligned";
    if (alignError[p.series_id]) return "error";
    return "needs";
  }

  async function detect() {
    if (!studyId) return;
    setDetecting(true);
    try {
      const manifest = await studiesApi.detectPhases(studyId, false);
      setPhases(manifest.phases);
    } catch {
      /* surfaced via the badge; keep prior manifest */
    } finally {
      setDetecting(false);
    }
  }

  async function setPhase(seriesId: string, acquisitionPhase: string | null) {
    setBusySeries(seriesId);
    try {
      const updated = await studiesApi.setSeriesPhase(seriesId, acquisitionPhase);
      setPhases((prev) => prev.map((p) => (p.series_id === seriesId ? { ...p, ...updated } : p)));
    } catch {
      /* leave unchanged on failure */
    } finally {
      setBusySeries(null);
    }
  }

  async function align(paneIndex: number) {
    const pane = panes[paneIndex];
    const ref = panes[0];
    if (!pane || !ref) return;
    const sid = pane.series_id;
    setAlignError((s) => ({ ...s, [sid]: false }));
    setAlignErrMsg((s) => ({ ...s, [sid]: null }));
    setAlignStage((s) => ({ ...s, [sid]: "queued" }));
    setAligning((s) => ({ ...s, [sid]: true }));
    alignCancelRef.current[sid] = false;
    const fail = (msg?: string) => {
      setAlignError((s) => ({ ...s, [sid]: true }));
      setAlignErrMsg((s) => ({ ...s, [sid]: msg ?? t("alignFailed") }));
    };
    try {
      const reg = await registrationsApi.create({
        fixed_series_id: ref.series_id,
        moving_series_id: sid,
        kind: "rigid",
      });
      setActiveReg((s) => ({ ...s, [sid]: reg.id }));
      // Poll until terminal. No blind short cap: rigid registration legitimately
      // runs a few minutes under worker load. ~10 min backstop, live stage shown.
      for (let i = 0; i < 300; i++) {
        if (alignCancelRef.current[sid]) return;
        const cur = await registrationsApi.get(reg.id);
        setAlignStage((s) => ({
          ...s,
          [sid]: cur.status === "running" ? (cur.stage ?? "running") : cur.status,
        }));
        if (cur.status === "succeeded") {
          const m = cur.result_meta?.lps_matrix;
          if (isMat4(m)) {
            grid.setTransform(paneIndex, m);
            setAlignedSet((s) => ({ ...s, [sid]: true }));
          } else {
            fail(t("noMatrix"));
          }
          return;
        }
        if (cur.status === "failed") {
          fail(cur.error ?? undefined);
          return;
        }
        if (cur.status === "cancelled") return;
        await new Promise((r) => setTimeout(r, 2000));
      }
      fail(t("alignTimeout"));
    } catch (e) {
      fail(e instanceof ApiError ? e.message : undefined);
    } finally {
      setAligning((s) => ({ ...s, [sid]: false }));
      setAlignStage((s) => ({ ...s, [sid]: null }));
      setActiveReg((s) => ({ ...s, [sid]: null }));
    }
  }

  async function cancelAlign(seriesId: string) {
    alignCancelRef.current[seriesId] = true;
    const id = activeReg[seriesId];
    if (id) {
      try {
        await registrationsApi.cancel(id);
      } catch {
        /* best-effort */
      }
    }
    setAligning((s) => ({ ...s, [seriesId]: false }));
    setAlignStage((s) => ({ ...s, [seriesId]: null }));
    setActiveReg((s) => ({ ...s, [seriesId]: null }));
  }

  function handleMeasurements(ms: DrawnMeasurement[]) {
    if (!studyId) return;
    // Most recent circle ROI: handle[0] = centre, handle[1] = a point on the
    // perimeter, both in world (LPS); radius = their distance (mm).
    const circle = [...ms]
      .reverse()
      .find(
        (m) =>
          (m.csToolName === "CircleROI" || m.tool === "sphere") &&
          (m.worldPoints?.length ?? 0) >= 2,
      );
    const pts = circle?.worldPoints;
    if (!pts || pts.length < 2) return;
    const c = pts[0];
    const e = pts[1];
    const radius = Math.hypot(e[0] - c[0], e[1] - c[1], e[2] - c[2]);
    if (!(radius > 0)) return;
    const forUid = circle?.frameOfReferenceUID ?? referenceFoR ?? null;
    const key = `${c[0].toFixed(3)},${c[1].toFixed(3)},${c[2].toFixed(3)}|${radius.toFixed(3)}`;
    if (lastRoiRef.current === key) return; // unchanged ROI; don't re-fetch
    lastRoiRef.current = key;
    savedRoiRef.current = { center_lps: c, radius_mm: radius, frame_of_reference_uid: forUid };
    setWashoutBusy(true);
    setWashoutErr(null);
    setWashoutSaved(false);
    studiesApi
      .phaseRoiStats(studyId, {
        kind: "sphere",
        center_lps: c,
        radius_mm: radius,
        frame_of_reference_uid: forUid,
      })
      .then((r) => setWashout(r))
      .catch((err) => setWashoutErr(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setWashoutBusy(false));
  }

  async function saveWashout() {
    const roi = savedRoiRef.current;
    if (!studyId || !washout || !roi) return;
    try {
      await studiesApi.createPhaseEnhancementSet(studyId, {
        roi_kind: "sphere",
        roi: {
          center_lps: roi.center_lps,
          radius_mm: roi.radius_mm,
          frame_of_reference_uid: roi.frame_of_reference_uid,
        },
        samples: washout.samples.map((s) => ({
          series_id: s.series_id,
          acquisition_phase: s.acquisition_phase,
          hu_mean: s.hu_mean,
          hu_std: s.hu_std,
          voxel_count: s.voxel_count,
        })),
      });
      setWashoutSaved(true);
    } catch (e) {
      setWashoutErr(e instanceof ApiError ? e.message : String(e));
    }
  }

  if (!studyId) {
    return (
      <main style={{ padding: "2rem" }}>
        <Link href="/studies">← studies</Link>
        <h1>{t("title")}</h1>
        <p className="meta">{t("pickHint")}</p>
      </main>
    );
  }

  const cols = panes.length <= 1 ? 1 : panes.length <= 4 ? 2 : 3;

  return (
    <div
      style={{
        height: "calc(100vh - 56px)",
        background: "#000",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <ViewerIdentityBanner patient={patient} study={study} />
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
        <button type="button" className="ghost" disabled={detecting} onClick={detect}>
          {detecting ? t("detecting") : t("detect")}
        </button>
        <button
          type="button"
          className={measureMode ? "viewer-btn viewer-btn--active" : "ghost"}
          aria-pressed={measureMode}
          onClick={() => {
            setMeasureMode((v) => !v);
            if (measureMode) {
              setWashout(null);
              lastRoiRef.current = null;
            }
          }}
          title={t("washoutHint")}
        >
          {t("measureWashout")}
        </button>
        <button
          type="button"
          className={showPicker ? "viewer-btn viewer-btn--active" : "ghost"}
          aria-pressed={showPicker}
          onClick={() => setShowPicker((v) => !v)}
          title={t("pickSubtitle")}
        >
          {t("chooseSeries")}
        </button>
        {loadErr && <span style={{ color: "var(--bv-danger, #f87171)" }}>{loadErr}</span>}
        {truncated && (
          <span className="meta" style={{ fontSize: "0.76rem" }}>
            {`showing first ${MAX_PANES}`}
          </span>
        )}
      </div>

      <div style={{ flex: "1 1 auto", display: "flex", minHeight: 0 }}>
        {showPicker ? (
          <PhaseSeriesPicker
            series={selectable}
            initialSelected={manualIds ?? classified.map((p) => p.series_id)}
            onConfirm={(ids) => {
              setManualIds(ids);
              setShowPicker(false);
            }}
            onCancel={panes.length > 0 ? () => setShowPicker(false) : undefined}
          />
        ) : panes.length === 0 ? (
          <div style={{ color: "#6b7280", padding: "1.5rem" }}>{t("noPhases")}</div>
        ) : (
          <div
            style={{
              flex: "1 1 auto",
              display: "grid",
              gridTemplateColumns: `repeat(${cols}, 1fr)`,
              gap: 2,
              minHeight: 0,
            }}
          >
            {panes.map((phase, i) => {
              const a = alignStateOf(phase, i);
              return (
                <div
                  key={phase.series_id}
                  style={{
                    position: "relative",
                    background: "#000",
                    border: "1px solid #1a1f2b",
                    display: "grid",
                    gridTemplateRows: "auto 1fr",
                    overflow: "hidden",
                    minHeight: 0,
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
                      flexWrap: "wrap",
                    }}
                  >
                    <ContrastPhaseTab
                      phase={phase}
                      busy={busySeries === phase.series_id}
                      onSet={(p) => setPhase(phase.series_id, p)}
                    />
                    {a === "needs" && (
                      <button
                        type="button"
                        className="ghost"
                        title={t("needsAlign")}
                        onClick={() => align(i)}
                        style={{ fontSize: "0.72rem", padding: "1px 6px" }}
                      >
                        {t("align")}
                      </button>
                    )}
                    {a === "aligning" && (
                      <>
                        <span className="meta">
                          {alignStage[phase.series_id] &&
                          t.has(`alignStage.${alignStage[phase.series_id]}`)
                            ? t(`alignStage.${alignStage[phase.series_id]}`)
                            : t("aligning")}
                        </span>
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => cancelAlign(phase.series_id)}
                          style={{ fontSize: "0.72rem", padding: "1px 6px" }}
                        >
                          {t("alignCancel")}
                        </button>
                      </>
                    )}
                    {a === "aligned" && (
                      <span style={{ color: "var(--bv-success, #047857)" }}>{t("aligned")}</span>
                    )}
                    {a === "error" && (
                      <span
                        style={{ color: "var(--bv-danger, #f87171)", fontSize: "0.72rem" }}
                        title={alignErrMsg[phase.series_id] ?? undefined}
                      >
                        {alignErrMsg[phase.series_id] ?? t("alignFailed")}
                      </span>
                    )}
                    {a === "error" && (
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => align(i)}
                        style={{ fontSize: "0.72rem", padding: "1px 6px" }}
                      >
                        {t("alignRetry")}
                      </button>
                    )}
                    {phase.series_description && (
                      <span
                        style={{
                          color: "#64748b",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          minWidth: 0,
                        }}
                        title={phase.series_description}
                      >
                        {phase.series_description}
                      </span>
                    )}
                  </div>
                  <PhasePane
                    phase={phase}
                    registerHandle={grid.registerPane(i)}
                    onCrosshair={(pos) => grid.onCrosshairChange(i, pos)}
                    activeTool={measureMode ? "measure-sphere" : "wl"}
                    onMeasurements={i === 0 ? handleMeasurements : undefined}
                  />
                </div>
              );
            })}
          </div>
        )}
        {(measureMode || washout) && (
          <WashoutPanel
            result={washout}
            busy={washoutBusy}
            error={washoutErr}
            saved={washoutSaved}
            onSave={saveWashout}
            onClose={() => {
              setMeasureMode(false);
              setWashout(null);
              lastRoiRef.current = null;
            }}
          />
        )}
      </div>
    </div>
  );
}
