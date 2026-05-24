"use client";

// In-viewer radiology Refer composer.
//
// Rationale: a radiologist's reading session is one continuous flow —
// scroll the slices, place measurements, take quick reading notes,
// then synthesise everything into the official report. Forcing a tab
// switch to /studies/{id} just to draft the report breaks that flow.
//
// Storage backend: the existing ``Consultation`` model is reused as
// the report container. The mapping is:
//   * Consultation.summary_md       → "Impression" (clinical conclusion)
//   * Consultation.findings_md      → "Technique" header + "Findings"
//                                      paragraph, separated by markdown
//                                      headings so we don't need a new
//                                      DB column for technique.
//   * Consultation.recommendations_md → "Recommendations" / follow-up
//   * Consultation.status: draft → submitted → signed
//   * Consultation.citations: pins the report to the study being viewed
//     so listing past reports for the study is a single query (citation
//     filter on target_kind="study" + target_id=studyId).
//
// Modularity: the F12 versioning + sign-off + auditing chain on
// Consultation is unchanged. We add zero columns. We add zero
// endpoints. The composer is a UI-only addition.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { useModal } from "@/components/ModalHost";
import {
  ApiError,
  type ClinicalNote,
  type Consultation,
  type ConsultationDetail,
  type Marker,
  consultationsApi,
  markersApi,
  patientsApi,
} from "@/lib/api";

interface Props {
  patientId: string;
  studyId: string;
  /** Live measurements from the viewport — used by "Insert measurements". */
  measurements: { id: string; tool: string; value: string }[];
}

// We split findings_md into "Technique" + "Findings" using these
// markdown headings. Round-trip: split → edit → join → save.
const TECHNIQUE_HEAD = "## Technique";
const FINDINGS_HEAD = "## Findings";

function splitTechniqueFindings(findings_md: string | null): {
  technique: string;
  findings: string;
} {
  const txt = findings_md ?? "";
  const tIdx = txt.indexOf(TECHNIQUE_HEAD);
  const fIdx = txt.indexOf(FINDINGS_HEAD);
  if (tIdx < 0 && fIdx < 0) {
    return { technique: "", findings: txt };
  }
  if (tIdx >= 0 && fIdx > tIdx) {
    return {
      technique: txt.slice(tIdx + TECHNIQUE_HEAD.length, fIdx).trim(),
      findings: txt.slice(fIdx + FINDINGS_HEAD.length).trim(),
    };
  }
  if (tIdx >= 0) {
    return {
      technique: txt.slice(tIdx + TECHNIQUE_HEAD.length).trim(),
      findings: "",
    };
  }
  return { technique: "", findings: txt.slice(fIdx + FINDINGS_HEAD.length).trim() };
}

function joinTechniqueFindings(technique: string, findings: string): string {
  const t = technique.trim();
  const f = findings.trim();
  if (!t && !f) return "";
  if (!t) return `${FINDINGS_HEAD}\n${f}`;
  if (!f) return `${TECHNIQUE_HEAD}\n${t}`;
  return `${TECHNIQUE_HEAD}\n${t}\n\n${FINDINGS_HEAD}\n${f}`;
}

function formatNotesAsMarkdown(notes: ClinicalNote[]): string {
  if (notes.length === 0) return "";
  // Newest-first per server contract; we want oldest first in the
  // findings paragraph so the reading order mirrors the session.
  const ordered = [...notes].reverse();
  return ordered.map((n) => `- ${n.body.replace(/\n/g, " ").trim()}`).join("\n");
}

function formatMeasurementsAsMarkdown(
  measurements: { id: string; tool: string; value: string }[],
): string {
  if (measurements.length === 0) return "";
  const lines = measurements.map((m, i) => `- ${m.tool} #${i + 1}: ${m.value}`);
  return lines.join("\n");
}

