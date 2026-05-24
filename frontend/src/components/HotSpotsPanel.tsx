"use client";

// Hot-spots panel for the viewer sidebar.
//
// Lists the top-N high-uptake connected regions in the active series
// (typically a PET volume). Wraps the backend
// ``POST /api/series/{id}/hot-spots`` endpoint, which runs a
// scipy-backed connected-components pass on the cached packed
// volume and returns each component's centroid IJK + bbox + voxel
// count + raw/SUV stats.
//
// Clinical conventions (Hermes / Mirada / MIM PETedge):
//   - PET default = "absolute SUV ≥ 2.5" (PERCIST 1.0 measurable
//     lesion threshold). Numbers are interpretable and don't depend
//     on the dataset's brightest pixel.
//   - "% of max" stays available for comparing against the volume's
//     own dynamic range (PERCIST tumor SUVpeak relative thresholds).
//   - The global SUVmax is shown next to the threshold so the
//     operator can see what "% of max" actually maps to.
//
// UX:
//   1. Operator dials in mode + threshold + min volume + top-N.
//   2. "Find" runs the backend pass.
//   3. Results sorted by SUVmax. Each row reports SUVmax / SUVpeak /
//      SUVmean / volume mL, plus the axial slice index of the
//      centroid so the operator knows where it sits anatomically.
//   4. Click a row → ``onJumpTo`` re-centres the MPR crosshair on
//      the centroid (same contract as MarkerListPanel.onJumpTo).
//      Coordinate translation across volumes (PT → primary CT for
//      fusion-on-CT-base review) is the parent's responsibility:
//      we hand back IJK in the volume the endpoint queried.

import { useCallback, useEffect, useRef, useState } from "react";

import { type Marker, markersApi, request } from "@/lib/api";
import { useModal } from "./ModalHost";

// Anatomic labels we want to surface as one-click exclusion toggles
// on the panel. Map UI groups → list of TotalSegmentator label names
// produced by the auto-seg worker. Kept in one place so adding "lung"
// or "heart" as a future organ exclusion is a one-line change here.
const EXCLUSION_GROUPS: Array<{
  id: "kidneys" | "bladder" | "liver";
  label: string;
  totalsegLabels: string[];
}> = [
  { id: "kidneys", label: "Reni", totalsegLabels: ["kidney_left", "kidney_right"] },
  { id: "bladder", label: "Vescica", totalsegLabels: ["urinary_bladder"] },
  { id: "liver", label: "Fegato", totalsegLabels: ["liver"] },
];

interface SegmentationListItem {
  label: string;
  size_bytes: number;
  nonzero_voxels?: number | null;
}

interface SegmentationsResponse {
  series_id: string;
  items: SegmentationListItem[];
}

interface AutoSegmentStatusResponse {
  job_id: string;
  state: "deferred" | "queued" | "in_progress" | "complete" | "not_found";
  failed?: boolean;
  error?: string | null;
  result?: Record<string, unknown> | null;
}

function segJobStorageKey(seriesId: string): string {
  return `bvp.seg-jobs.${seriesId}`;
}

interface HotSpot {
  rank: number;
  centroid_ijk: [number, number, number];
  bbox_min_ijk: [number, number, number];
  bbox_max_ijk: [number, number, number];
  voxel_count: number;
  volume_ml: number;
  raw_max: number;
  raw_mean: number;
  suv_max: number | null;
  suv_mean: number | null;
  suv_peak: number | null;
}

interface HotSpotsResponse {
  spots: HotSpot[];
  threshold_used: number;
  threshold_kind: string;
  suv_variant_used: string | null;
  units_native: string | null;
  global_max_raw: number;
  global_max_suv: number | null;
  volume_nz: number;
  slice_min_used: number;
  slice_max_used: number;
}

type ThresholdMode = "percent_of_max" | "absolute_suv";

