"use client";

// Structured Finding authoring panel (annotation overhaul P4).
//
// Turns the viewer from "draws untyped markers" into "captures coded,
// queryable findings": a vocab-driven form (finding type / anatomy /
// laterality / morphology + typed measurements) that POSTs a Finding to
// the backend, plus a list of the study's findings with AI-provenance
// badges and delete. The controlled vocabulary is fetched once from
// /api/findings/vocab so the slugs always match the backend.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useModal } from "@/components/ModalHost";
import {
  ApiError,
  type Finding,
  type FindingLaterality,
  type FindingMeasurements,
  type FindingStatus,
  type FindingVocab,
  findingsApi,
} from "@/lib/api";
import { colorForCategory } from "@/lib/findingColors";

interface Props {
  patientId: string;
  studyId: string;
  seriesId: string;
  frameOfReferenceUid?: string | null;
  refreshKey?: number;
}

const _LATERALITIES: FindingLaterality[] = ["left", "right", "bilateral", "midline"];
const _STATUSES: FindingStatus[] = ["candidate", "confirmed", "retracted"];
// Order shown in the measurement grid (keys must exist on FindingMeasurements).
const _MEASUREMENT_FIELDS: Array<keyof FindingMeasurements> = [
  "longest_diameter_mm",
  "short_axis_mm",
  "volume_ml",
  "suv_max",
  "suv_peak",
  "suv_mean",
  "hu_mean",
  "hu_std",
];

