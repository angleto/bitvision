"use client";

// RECIST target authoring, mounted as a drawer in the follow-up compare
// viewer. The radiologist draws a bidirectional measurement on a target
// lesion in each pane; here they turn a baseline measurement into a target
// lesion track and link the matching follow-up measurement to it. The
// ResponseAssessmentCard at the top computes the response from the tracks.

import { ApiError, type LesionTrack, findingsApi, lesionTracksApi, markersApi } from "@/lib/api";
import {
  type BidirectionalMeasurement,
  type RecistLesionType,
  type RecistPersistenceApi,
  isBelowTargetThreshold,
  persistTargetTimepoint,
} from "@/lib/recist";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import ResponseAssessmentCard from "./ResponseAssessmentCard";

/** A completed measurement emitted by ``CornerstoneMPRLayout`` (the subset
 *  we consume). Structurally compatible with the layout's emitted shape. */
export interface DrawnMeasurement {
  id: number;
  value: string;
  label?: string;
  longAxisMm?: number;
  shortAxisMm?: number;
  worldPoints?: Array<[number, number, number]>;
  frameOfReferenceUID?: string;
}

export interface PaneTimepoint {
  studyId: string;
  seriesId: string;
  frameOfReferenceUID?: string | null;
  studyDate?: string | null;
  measurements: DrawnMeasurement[];
}

interface Props {
  patientId: string;
  baseline: PaneTimepoint;
  followup: PaneTimepoint;
  registrationId?: string | null;
  onClose?: () => void;
}

// Adapter: the real API client, narrowed to what persistTargetTimepoint needs.
const persistenceApi: RecistPersistenceApi = { markers: markersApi, findings: findingsApi };

function isBidirectional(m: DrawnMeasurement): boolean {
  return m.longAxisMm !== undefined && m.shortAxisMm !== undefined;
}

// Cornerstone's annotation state is global, so each pane emits every
// annotation. Attribute a measurement to a pane by the frame of reference it
// was drawn on; when the FoR is unknown on either side we can't discriminate,
// so include it (best effort) rather than hide a real measurement.
function onPane(m: DrawnMeasurement, paneFoR: string | null | undefined): boolean {
  if (!paneFoR || !m.frameOfReferenceUID) return true;
  return m.frameOfReferenceUID === paneFoR;
}

function toMeasurement(m: DrawnMeasurement): BidirectionalMeasurement {
  return {
    longAxisMm: m.longAxisMm,
    shortAxisMm: m.shortAxisMm,
    worldPoints: m.worldPoints,
    frameOfReferenceUID: m.frameOfReferenceUID,
  };
}