interface Props {
  /** Series the hot-spot search runs on (the PET in PT-CT review). */
  seriesId: string;
  /** ``true`` when the volume is PET and SUV stats are available. */
  isPet: boolean;
  /** Jump-to handler. Voxel is in the queried series' IJK; if the
   *  parent's MPR primary differs from ``seriesId``, the parent must
   *  translate via world coordinates before calling setCrosshair. */
  onJumpTo: (voxel: [number, number, number]) => void;
  /** Patient owning the series — needed to create / list
   *  ``bbox.exclusion`` markers as the manual fallback when an
   *  automatic segmentation isn't available. ``null`` disables the
   *  manual-exclusion affordance with a tooltip. */
  patientId?: string | null;
}

// Defaults reflect PERCIST 1.0 clinical practice when the volume is
// PET (absolute SUV ≥ 2.5, ≥ 1 mL). For non-PET volumes (HU on CT)
// "% of max" 50% is the closest neutral default — there's no
// universally agreed clinical anchor for high-density components.
const DEFAULT_PET = {
  mode: "absolute_suv" as ThresholdMode,
  value: 2.5,
  minMl: 1.0,
};
const DEFAULT_RAW = {
  mode: "percent_of_max" as ThresholdMode,
  value: 0.5,
  minMl: 1.0,
};