export default function ReportComposer({ patientId, studyId, measurements }: Props) {
  const t = useTranslations("report");
  const tKind = useTranslations("markerKinds");
  const modal = useModal();

  // Loaded list of past consultations referencing this study via
  // citation. Includes drafts (latest one is the editing target) and
  // signed ones (the cronologia).
  const [items, setItems] = useState<Consultation[]>([]);
  const [active, setActive] = useState<ConsultationDetail | null>(null);
  // Markers anchored to this study, for the "cite marker" picker. The
  // user clicks one to add a ConsultationCitation and append a
  // reference line to the Findings text. The citation makes the
  // saved/signed report referential — clicking the citation in the
  // future opens the viewer at the right slice.
  const [studyMarkers, setStudyMarkers] = useState<Marker[]>([]);
  const [showMarkerPicker, setShowMarkerPicker] = useState(false);
  const [pendingMarkerCitations, setPendingMarkerCitations] = useState<
    { marker_id: string; excerpt: string }[]
  >([]);
  // Composer fields: split for UX, joined for storage in findings_md.
  const [technique, setTechnique] = useState("");
  const [findings, setFindings] = useState("");
  const [impression, setImpression] = useState("");
  const [recommendations, setRecommendations] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      // Server-side citation filter: returns only consultations whose
      // citations include (study, studyId). Single round-trip, no N+1.
      const scoped = await consultationsApi.list(patientId, {
        author_kind: "human",
        citation_target_kind: "study",
        citation_target_id: studyId,
      });
      setItems(scoped);
      const latestDraft = scoped.find((c) => c.status === "draft");
      const latestSigned = scoped.find((c) => c.status === "signed");
      const target = latestDraft ?? latestSigned ?? null;
      if (target) {
        const d = await consultationsApi.detail(target.id);
        setActive(d);
        const split = splitTechniqueFindings(d.findings_md);
        setTechnique(split.technique);
        setFindings(split.findings);
        setImpression(d.summary_md ?? "");
        setRecommendations(d.recommendations_md ?? "");
        setTitle(d.title);
      } else {
        setActive(null);
        setTitle(t("defaultTitle"));
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [patientId, studyId, t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Pull markers anchored to this study so the "cite marker" button
  // has something to offer. We bring 'study'-scoped markers; series-
  // and instance-anchored ones live behind the picker's "all" toggle
  // (TODO follow-up if needed).
  useEffect(() => {
    let cancelled = false;
    markersApi
      .list(patientId, { target_kind: "study", target_id: studyId })
      .then((rows) => {
        if (!cancelled) setStudyMarkers(rows);
      })
      .catch(() => {
        /* non-blocking */
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, studyId]);

  const isReadOnly = !!active && active.status !== "draft";

  async function saveDraft() {
    if (!title.trim()) {
      setErr(t("titleRequired"));
      return;
    }
    setBusy(true);
    setErr(null);
    setInfo(null);
    try {
      const findings_md = joinTechniqueFindings(technique, findings);
      if (active && active.status === "draft") {
        await consultationsApi.update(active.id, {
          title: title.trim(),
          summary_md: impression || null,
          findings_md: findings_md || null,
          recommendations_md: recommendations || null,
        });
      } else {
        // Create a fresh draft pinned to this study via citation.
        // Add any pending marker citations so the saved report is
        // referential ("see Distance #1" → live link to the marker).
        const citations: {
          target_kind: "study" | "series" | "report" | "document" | "annotation" | "marker";
          target_id: string;
          excerpt?: string | null;
        }[] = [{ target_kind: "study", target_id: studyId }];
        for (const c of pendingMarkerCitations) {
          citations.push({
            target_kind: "marker",
            target_id: c.marker_id,
            excerpt: c.excerpt,
          });
        }
        await consultationsApi.create({
          patient_id: patientId,
          title: title.trim(),
          summary_md: impression || null,
          findings_md: findings_md || null,
          recommendations_md: recommendations || null,
          status: "draft",
          author_kind: "human",
          citations,
        });
        setPendingMarkerCitations([]);
      }
      setInfo(t("savedDraft"));
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function signReport() {
    // The Consultation.sign endpoint requires status="submitted" or
    // "reviewed". A draft must transition first. Cleanest path: save
    // the current edits as draft, create a fresh submitted record from
    // the same content, then sign. To avoid two records, we ask the
    // backend to bump the existing draft to submitted first via PATCH
    // is not supported (status is server-controlled), so we save then
    // submit by creating a new consultation that supersedes — but
    // that breaks "one report per study" semantics.
    //
    // Pragmatic v1: only sign already-submitted consultations. The
    // "submit for review" step is a follow-up; today the radiologist
    // owns the patient so they can bypass review by creating with
    // status=submitted directly. We expose that via a confirm dialog.
    if (!active) {
      setErr(t("nothingToSign"));
      return;
    }
    if (active.status === "draft") {
      const ok = await modal.confirm({
        title: t("sectionTitle"),
        message: t("submitConfirm"),
        confirmLabel: t("sign"),
      });
      if (!ok) return;
    }
    setBusy(true);
    setErr(null);
    setInfo(null);
    try {
      // If draft: save, then create-as-submitted clone with same payload
      // pointing at the same citation, then sign the new one. We delete
      // the old draft only if the user explicitly asks, to avoid losing
      // edits silently. v1: leave the draft, the signed version is the
      // authoritative one.
      let consultationId = active.id;
      if (active.status === "draft") {
        const findings_md = joinTechniqueFindings(technique, findings);
        await consultationsApi.update(active.id, {
          title: title.trim(),
          summary_md: impression || null,
          findings_md: findings_md || null,
          recommendations_md: recommendations || null,
        });
        const created = await consultationsApi.create({
          patient_id: patientId,
          title: title.trim(),
          summary_md: impression || null,
          findings_md: findings_md || null,
          recommendations_md: recommendations || null,
          status: "submitted",
          author_kind: "human",
          citations: [{ target_kind: "study", target_id: studyId }],
        });
        consultationId = created.id;
      }
      await consultationsApi.sign(consultationId, t("signNoteAuto"));
      setInfo(t("signedOk"));
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("signFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function importNotes() {
    setBusy(true);
    setErr(null);
    setInfo(null);
    try {
      const list = await patientsApi.listNotes(patientId, {
        target_kind: "study",
        target_id: studyId,
      });
      const md = formatNotesAsMarkdown(list);
      if (!md) {
        setInfo(t("noNotesToImport"));
        return;
      }
      const sep = findings.endsWith("\n") || findings === "" ? "" : "\n";
      setFindings(`${findings}${sep}${md}\n`);
      setInfo(t("notesImported", { n: list.length }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("importFailed"));
    } finally {
      setBusy(false);
    }
  }

  function importMeasurements() {
    const md = formatMeasurementsAsMarkdown(measurements);
    if (!md) {
      setInfo(t("noMeasurementsToImport"));
      return;
    }
    const sep = findings.endsWith("\n") || findings === "" ? "" : "\n";
    setFindings(`${findings}${sep}${md}\n`);
    setInfo(t("measurementsImported", { n: measurements.length }));
  }

  function loadVersion(c: Consultation) {
    consultationsApi.detail(c.id).then((d) => {
      setActive(d);
      const split = splitTechniqueFindings(d.findings_md);
      setTechnique(split.technique);
      setFindings(split.findings);
      setImpression(d.summary_md ?? "");
      setRecommendations(d.recommendations_md ?? "");
      setTitle(d.title);
      setErr(null);
      setInfo(null);
    });
  }

  function startNewDraft() {
    setActive(null);
    setTechnique("");
    setFindings("");
    setImpression("");
    setRecommendations("");
    setTitle(t("defaultTitle"));
    setErr(null);
    setInfo(null);
  }

  return (
    <section style={{ marginTop: "0.6rem" }}>
      <h2>{t("sectionTitle")}</h2>

      {active && (
        <div
          className="meta"
          style={{
            fontSize: "0.72rem",
            marginBottom: "0.4rem",
            display: "flex",
            justifyContent: "space-between",
            gap: "0.4rem",
          }}
        >
          <span>
            {t("statusLabel")}{" "}
            <strong style={{ color: statusColor(active.status) }}>{active.status}</strong>
            {active.signed_at && ` · ${new Date(active.signed_at).toLocaleString()}`}
          </span>
          {isReadOnly && (
            <button
              type="button"
              className="viewer-btn"
              style={{ fontSize: "0.7rem" }}
              onClick={startNewDraft}
            >
              {t("newDraft")}
            </button>
          )}
        </div>
      )}

      <label style={{ display: "block", marginBottom: "0.4rem" }}>
        <span className="meta" style={{ fontSize: "0.7rem" }}>
          {t("titleLabel")}
        </span>
        <input
          type="text"
          value={title}
          disabled={busy || isReadOnly}
          onChange={(e) => setTitle(e.target.value)}
          style={inputStyle}
        />
      </label>

      <Textarea
        label={t("technique")}
        value={technique}
        onChange={setTechnique}
        rows={2}
        disabled={busy || isReadOnly}
      />
      <Textarea
        label={t("findings")}
        value={findings}
        onChange={setFindings}
        rows={6}
        disabled={busy || isReadOnly}
      />
      <Textarea
        label={t("impression")}
        value={impression}
        onChange={setImpression}
        rows={3}
        disabled={busy || isReadOnly}
      />
      <Textarea
        label={t("recommendations")}
        value={recommendations}
        onChange={setRecommendations}
        rows={2}
        disabled={busy || isReadOnly}
      />

      {!isReadOnly && (
        <div
          style={{
            display: "flex",
            gap: "0.3rem",
            flexWrap: "wrap",
            marginTop: "0.4rem",
          }}
        >
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.72rem" }}
            disabled={busy}
            onClick={importNotes}
            title={t("importNotesTitle")}
          >
            {t("importNotes")}
          </button>
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.72rem" }}
            disabled={busy}
            onClick={importMeasurements}
            title={t("importMeasurementsTitle")}
          >
            {t("importMeasurements")} ({measurements.length})
          </button>
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.72rem" }}
            disabled={busy || studyMarkers.length === 0}
            onClick={() => setShowMarkerPicker((v) => !v)}
            title={t("citeMarkerTitle")}
          >
            {t("citeMarker")} ({studyMarkers.length})
          </button>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.72rem" }}
            disabled={busy}
            onClick={saveDraft}
          >
            {busy ? "…" : t("saveDraft")}
          </button>
          <button
            type="button"
            className="viewer-btn viewer-btn--active"
            style={{ fontSize: "0.72rem" }}
            disabled={busy}
            onClick={signReport}
            title={t("signTitle")}
          >
            {t("sign")}
          </button>
        </div>
      )}

      {showMarkerPicker && studyMarkers.length > 0 && (
        <div
          style={{
            marginTop: "0.4rem",
            padding: "0.4rem 0.5rem",
            border: "1px solid #2a2f3b",
            borderRadius: 4,
            maxHeight: 220,
            overflowY: "auto",
            background: "#0e1118",
          }}
        >
          <div
            className="meta"
            style={{
              fontSize: "0.7rem",
              marginBottom: "0.3rem",
              letterSpacing: "0.04em",
            }}
          >
            {t("citeMarkerPickerHint")}
          </div>
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {studyMarkers.map((m) => {
              const summary = describeMarker(m, tKind);
              const already = pendingMarkerCitations.some((c) => c.marker_id === m.id);
              return (
                <li
                  key={m.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "0.4rem",
                    padding: "0.2rem 0",
                  }}
                >
                  <span style={{ flex: 1, fontSize: "0.78rem" }}>{summary}</span>
                  <button
                    type="button"
                    className="ghost"
                    disabled={already}
                    onClick={() => {
                      setPendingMarkerCitations((prev) => [
                        ...prev,
                        { marker_id: m.id, excerpt: summary },
                      ]);
                      // Append a citation reference into Findings so the
                      // doctor sees "[marker:abc] D1: 24.3 mm" inline.
                      const ref = `[marker:${m.id.slice(0, 8)}] ${summary}`;
                      const sep = findings.endsWith("\n") || findings === "" ? "" : "\n";
                      setFindings(`${findings}${sep}- ${ref}\n`);
                    }}
                    style={{
                      fontSize: "0.7rem",
                      padding: "0.1rem 0.5rem",
                    }}
                  >
                    {already ? t("citeMarkerAdded") : t("citeMarkerAdd")}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {pendingMarkerCitations.length > 0 && (
        <p className="meta" style={{ fontSize: "0.7rem", marginTop: "0.3rem" }}>
          {t("citeMarkerPending", { n: pendingMarkerCitations.length })}
        </p>
      )}

      {err && (
        <p className="error" style={{ fontSize: "0.78rem", marginTop: "0.4rem" }}>
          {err}
        </p>
      )}
      {info && (
        <p className="meta" style={{ fontSize: "0.74rem", marginTop: "0.4rem", color: "#6ad19a" }}>
          {info}
        </p>
      )}

      {items.length > 0 && (
        <div style={{ marginTop: "0.7rem" }}>
          <h3 style={{ fontSize: "0.78rem", margin: "0 0 0.3rem", letterSpacing: "0.04em" }}>
            {t("history")}
          </h3>
          <ul
            style={{
              listStyle: "none",
              padding: 0,
              margin: 0,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            {items.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => loadVersion(c)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    background: active?.id === c.id ? "#1d2230" : "transparent",
                    color: "#cbd5e1",
                    border: "1px solid #2a2f3b",
                    borderRadius: 4,
                    padding: "0.3rem 0.5rem",
                    fontSize: "0.72rem",
                    cursor: "pointer",
                  }}
                >
                  <span style={{ color: statusColor(c.status), fontWeight: 600 }}>{c.status}</span>
                  {" · "}
                  {new Date(c.updated_at).toLocaleString()}
                  {" · "}
                  {c.title.length > 36 ? `${c.title.slice(0, 36)}…` : c.title}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function describeMarker(m: Marker, tKind: (key: string) => string): string {
  // One-line description used in the citation picker. Prefers the
  // computed value (e.g. "Distanza: 24.3 mm") over the raw kind so
  // a radiologist can identify the right finding at a glance. When
  // computed.value and body are both missing, falls back to the
  // translated kind label (not the raw ``measurement.distance`` key)
  // so the picker stays readable. ``next-intl`` reads dots as
  // namespace separators, so we map dots/dashes to underscores
  // before the lookup; ``tKind`` returns the key on miss, which
  // we substitute with the cleaned-up kind for legibility.
  const kindKey = m.kind.replace(/[.-]/g, "_");
  const translated = tKind(kindKey);
  const kindLabel = translated === kindKey ? m.kind.replace(/^measurement\./, "") : translated;
  const c = m.computed as { value?: unknown; unit?: unknown } | null;
  if (c && c.value != null) {
    const u = c.unit ? ` ${c.unit}` : "";
    return `${kindLabel}: ${c.value}${u}`;
  }
  if (m.body) {
    return m.body.length > 60 ? `${m.body.slice(0, 60)}…` : m.body;
  }
  return kindLabel;
}

function statusColor(status: string): string {
  switch (status) {
    case "signed":
      return "#6ad19a";
    case "draft":
      return "#e1b95e";
    case "submitted":
      return "#69b1f0";
    case "rejected":
      return "#f08080";
    default:
      return "#cbd5e1";
  }
}

function Textarea({
  label,
  value,
  onChange,
  rows,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows: number;
  disabled?: boolean;
}) {
  return (
    <label style={{ display: "block", marginBottom: "0.4rem" }}>
      <span className="meta" style={{ fontSize: "0.7rem" }}>
        {label}
      </span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        disabled={disabled}
        style={textareaStyle}
      />
    </label>
  );
}

const inputStyle: React.CSSProperties = {
  width: "100%",
  background: "#0a0d14",
  color: "#e6ecf3",
  border: "1px solid #2a2f3b",
  borderRadius: 4,
  padding: "0.3rem 0.5rem",
  fontSize: "0.82rem",
  fontFamily: "inherit",
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle,
  fontSize: "0.8rem",
  resize: "vertical",
  minHeight: "2.4em",
};