export default function RecistTargetsPanel({
  patientId,
  baseline,
  followup,
  registrationId,
  onClose,
}: Props) {
  const t = useTranslations("compare");
  const tr = useTranslations("response");
  const [tracks, setTracks] = useState<LesionTrack[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadTracks = useCallback(async () => {
    try {
      const list = await lesionTracksApi.list(patientId, { recist_role: "target" });
      setTracks(list);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [patientId]);

  useEffect(() => {
    void loadTracks();
  }, [loadTracks]);

  function afterChange() {
    void loadTracks();
    setRefreshKey((k) => k + 1);
  }

  const baseBidi = baseline.measurements.filter(
    (m) => isBidirectional(m) && onPane(m, baseline.frameOfReferenceUID),
  );
  const followBidi = followup.measurements.filter(
    (m) => isBidirectional(m) && onPane(m, followup.frameOfReferenceUID),
  );

  return (
    <aside
      aria-label={t("recist.title")}
      style={{
        flex: "0 0 360px",
        maxWidth: 360,
        overflowY: "auto",
        background: "var(--bv-card-bg, #11151c)",
        color: "var(--bv-fg, #e6ecf3)",
        borderLeft: "1px solid var(--bv-card-border, #1a1f2b)",
        padding: "0.75rem",
        display: "grid",
        gap: "0.75rem",
        alignContent: "start",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <strong style={{ fontSize: "0.9rem" }}>{t("recist.title")}</strong>
        {onClose && (
          <button type="button" className="ghost" onClick={onClose} aria-label={t("recist.close")}>
            ✕
          </button>
        )}
      </div>
      <p className="meta" style={{ margin: 0, fontSize: "0.78rem" }}>
        {t("recist.measureHint")}
      </p>

      <ResponseAssessmentCard
        patientId={patientId}
        baselineStudyId={baseline.studyId}
        currentStudyId={followup.studyId}
        refreshKey={refreshKey}
      />

      {err && <p className="error">{err}</p>}

      <section style={{ display: "grid", gap: "0.4rem" }}>
        <h4 style={{ margin: 0, fontSize: "0.82rem" }}>{t("recist.baselineCandidates")}</h4>
        {baseBidi.length === 0 ? (
          <p className="meta" style={{ margin: 0, fontSize: "0.76rem" }}>
            {t("recist.noBaselineMeasurements")}
          </p>
        ) : (
          baseBidi.map((m) => (
            <DesignateRow
              key={m.id}
              measurement={m}
              disabled={busy}
              onSubmit={async ({ label, lesionType, anatomy }) => {
                setBusy(true);
                setErr(null);
                try {
                  const { findingId } = await persistTargetTimepoint(persistenceApi, {
                    patientId,
                    studyId: baseline.studyId,
                    seriesId: baseline.seriesId,
                    frameOfReferenceUID: baseline.frameOfReferenceUID,
                    measurement: toMeasurement(m),
                    lesionType,
                    anatomy: anatomy || null,
                  });
                  await lesionTracksApi.create(patientId, {
                    label,
                    recist_role: "target",
                    type: lesionType,
                    anatomy: anatomy || undefined,
                    baseline_finding_id: findingId,
                  });
                  afterChange();
                } catch (e) {
                  setErr(e instanceof ApiError ? e.message : "designate failed");
                } finally {
                  setBusy(false);
                }
              }}
            />
          ))
        )}
      </section>

      <section style={{ display: "grid", gap: "0.4rem" }}>
        <h4 style={{ margin: 0, fontSize: "0.82rem" }}>{t("recist.followupCandidates")}</h4>
        {tracks.length === 0 ? (
          <p className="meta" style={{ margin: 0, fontSize: "0.76rem" }}>
            {t("recist.designateBaselineFirst")}
          </p>
        ) : followBidi.length === 0 ? (
          <p className="meta" style={{ margin: 0, fontSize: "0.76rem" }}>
            {t("recist.noFollowupMeasurements")}
          </p>
        ) : (
          followBidi.map((m) => (
            <LinkRow
              key={m.id}
              measurement={m}
              tracks={tracks}
              disabled={busy}
              onSubmit={async (trackId) => {
                setBusy(true);
                setErr(null);
                try {
                  const { findingId } = await persistTargetTimepoint(persistenceApi, {
                    patientId,
                    studyId: followup.studyId,
                    seriesId: followup.seriesId,
                    frameOfReferenceUID: followup.frameOfReferenceUID,
                    measurement: toMeasurement(m),
                    lesionType:
                      tracks.find((tk) => tk.id === trackId)?.type === "lymph_node"
                        ? "lymph_node"
                        : "lesion",
                  });
                  await lesionTracksApi.addPoint(trackId, {
                    finding_id: findingId,
                    is_baseline: false,
                    registration_id: registrationId ?? undefined,
                  });
                  afterChange();
                } catch (e) {
                  setErr(e instanceof ApiError ? e.message : "link failed");
                } finally {
                  setBusy(false);
                }
              }}
            />
          ))
        )}
      </section>

      {tracks.length > 0 && (
        <section style={{ display: "grid", gap: "0.3rem" }}>
          <h4 style={{ margin: 0, fontSize: "0.82rem" }}>{t("recist.existingTargets")}</h4>
          {tracks.map((tk) => (
            <div key={tk.id} className="meta" style={{ fontSize: "0.76rem" }}>
              {tk.label}
              {tk.type === "lymph_node" && ` · ${tr("lesionType.node")}`} · {tk.points.length}{" "}
              {t("recist.timepoints")}
            </div>
          ))}
        </section>
      )}
    </aside>
  );
}

function DesignateRow({
  measurement,
  disabled,
  onSubmit,
}: {
  measurement: DrawnMeasurement;
  disabled: boolean;
  onSubmit: (v: { label: string; lesionType: RecistLesionType; anatomy: string }) => void;
}) {
  const t = useTranslations("compare");
  const tr = useTranslations("response");
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [lesionType, setLesionType] = useState<RecistLesionType>("lesion");
  const [anatomy, setAnatomy] = useState("");
  const below = isBelowTargetThreshold(lesionType, measurement.longAxisMm, measurement.shortAxisMm);

  return (
    <div
      style={{
        border: "1px solid var(--bv-card-border, #1a1f2b)",
        borderRadius: 6,
        padding: "0.4rem",
        display: "grid",
        gap: "0.35rem",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 6 }}>
        <span style={{ fontSize: "0.8rem" }}>{measurement.value}</span>
        {!open && (
          <button type="button" className="ghost" disabled={disabled} onClick={() => setOpen(true)}>
            {tr("designate")}
          </button>
        )}
      </div>
      {open && (
        <div style={{ display: "grid", gap: "0.35rem" }}>
          <input
            type="text"
            placeholder={t("recist.labelPlaceholder")}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            style={inputStyle}
          />
          <div style={{ display: "flex", gap: 10, fontSize: "0.78rem" }}>
            <label style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
              <input
                type="radio"
                name={`lt-${measurement.id}`}
                checked={lesionType === "lesion"}
                onChange={() => setLesionType("lesion")}
              />
              {tr("lesionType.lesion")}
            </label>
            <label style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
              <input
                type="radio"
                name={`lt-${measurement.id}`}
                checked={lesionType === "lymph_node"}
                onChange={() => setLesionType("lymph_node")}
              />
              {tr("lesionType.node")}
            </label>
          </div>
          <input
            type="text"
            placeholder={t("recist.anatomyPlaceholder")}
            value={anatomy}
            onChange={(e) => setAnatomy(e.target.value)}
            style={inputStyle}
          />
          {below && (
            <p className="meta" style={{ margin: 0, color: "var(--bv-warning, #b45309)" }}>
              {tr("threshold.tooSmall")}
            </p>
          )}
          <div style={{ display: "flex", gap: 6 }}>
            <button
              type="button"
              className="ghost"
              disabled={disabled || !label.trim()}
              onClick={() => onSubmit({ label: label.trim(), lesionType, anatomy: anatomy.trim() })}
            >
              {tr("designate")}
            </button>
            <button
              type="button"
              className="ghost"
              disabled={disabled}
              onClick={() => setOpen(false)}
            >
              {t("recist.cancel")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function LinkRow({
  measurement,
  tracks,
  disabled,
  onSubmit,
}: {
  measurement: DrawnMeasurement;
  tracks: LesionTrack[];
  disabled: boolean;
  onSubmit: (trackId: string) => void;
}) {
  const t = useTranslations("compare");
  const [trackId, setTrackId] = useState<string>(tracks[0]?.id ?? "");
  return (
    <div
      style={{
        border: "1px solid var(--bv-card-border, #1a1f2b)",
        borderRadius: 6,
        padding: "0.4rem",
        display: "grid",
        gap: "0.35rem",
      }}
    >
      <span style={{ fontSize: "0.8rem" }}>{measurement.value}</span>
      <div style={{ display: "flex", gap: 6 }}>
        <select
          aria-label={t("recist.linkTo")}
          value={trackId}
          onChange={(e) => setTrackId(e.target.value)}
          style={{ ...inputStyle, flex: 1 }}
        >
          {tracks.map((tk) => (
            <option key={tk.id} value={tk.id}>
              {tk.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="ghost"
          disabled={disabled || !trackId}
          onClick={() => onSubmit(trackId)}
        >
          {t("recist.link")}
        </button>
      </div>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  background: "#0b0e13",
  color: "#e6ecf3",
  border: "1px solid #1a1f2b",
  borderRadius: 6,
  padding: "3px 6px",
  fontSize: "0.78rem",
};