export default function FindingPanel({
  patientId,
  studyId,
  seriesId,
  frameOfReferenceUid,
  refreshKey,
}: Props) {
  const t = useTranslations("findingPanel");
  const modal = useModal();

  const [vocab, setVocab] = useState<FindingVocab | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Form state.
  const [typeKey, setTypeKey] = useState("");
  const [anatomyKey, setAnatomyKey] = useState("");
  const [laterality, setLaterality] = useState<FindingLaterality | "">("");
  const [morphology, setMorphology] = useState<Set<string>>(() => new Set());
  const [status, setStatus] = useState<FindingStatus>("candidate");
  const [description, setDescription] = useState("");
  const [meas, setMeas] = useState<Record<string, number>>({});

  // Vocab is immutable for the session — fetch once.
  useEffect(() => {
    let cancelled = false;
    findingsApi
      .getVocab()
      .then((v) => {
        if (!cancelled) setVocab(v);
      })
      .catch(() => {
        if (!cancelled) setError(t("loadFailed"));
      });
    return () => {
      cancelled = true;
    };
  }, [t]);

  const reload = useCallback(async () => {
    try {
      const rows = await findingsApi.list(patientId, { study_id: studyId, limit: 500 });
      setFindings(rows);
    } catch {
      setError(t("loadFailed"));
    }
  }, [patientId, studyId, t]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey is a manual refresh trigger bumped by the parent (canvas to panel sync).
  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const typeDisplay = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of vocab?.finding_types ?? []) m.set(x.key, x.display);
    return m;
  }, [vocab]);
  const anatomyDisplay = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of vocab?.anatomy_sites ?? []) m.set(x.key, x.display);
    return m;
  }, [vocab]);
  // Finding type key → category, so each row can show a class colour chip
  // matching the on-canvas annotation colour (task cde63ced).
  const typeCategory = useMemo(() => {
    const m = new Map<string, string>();
    for (const x of vocab?.finding_types ?? []) m.set(x.key, x.category);
    return m;
  }, [vocab]);

  const resetForm = () => {
    setTypeKey("");
    setAnatomyKey("");
    setLaterality("");
    setMorphology(new Set());
    setStatus("candidate");
    setDescription("");
    setMeas({});
  };

  const submit = async () => {
    if (!typeKey) {
      setError(t("typeRequired"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const measurements: FindingMeasurements = {};
      for (const f of _MEASUREMENT_FIELDS) {
        const v = meas[f];
        if (typeof v === "number" && Number.isFinite(v)) measurements[f] = v;
      }
      await findingsApi.create(patientId, {
        study_id: studyId,
        series_id: seriesId,
        frame_of_reference_uid: frameOfReferenceUid ?? null,
        type: typeKey,
        anatomy: anatomyKey || null,
        laterality: laterality || null,
        morphology: [...morphology],
        status,
        description: description.trim() || null,
        ...measurements,
      });
      resetForm();
      await reload();
    } catch (e) {
      setError(e instanceof ApiError ? `${t("createFailed")} (${e.status})` : t("createFailed"));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (f: Finding) => {
    const ok = await modal.confirm({
      message: t("deleteConfirm"),
      destructive: true,
    });
    if (!ok) return;
    try {
      await findingsApi.remove(f.id, f.etag);
      await reload();
    } catch {
      setError(t("loadFailed"));
    }
  };

  const labelStyle = { fontSize: "0.72rem", display: "block" } as const;
  const selectStyle = { display: "block", marginTop: "0.2rem", width: "100%" } as const;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.7rem" }}>
      {error && <div className="error">{error}</div>}

      {/* Existing findings */}
      {findings.length === 0 ? (
        <div className="meta">{t("empty")}</div>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {findings.map((f) => (
            <li
              key={f.id}
              style={{
                display: "flex",
                gap: "0.4rem",
                alignItems: "baseline",
                padding: "0.3rem 0",
                borderBottom: "1px solid #2a2f3b",
                fontSize: "0.78rem",
              }}
            >
              <span
                aria-hidden="true"
                title={f.type}
                style={{
                  display: "inline-block",
                  width: "0.7rem",
                  height: "0.7rem",
                  borderRadius: 2,
                  flexShrink: 0,
                  alignSelf: "center",
                  background: colorForCategory(typeCategory.get(f.type)),
                }}
              />
              <span style={{ flex: 1 }}>
                <strong>{typeDisplay.get(f.type) ?? f.type}</strong>
                {f.anatomy && <> · {anatomyDisplay.get(f.anatomy) ?? f.anatomy}</>}
                {f.laterality && <> ({t(`laterality_${f.laterality}`)})</>}
                {f.morphology.length > 0 && (
                  <span className="meta"> · {f.morphology.join(", ")}</span>
                )}
                {typeof f.longest_diameter_mm === "number" && (
                  <span className="meta"> · {f.longest_diameter_mm} mm</span>
                )}
                {typeof f.suv_max === "number" && (
                  <span className="meta"> · SUVmax {f.suv_max}</span>
                )}
                {f.author_kind !== "human" && (
                  <span className="badge" title={f.model_id ?? f.author_kind}>
                    {t("aiBadge")}
                  </span>
                )}
              </span>
              <button
                type="button"
                className="viewer-btn"
                style={{ color: "var(--bv-danger, #d6322e)" }}
                onClick={() => void remove(f)}
              >
                {t("delete")}
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Add finding */}
      <div style={{ display: "flex", flexDirection: "column", gap: "0.45rem" }}>
        <h3 style={{ fontSize: "0.82rem", margin: 0 }}>{t("addTitle")}</h3>

        <label className="meta" style={labelStyle}>
          {t("fieldType")} *
          <select value={typeKey} onChange={(e) => setTypeKey(e.target.value)} style={selectStyle}>
            <option value="">{t("selectPlaceholder")}</option>
            {(vocab?.finding_types ?? []).map((x) => (
              <option key={x.key} value={x.key}>
                {x.display}
              </option>
            ))}
          </select>
        </label>

        <label className="meta" style={labelStyle}>
          {t("fieldAnatomy")}
          <select
            value={anatomyKey}
            onChange={(e) => setAnatomyKey(e.target.value)}
            style={selectStyle}
          >
            <option value="">{t("selectPlaceholder")}</option>
            {(vocab?.anatomy_sites ?? []).map((x) => (
              <option key={x.key} value={x.key}>
                {x.display}
              </option>
            ))}
          </select>
        </label>

        <label className="meta" style={labelStyle}>
          {t("fieldLaterality")}
          <select
            value={laterality}
            onChange={(e) => setLaterality(e.target.value as FindingLaterality | "")}
            style={selectStyle}
          >
            <option value="">{t("selectPlaceholder")}</option>
            {_LATERALITIES.map((l) => (
              <option key={l} value={l}>
                {t(`laterality_${l}`)}
              </option>
            ))}
          </select>
        </label>

        {(vocab?.morphology_terms ?? []).length > 0 && (
          <fieldset style={{ border: "1px solid #2a2f3b", borderRadius: 5, padding: "0.4rem" }}>
            <legend className="meta" style={{ fontSize: "0.72rem" }}>
              {t("fieldMorphology")}
            </legend>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
              {(vocab?.morphology_terms ?? []).map((x) => (
                <label key={x.key} style={{ fontSize: "0.74rem", display: "flex", gap: "0.25rem" }}>
                  <input
                    type="checkbox"
                    checked={morphology.has(x.key)}
                    onChange={(e) => {
                      setMorphology((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) next.add(x.key);
                        else next.delete(x.key);
                        return next;
                      });
                    }}
                  />
                  {x.display}
                </label>
              ))}
            </div>
          </fieldset>
        )}

        <fieldset style={{ border: "1px solid #2a2f3b", borderRadius: 5, padding: "0.4rem" }}>
          <legend className="meta" style={{ fontSize: "0.72rem" }}>
            {t("fieldMeasurements")}
          </legend>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.35rem" }}>
            {_MEASUREMENT_FIELDS.map((f) => (
              <label key={f} className="meta" style={{ fontSize: "0.68rem" }}>
                {t(`meas_${f}`)}
                <input
                  type="number"
                  inputMode="decimal"
                  step="any"
                  value={meas[f] ?? ""}
                  onChange={(e) => {
                    const raw = e.target.value;
                    setMeas((prev) => {
                      const next = { ...prev };
                      if (raw === "") delete next[f];
                      else next[f] = Number.parseFloat(raw);
                      return next;
                    });
                  }}
                  style={{ display: "block", marginTop: "0.15rem", width: "100%" }}
                />
              </label>
            ))}
          </div>
        </fieldset>

        <label className="meta" style={labelStyle}>
          {t("fieldStatus")}
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as FindingStatus)}
            style={selectStyle}
          >
            {_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`status_${s}`)}
              </option>
            ))}
          </select>
        </label>

        <label className="meta" style={labelStyle}>
          {t("fieldDescription")}
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            style={{ display: "block", marginTop: "0.2rem", width: "100%" }}
          />
        </label>

        <button
          type="button"
          className="viewer-btn"
          disabled={busy || !typeKey}
          onClick={() => void submit()}
        >
          {busy ? t("creating") : t("create")}
        </button>
      </div>
    </div>
  );
}