export default function HotSpotsPanel({ seriesId, isPet, onJumpTo, patientId }: Props) {
  const modal = useModal();
  const initial = isPet ? DEFAULT_PET : DEFAULT_RAW;
  const [thresholdMode, setThresholdMode] = useState<ThresholdMode>(initial.mode);
  const [thresholdValue, setThresholdValue] = useState<number>(initial.value);
  const [minVolumeMl, setMinVolumeMl] = useState<number>(initial.minMl);
  const [topN, setTopN] = useState<number>(50);
  // Optional Z-axis slab restriction. Empty string = "no restriction
  // on that side"; we only forward a value to the backend when the
  // input is non-empty so the panel keeps working on series the user
  // doesn't want to slab. Stored as strings so the inputs handle
  // partial typing ("18", then "180") without clamping mid-edit.
  const [sliceMin, setSliceMin] = useState<string>("");
  const [sliceMax, setSliceMax] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<HotSpotsResponse | null>(null);
  // Organ exclusion toggles. Default-on for PET kidneys + bladder
  // (these dominate the MIP and are almost never the target of
  // interest); default-off for non-PET CT and for the liver (liver
  // exclusion only makes sense when the operator is hunting for
  // extra-hepatic disease).
  const [excludeKidneys, setExcludeKidneys] = useState<boolean>(isPet);
  const [excludeBladder, setExcludeBladder] = useState<boolean>(isPet);
  const [excludeLiver, setExcludeLiver] = useState<boolean>(false);
  const [availableLabels, setAvailableLabels] = useState<Set<string>>(new Set());
  // ``jobId`` is the Arq job id returned by POST /segmentations/auto.
  // Stored in state + localStorage so the polling loop can ask the
  // backend for the actual engine status (queued / running / failed)
  // instead of inferring "still going" from the absence of new
  // segmentation labels — which is what the previous version did and
  // what kept the spinner up forever after an engine_error.
  const [segJobActive, setSegJobActive] = useState<{
    startedAt: number;
    jobId: string | null;
  } | null>(null);
  const [segJobError, setSegJobError] = useState<string | null>(null);
  const pollingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // List the available segmentation labels for the series so we can
  // disable the exclusion checkbox + offer the "Run auto-seg" button
  // when an organ is missing.
  const refreshLabels = useCallback(async () => {
    try {
      const r = await request<SegmentationsResponse>(`/api/series/${seriesId}/segmentations`);
      setAvailableLabels(new Set(r.items.map((it) => it.label)));
    } catch {
      // Network or permission error — degrade to "no labels" so the
      // panel still works and the checkboxes become disabled with a
      // hint to retry.
      setAvailableLabels(new Set());
    }
  }, [seriesId]);

  useEffect(() => {
    refreshLabels();
    // Recover an in-flight auto-seg job started in a previous session.
    // Old localStorage entries (pre-v3.6.3) only carry ``startedAt``;
    // ``jobId`` will be ``null`` and the backend status poll degrades
    // to "best-effort label watch" for those. New entries carry the
    // jobId so we can read the engine status directly.
    try {
      const raw = window.localStorage.getItem(segJobStorageKey(seriesId));
      if (raw) {
        const parsed = JSON.parse(raw) as { startedAt?: number; jobId?: string };
        if (typeof parsed.startedAt === "number") {
          setSegJobActive({
            startedAt: parsed.startedAt,
            jobId: typeof parsed.jobId === "string" ? parsed.jobId : null,
          });
        }
      }
    } catch {
      /* ignore */
    }
  }, [refreshLabels, seriesId]);

  // Poll while a job is active. Two checks per tick:
  //   1. ``GET /segmentations/auto/status?job_id=...`` — engine state
  //      (queued / in_progress / complete / failed). Lets us clear the
  //      spinner on engine_error / OOM / not_found instead of waiting
  //      forever for labels that will never appear.
  //   2. ``refreshLabels`` — keeps the exclusion checkboxes activating
  //      as labels land. Some labels may be uploaded mid-run (the worker
  //      streams masks one at a time once each is ready), so we still
  //      need this even after we know the state.
  // Polling stops when state ∈ {complete, failed, not_found}.
  useEffect(() => {
    if (!segJobActive) return;
    // ``cancelled`` guards against a re-run of this effect (e.g. the
    // segJobActive identity changes while a tick is mid-await): the
    // previous tick must NOT re-arm pollingTimerRef.current after the
    // new effect run has already armed its own, otherwise the new
    // timer is overwritten and we leak it.
    const jobId = segJobActive.jobId;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await refreshLabels();
      if (cancelled) return;
      let stop = false;
      if (jobId) {
        try {
          const s = await request<AutoSegmentStatusResponse>(
            `/api/series/${seriesId}/segmentations/auto/status?job_id=${encodeURIComponent(jobId)}`,
          );
          if (cancelled) return;
          if (s.failed || s.state === "complete" || s.state === "not_found") {
            stop = true;
            if (s.failed) {
              setSegJobError(`Motore: ${s.error ?? "errore sconosciuto"}`);
            }
            // ``not_found`` (Arq retention expired, default 24 h) is
            // not surfaced as an error: silent stop with the stale
            // entry cleared from localStorage is the best we can do
            // without a Job db row.
            setSegJobActive(null);
            try {
              window.localStorage.removeItem(segJobStorageKey(seriesId));
            } catch {
              /* ignore */
            }
          }
        } catch {
          // Transient network error — keep polling. A persistent 4xx
          // (eg permissions revoked) will be reported by the next
          // user-visible action; we don't want to clear the spinner
          // on a single failed status check.
        }
      }
      if (!stop && !cancelled) {
        pollingTimerRef.current = setTimeout(tick, 5000);
      }
    };
    pollingTimerRef.current = setTimeout(tick, 5000);
    return () => {
      cancelled = true;
      if (pollingTimerRef.current) {
        clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
    };
  }, [segJobActive, refreshLabels, seriesId]);

  // Auto-clear the job marker when every expected label is present.
  useEffect(() => {
    if (!segJobActive) return;
    const wanted = new Set<string>();
    for (const g of EXCLUSION_GROUPS) {
      for (const l of g.totalsegLabels) wanted.add(l);
    }
    const allPresent = Array.from(wanted).every((l) => availableLabels.has(l));
    if (allPresent) {
      setSegJobActive(null);
      try {
        window.localStorage.removeItem(segJobStorageKey(seriesId));
      } catch {
        /* ignore */
      }
    }
  }, [availableLabels, segJobActive, seriesId]);

  const runAutoSegmentation = async () => {
    setSegJobError(null);
    try {
      const r = await request<{ job_id?: string }>(`/api/series/${seriesId}/segmentations/auto`, {
        method: "POST",
        json: {},
      });
      const startedAt = Date.now();
      const jobId = typeof r.job_id === "string" && r.job_id ? r.job_id : null;
      setSegJobActive({ startedAt, jobId });
      try {
        window.localStorage.setItem(
          segJobStorageKey(seriesId),
          JSON.stringify({ startedAt, jobId }),
        );
      } catch {
        /* ignore */
      }
    } catch (e) {
      setSegJobError(e instanceof Error ? e.message : String(e));
    }
  };

  const resolveExclusionLabels = (): string[] => {
    const out: string[] = [];
    const push = (group: (typeof EXCLUSION_GROUPS)[number]) => {
      for (const l of group.totalsegLabels) {
        if (availableLabels.has(l)) out.push(l);
      }
    };
    if (excludeKidneys) push(EXCLUSION_GROUPS[0]);
    if (excludeBladder) push(EXCLUSION_GROUPS[1]);
    if (excludeLiver) push(EXCLUSION_GROUPS[2]);
    return out;
  };

  const missingForToggled = (): string[] => {
    const want = new Set<string>();
    if (excludeKidneys) for (const l of EXCLUSION_GROUPS[0].totalsegLabels) want.add(l);
    if (excludeBladder) for (const l of EXCLUSION_GROUPS[1].totalsegLabels) want.add(l);
    if (excludeLiver) for (const l of EXCLUSION_GROUPS[2].totalsegLabels) want.add(l);
    return Array.from(want).filter((l) => !availableLabels.has(l));
  };

  const segJobElapsedSeconds = segJobActive
    ? Math.max(0, Math.round((Date.now() - segJobActive.startedAt) / 1000))
    : null;

  // Manual exclusion markers — the day-1 fallback when no
  // segmentation mask exists. The operator clicks once to add a box;
  // the marker persists and is offered on subsequent searches.
  const [exclusionMarkers, setExclusionMarkers] = useState<Marker[]>([]);
  const [activeExclusionIds, setActiveExclusionIds] = useState<Set<string>>(new Set());

  const refreshExclusionMarkers = useCallback(async () => {
    if (!patientId) return;
    try {
      const list = await markersApi.list(patientId, {
        target_kind: "series",
        target_id: seriesId,
        kind: "bbox.exclusion",
      });
      setExclusionMarkers(list);
      // Default-on: any pre-existing exclusion is applied unless the
      // operator explicitly disables it. Same UX as the organ
      // checkboxes above — pre-drawn exclusions ARE the exclusion.
      setActiveExclusionIds(new Set(list.map((m) => m.id)));
    } catch {
      setExclusionMarkers([]);
    }
  }, [patientId, seriesId]);

  useEffect(() => {
    refreshExclusionMarkers();
  }, [refreshExclusionMarkers]);

  const parseTriplet = (raw: string | null): [number, number, number] | null => {
    if (!raw) return null;
    const parts = raw
      .split(/[\s,;]+/)
      .filter(Boolean)
      .map(Number);
    if (parts.length !== 3 || parts.some((p) => !Number.isFinite(p))) return null;
    return [parts[0], parts[1], parts[2]];
  };

  const addManualExclusion = async () => {
    if (!patientId) return;
    const minRaw = await modal.prompt({
      title: "Esclusione manuale — corner min",
      label: "min_ijk (i, j, k, separati da virgola)",
      placeholder: "es. 90, 151, 130",
    });
    const min_ijk = parseTriplet(minRaw);
    if (!min_ijk) return;
    const maxRaw = await modal.prompt({
      title: "Esclusione manuale — corner max",
      label: "max_ijk (i, j, k, separati da virgola)",
      placeholder: "es. 130, 200, 160",
    });
    const max_ijk = parseTriplet(maxRaw);
    if (!max_ijk) return;
    // Sanity: max ≥ min on every axis.
    for (let i = 0; i < 3; i++) {
      if (max_ijk[i] < min_ijk[i]) {
        modal.confirm({
          title: "Esclusione non valida",
          message: `max_ijk[${i}] (${max_ijk[i]}) deve essere ≥ min_ijk[${i}] (${min_ijk[i]}).`,
          confirmLabel: "OK",
        });
        return;
      }
    }
    try {
      await markersApi.create(patientId, {
        target_kind: "series",
        target_id: seriesId,
        kind: "bbox.exclusion",
        geometry: { min_ijk, max_ijk },
        computed: { source: "manual" },
        body: `Manual exclusion ${min_ijk.join(",")} → ${max_ijk.join(",")}`,
      });
      await refreshExclusionMarkers();
    } catch (e) {
      setSegJobError(e instanceof Error ? e.message : String(e));
    }
  };

  const removeExclusionMarker = async (markerId: string) => {
    const ok = await modal.confirm({
      title: "Rimuovere esclusione?",
      message:
        "Il marker bbox.exclusion verrà eliminato; le ricerche successive non lo applicheranno più.",
      confirmLabel: "Elimina",
    });
    if (!ok) return;
    try {
      await markersApi.remove(markerId);
      await refreshExclusionMarkers();
    } catch (e) {
      setSegJobError(e instanceof Error ? e.message : String(e));
    }
  };

  const find = async () => {
    setLoading(true);
    setErr(null);
    try {
      const sliceMinNum = sliceMin.trim() === "" ? null : Number(sliceMin);
      const sliceMaxNum = sliceMax.trim() === "" ? null : Number(sliceMax);
      const exclusionLabels = resolveExclusionLabels();
      const exclusionMarkerIds = Array.from(activeExclusionIds);
      const json = await request<HotSpotsResponse>(`/api/series/${seriesId}/hot-spots`, {
        method: "POST",
        json: {
          threshold_mode: thresholdMode,
          threshold_value: thresholdValue,
          min_volume_ml: minVolumeMl,
          top_n: topN,
          slice_min: Number.isFinite(sliceMinNum) ? sliceMinNum : null,
          slice_max: Number.isFinite(sliceMaxNum) ? sliceMaxNum : null,
          suv_variant: isPet ? "bw" : null,
          exclude_segmentation_labels: exclusionLabels.length > 0 ? exclusionLabels : null,
          exclude_marker_ids: exclusionMarkerIds.length > 0 ? exclusionMarkerIds : null,
        },
      });
      setData(json);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const fmt = (n: number, digits = 2) => (Number.isFinite(n) ? n.toFixed(digits) : "—");

  // Convert the user's threshold (in their chosen units) into the
  // SUV cut-off it actually means, given the global SUVmax we know
  // about. Surfaced as a hint next to the threshold input so the
  // operator can see e.g. "0.5 × max ⇒ SUV ≥ 7.4" before clicking
  // Find. Returns null when we can't compute it (no PET, no prior
  // search yet).
  const previewSuvCutoff = (): number | null => {
    if (!isPet) return null;
    if (thresholdMode === "absolute_suv") return thresholdValue;
    if (data?.global_max_suv != null) {
      return thresholdValue * data.global_max_suv;
    }
    return null;
  };
  const suvCutoffPreview = previewSuvCutoff();

  return (
    <>
      <p
        style={{
          fontSize: "0.65rem",
          color: "#94a3b8",
          margin: "0 0 0.35rem",
          lineHeight: 1.35,
        }}
      >
        Connected-component lesion finder. PERCIST 1.0 default for PET: absolute SUV ≥ 2.5, ≥
        1&nbsp;mL.
      </p>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          rowGap: 4,
          columnGap: 6,
          fontSize: "0.7rem",
          color: "#cbd5e1",
          alignItems: "center",
          marginBottom: "0.4rem",
        }}
      >
        <label htmlFor="hs-mode">Mode</label>
        <select
          id="hs-mode"
          value={thresholdMode}
          onChange={(e) => setThresholdMode(e.target.value as ThresholdMode)}
          style={{ fontSize: "0.72rem" }}
        >
          <option value="absolute_suv" disabled={!isPet}>
            Absolute SUV ≥
          </option>
          <option value="percent_of_max">Fraction of volume max ×</option>
        </select>

        <label htmlFor="hs-thr">{thresholdMode === "absolute_suv" ? "SUV ≥" : "× max"}</label>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            id="hs-thr"
            type="number"
            min={thresholdMode === "absolute_suv" ? 0.1 : 0.01}
            max={thresholdMode === "absolute_suv" ? 50 : 1}
            step={thresholdMode === "absolute_suv" ? 0.1 : 0.05}
            value={thresholdValue}
            onChange={(e) => setThresholdValue(Number(e.target.value))}
            style={{ width: 70, fontSize: "0.72rem" }}
          />
          {suvCutoffPreview !== null && thresholdMode === "percent_of_max" && (
            <span style={{ fontSize: "0.65rem", color: "#94a3b8" }}>
              ≈ SUV ≥ {fmt(suvCutoffPreview)}
            </span>
          )}
        </div>

        <label htmlFor="hs-vol">Min volume</label>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input
            id="hs-vol"
            type="number"
            min={0}
            step={0.1}
            value={minVolumeMl}
            onChange={(e) => setMinVolumeMl(Number(e.target.value))}
            style={{ width: 70, fontSize: "0.72rem" }}
          />
          <span style={{ fontSize: "0.65rem", color: "#94a3b8" }}>mL</span>
        </div>

        <label htmlFor="hs-top">Top N</label>
        <input
          id="hs-top"
          type="number"
          min={1}
          max={200}
          value={topN}
          onChange={(e) => setTopN(Number(e.target.value))}
          style={{ width: 70, fontSize: "0.72rem" }}
        />

        <span title="Restrict the search to a Z-axis slab. Read slice numbers off the axial MPR (top-left of each slice). Leave both empty to scan the whole volume.">
          Slice range
        </span>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            fontSize: "0.7rem",
          }}
        >
          <input
            id="hs-slice-min"
            type="number"
            min={0}
            max={data ? data.volume_nz - 1 : undefined}
            value={sliceMin}
            placeholder="from"
            onChange={(e) => setSliceMin(e.target.value)}
            style={{ width: 56, fontSize: "0.72rem" }}
          />
          <span style={{ color: "#94a3b8" }}>–</span>
          <input
            id="hs-slice-max"
            type="number"
            min={0}
            max={data ? data.volume_nz - 1 : undefined}
            value={sliceMax}
            placeholder="to"
            onChange={(e) => setSliceMax(e.target.value)}
            style={{ width: 56, fontSize: "0.72rem" }}
          />
          {data && (
            <span style={{ color: "#94a3b8", fontSize: "0.65rem" }}>of {data.volume_nz}</span>
          )}
        </div>
      </div>

      <div
        style={{
          padding: "0.35rem 0.4rem",
          background: "rgba(15,23,42,0.4)",
          border: "1px solid rgba(255,255,255,0.08)",
          borderRadius: 3,
          marginBottom: "0.4rem",
          display: "flex",
          flexDirection: "column",
          gap: 4,
        }}
      >
        <div style={{ fontSize: "0.7rem", color: "#cbd5e1" }}>
          Esclusione organi
          <span style={{ color: "#94a3b8", fontWeight: 400 }}>
            {" "}
            (rimuove i voxel dal volume di ricerca)
          </span>
        </div>
        <div style={{ display: "flex", gap: "0.8rem", flexWrap: "wrap", fontSize: "0.72rem" }}>
          {EXCLUSION_GROUPS.map((g) => {
            const checked =
              g.id === "kidneys"
                ? excludeKidneys
                : g.id === "bladder"
                  ? excludeBladder
                  : excludeLiver;
            const setter =
              g.id === "kidneys"
                ? setExcludeKidneys
                : g.id === "bladder"
                  ? setExcludeBladder
                  : setExcludeLiver;
            const have = g.totalsegLabels.every((l) => availableLabels.has(l));
            return (
              <label
                key={g.id}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  color: have ? "#e6ecf3" : "#94a3b8",
                  cursor: have ? "pointer" : "not-allowed",
                }}
                title={
                  have
                    ? `Esclude i voxel marcati come ${g.totalsegLabels.join(", ")}`
                    : `Mancano le maschere ${g.totalsegLabels.join(", ")} — esegui la segmentazione automatica`
                }
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) => setter(e.target.checked)}
                />
                {g.label}
                {!have && <span style={{ fontSize: "0.65rem" }}> (mancante)</span>}
              </label>
            );
          })}
        </div>
        {missingForToggled().length > 0 && (
          <div
            style={{
              fontSize: "0.65rem",
              color: "#fbbf24",
              display: "flex",
              alignItems: "center",
              gap: 6,
              flexWrap: "wrap",
            }}
          >
            <span>Mancano: {missingForToggled().join(", ")}</span>
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.65rem", padding: "1px 6px" }}
              onClick={runAutoSegmentation}
              disabled={Boolean(segJobActive)}
            >
              {segJobActive ? "Segmentazione in corso…" : "Esegui segmentazione automatica"}
            </button>
          </div>
        )}
        {segJobActive && (
          <div
            style={{
              fontSize: "0.65rem",
              color: "#94a3b8",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            Esecuzione TotalSegmentator (CPU 5–15 min) · trascorsi {segJobElapsedSeconds}s
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.6rem", padding: "0 4px" }}
              title="Pulisci il marker locale (il job continua sul server)"
              onClick={() => {
                setSegJobActive(null);
                try {
                  window.localStorage.removeItem(segJobStorageKey(seriesId));
                } catch {
                  /* ignore */
                }
              }}
            >
              Reset
            </button>
          </div>
        )}
        {segJobError && (
          <div style={{ fontSize: "0.65rem", color: "#fca5a5" }}>
            Segmentazione automatica fallita: {segJobError}
          </div>
        )}
        {patientId && (
          <div
            style={{
              marginTop: 4,
              paddingTop: 4,
              borderTop: "1px dashed rgba(255,255,255,0.08)",
              display: "flex",
              flexDirection: "column",
              gap: 3,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ fontSize: "0.65rem", color: "#94a3b8" }}>
                Esclusioni manuali (fallback)
              </span>
              <button
                type="button"
                className="viewer-btn"
                style={{ fontSize: "0.6rem", padding: "1px 6px" }}
                onClick={addManualExclusion}
              >
                + Aggiungi
              </button>
            </div>
            {exclusionMarkers.length === 0 ? (
              <span style={{ fontSize: "0.6rem", color: "#94a3b8" }}>
                Nessuna esclusione manuale. Aggiungine una se la segmentazione automatica non è
                disponibile.
              </span>
            ) : (
              exclusionMarkers.map((m) => {
                const checked = activeExclusionIds.has(m.id);
                const min = (m.geometry as { min_ijk?: number[] } | null)?.min_ijk ?? [0, 0, 0];
                const max = (m.geometry as { max_ijk?: number[] } | null)?.max_ijk ?? [0, 0, 0];
                return (
                  <div
                    key={m.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 4,
                      fontSize: "0.65rem",
                      color: "#cbd5e1",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        setActiveExclusionIds((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(m.id);
                          else next.delete(m.id);
                          return next;
                        });
                      }}
                    />
                    <span style={{ fontFamily: "ui-monospace, monospace" }}>
                      [{min.join(",")}] → [{max.join(",")}]
                    </span>
                    <button
                      type="button"
                      onClick={() => removeExclusionMarker(m.id)}
                      style={{
                        marginLeft: "auto",
                        background: "transparent",
                        border: "none",
                        color: "#f66",
                        cursor: "pointer",
                        fontSize: "0.7rem",
                      }}
                      title="Elimina questa esclusione"
                    >
                      ×
                    </button>
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>

      <button
        type="button"
        className="viewer-btn"
        style={{ fontSize: "0.72rem", width: "100%" }}
        onClick={find}
        disabled={loading}
      >
        {loading ? "Finding…" : "Find hot spots"}
      </button>

      {err && (
        <div
          style={{
            fontSize: "0.7rem",
            color: "#fca5a5",
            background: "rgba(127,29,29,0.25)",
            padding: "3px 6px",
            borderRadius: 3,
            marginTop: "0.4rem",
          }}
        >
          {err}
        </div>
      )}

      {data && (
        <div style={{ marginTop: "0.5rem" }}>
          {/* Search summary so the operator can verify the criteria
              actually used. ``threshold_used`` is in native units so
              we convert to SUV when the variant is known. */}
          <div
            style={{
              fontSize: "0.65rem",
              color: "#94a3b8",
              padding: "3px 0",
              borderBottom: "1px solid rgba(255,255,255,0.08)",
              marginBottom: 3,
              lineHeight: 1.4,
            }}
          >
            {data.global_max_suv != null ? (
              <>
                Global SUVmax <strong>{fmt(data.global_max_suv)}</strong>
                {" · "}variant {data.suv_variant_used ?? "n/a"}
                <br />
                Cut-off SUV ≥{" "}
                <strong>
                  {fmt(
                    data.threshold_kind === "absolute_suv" &&
                      data.suv_variant_used != null &&
                      data.global_max_suv != null
                      ? thresholdValue
                      : data.threshold_kind === "percent_of_max" && data.global_max_suv != null
                        ? thresholdValue * data.global_max_suv
                        : Number.NaN,
                  )}
                </strong>
                {" · "}≥ {fmt(minVolumeMl, 1)} mL{" · "}
                {data.spots.length} found
              </>
            ) : (
              <>
                Cut-off (native) {fmt(data.threshold_used, 2)} · {data.spots.length} found
              </>
            )}
            {(data.slice_min_used > 0 || data.slice_max_used < data.volume_nz - 1) && (
              <>
                {" · "}slab {data.slice_min_used}–{data.slice_max_used}
              </>
            )}
          </div>

          {data.spots.length === 0 ? (
            <div
              style={{
                fontSize: "0.72rem",
                color: "#94a3b8",
                padding: "4px 0",
              }}
            >
              No regions match. Try a lower threshold, smaller min volume, or widen the slice range.
            </div>
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 2,
                fontSize: "0.72rem",
                maxHeight: 360,
                overflowY: "auto",
              }}
            >
              {data.spots.map((s) => (
                <button
                  key={s.rank}
                  type="button"
                  onClick={() => onJumpTo(s.centroid_ijk)}
                  title={`Jump to centroid (i=${s.centroid_ijk[0]}, j=${s.centroid_ijk[1]}, slice=${s.centroid_ijk[2]})`}
                  style={{
                    textAlign: "left",
                    background: "rgba(15,23,42,0.5)",
                    color: "#e6ecf3",
                    border: "1px solid rgba(255,255,255,0.1)",
                    borderRadius: 3,
                    padding: "4px 6px",
                    cursor: "pointer",
                    fontFamily: "ui-monospace, monospace",
                    display: "flex",
                    alignItems: "baseline",
                    gap: 6,
                    flexWrap: "wrap",
                  }}
                >
                  <span
                    style={{
                      color: "#e96b1f",
                      fontWeight: 600,
                      minWidth: 22,
                    }}
                  >
                    #{s.rank}
                  </span>
                  {s.suv_max !== null ? (
                    <span>
                      SUV <strong style={{ color: "#fbbf24" }}>{fmt(s.suv_max)}</strong>
                      {s.suv_peak != null && (
                        <span style={{ color: "#94a3b8" }}>
                          {" · pk "}
                          {fmt(s.suv_peak)}
                        </span>
                      )}
                      {s.suv_mean != null && (
                        <span style={{ color: "#94a3b8" }}>
                          {" · μ "}
                          {fmt(s.suv_mean)}
                        </span>
                      )}
                    </span>
                  ) : (
                    <span>
                      max <strong style={{ color: "#fbbf24" }}>{fmt(s.raw_max, 1)}</strong>
                      <span style={{ color: "#94a3b8" }}>
                        {" · μ "}
                        {fmt(s.raw_mean, 1)}
                      </span>
                    </span>
                  )}
                  <span style={{ color: "#94a3b8", marginLeft: "auto" }}>
                    {fmt(s.volume_ml, 2)} mL · ax {s.centroid_ijk[2]}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}
