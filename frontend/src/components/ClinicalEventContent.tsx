"use client";

// Render-side of the clinical-event detail. Decoupled from a specific
// Next route so both the legacy ``/clinical-events/:id`` page (for
// backward-compat redirect + render fallback) and the canonical
// patient-namespaced ``/patients/:pid/clinical-events/:eid`` page
// can share it.
//
// Description is rendered through ``EvidenceContent`` so the
// ``@kind:UUID`` mention DSL (links to studies, documents, reports,
// folders, consultations, tags) round-trips as clickable pills, the
// same way it does in clinical notes and report syntheses. Edit mode
// reuses ``EvidenceEditor`` (TipTap WYSIWYG + raw markdown toggle +
// patient-scoped @-autocomplete); the cross-patient guard is enforced
// server-side at PATCH time and the 422 violations are surfaced inline
// above the editor.
//
// A "last edited by" chip below the description reads the most recent
// ``provenance_events`` row for this clinical_event so the reader can
// tell at a glance whether the latest write was a human or an AI
// assistant.

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import EvidenceContent from "@/components/EvidenceContent";
import EvidenceEditor from "@/components/EvidenceEditor";
import { useModal } from "@/components/ModalHost";
import ReportContentDetail from "@/components/ReportContentDetail";
import { ApiError } from "@/lib/api";
import {
  type ClinicalEvent,
  type ProvenanceEvent,
  type ReportContent,
  clinicalEventsApi,
  provenanceApi,
  reportContentsApi,
} from "@/lib/api_records";
import { authoritativeInstant, formatInZone } from "@/lib/event_dates";
import { type EvidenceLinkViolation, parseEvidenceLinkError } from "@/lib/evidenceLinks";

interface Props {
  eventId: string;
  /** Same opt-in pattern as ``StudyDetailContent``: the
   *  patient-namespaced route hands down the event it already fetched
   *  for cross-patient guarding so this component skips the round-trip. */
  initialEvent?: ClinicalEvent;
}

export default function ClinicalEventContent({ eventId, initialEvent }: Props) {
  const search = useSearchParams();
  const router = useRouter();
  const modal = useModal();
  const t = useTranslations("clinicalEventDetail");
  const locale = useLocale();
  const [event, setEvent] = useState<ClinicalEvent | null>(initialEvent ?? null);
  const [contents, setContents] = useState<ReportContent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [lastProv, setLastProv] = useState<ProvenanceEvent | null>(null);

  // Description edit state. ``editing`` flips the description block
  // into ``EvidenceEditor``; ``draft`` is the controlled markdown
  // string; ``linkErrors`` carries the 422 ``violations[]`` so the
  // editor can highlight them inline.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saveBusy, setSaveBusy] = useState(false);
  const [linkErrors, setLinkErrors] = useState<EvidenceLinkViolation[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);

  const KIND_KEY: Record<string, string> = {
    imaging_study: "kindImaging",
    surgical_procedure: "kindSurgical",
    outpatient_visit: "kindOutpatient",
    inpatient_admission: "kindInpatient",
    lab_batch: "kindLab",
    consultation_event: "kindConsult",
    other: "kindOther",
  };

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [ev, rcs] = await Promise.all([
        clinicalEventsApi.read(eventId),
        reportContentsApi.listForEvent(eventId),
      ]);
      setEvent(ev);
      setContents(rcs);
    } catch (e: unknown) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : t("errorLoad"));
    }
  }, [eventId, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Latest provenance entry for the "last edited by" chip. We only
  // need the head of the list; the backend already orders newest
  // first. The fetch is best-effort: a failure (e.g. transient 404
  // for an event without provenance yet) should not block the page.
  // Re-runs when ``eventId`` changes; explicit save flow already
  // refreshes the chip locally so a stale ``etag`` here is fine.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await provenanceApi.read("clinical_event", eventId, { limit: 1 });
        if (!cancelled) setLastProv(rows[0] ?? null);
      } catch {
        if (!cancelled) setLastProv(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [eventId]);

  const handleDelete = useCallback(async () => {
    if (!event) return;
    if (event.kind === "imaging_study") return;
    const linkedCount = contents?.filter((c) => c.status !== "stale").length ?? 0;
    const warning = linkedCount > 0 ? t("deleteConfirmCascade", { n: linkedCount }) : "";
    const ok = await modal.confirm({
      title: t("deleteConfirmTitle"),
      message: t("deleteConfirmBody", { title: event.title, warning }),
      confirmLabel: t("deleteConfirmBtn"),
      destructive: true,
    });
    if (!ok) return;
    setDeleting(true);
    setError(null);
    try {
      await clinicalEventsApi.remove(event.id, event.etag);
      const from = search.get("from");
      if (from === "care-phase") {
        const slug = search.get("phase");
        if (slug) {
          router.push(`/patients/${event.patient_id}/care-phases/${slug}`);
          return;
        }
      }
      router.push(`/patients/${event.patient_id}?view=events`);
    } catch (e: unknown) {
      setDeleting(false);
      if (e instanceof ApiError) {
        if (e.status === 412) {
          setError(t("deleteEtagHint"));
        } else if (e.status === 409) {
          setError(t("deleteImagingHint"));
        } else {
          setError(`${e.status}: ${e.message}`);
        }
      } else {
        setError(t("errorDelete"));
      }
    }
  }, [event, contents, modal, router, search, t]);

  const startEdit = useCallback(() => {
    if (!event) return;
    setDraft(event.narrative ?? "");
    setLinkErrors([]);
    setSaveError(null);
    setEditing(true);
  }, [event]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setLinkErrors([]);
    setSaveError(null);
    setDraft("");
  }, []);

  const handleSave = useCallback(async () => {
    if (!event) return;
    setSaveBusy(true);
    setSaveError(null);
    setLinkErrors([]);
    try {
      const trimmed = draft.trim();
      const updated = await clinicalEventsApi.update(
        event.id,
        { narrative: trimmed.length === 0 ? null : trimmed },
        event.etag,
      );
      setEvent(updated);
      setEditing(false);
      // Best-effort refresh of the provenance head so the chip below
      // reflects the write the user just performed without forcing a
      // full page reload.
      try {
        const rows = await provenanceApi.read("clinical_event", event.id, { limit: 1 });
        setLastProv(rows[0] ?? null);
      } catch {
        // ignore
      }
    } catch (e: unknown) {
      if (e instanceof ApiError) {
        if (e.status === 412) {
          setSaveError(t("concurrentEditError"));
        } else if (e.status === 422) {
          const parsed = parseEvidenceLinkError(e.detail);
          if (parsed) {
            setLinkErrors(parsed.violations);
          } else {
            setSaveError(`${e.status}: ${e.message}`);
          }
        } else {
          setSaveError(`${e.status}: ${e.message}`);
        }
      } else {
        setSaveError(t("saveDescriptionError"));
      }
    } finally {
      setSaveBusy(false);
    }
  }, [event, draft, t]);

  if (error && !event) {
    return (
      <main style={{ padding: "1rem 1.5rem" }}>
        <p role="alert" style={{ color: "#c00" }}>
          {error}
        </p>
      </main>
    );
  }
  if (!event || !contents) {
    return (
      <main style={{ padding: "1rem 1.5rem" }}>
        <p>{t("loading")}</p>
      </main>
    );
  }

  // Group contents by authority for clearer rendering.
  const synthesis = contents.filter(
    (c) => c.authority === "canonical_synthesis" && c.status !== "stale",
  );
  const originals = contents.filter((c) => c.authority === "original" && c.status !== "stale");
  const derived = contents.filter((c) => c.authority === "derived" && c.status !== "stale");
  const stale = contents.filter((c) => c.status === "stale");

  // Deep-link from the Chiedi tab arrives with ``#rc-<id>`` in the URL.
  // We expose the id to every ``ReportContentDetail`` as a ``highlight``
  // prop; the matching card scrolls into view and pulses for 2 seconds.
  // Read once the contents list is non-null (so the targeted card has
  // actually mounted) to avoid scrolling before the card exists.
  const highlightRcId =
    typeof window !== "undefined" && window.location.hash.startsWith("#rc-")
      ? window.location.hash.slice("#rc-".length)
      : null;

  const hasNarrative = !!(event.narrative && event.narrative.trim().length > 0);
  // The instant the record is anchored on, in the event's own zone. Falls
  // back to the standalone DATE only for rows that never had a time.
  const whenLabel = formatInZone(
    authoritativeInstant(event) ?? event.event_date,
    locale,
    event.timezone,
  );

  return (
    <main style={{ padding: "1rem 1.5rem", maxWidth: "1100px" }}>
      {error && (
        <p
          role="alert"
          style={{
            color: "#c00",
            background: "rgba(204,0,0,0.06)",
            border: "1px solid rgba(204,0,0,0.2)",
            padding: "0.5rem 0.75rem",
            borderRadius: 4,
            marginBottom: "0.75rem",
          }}
        >
          {error}
        </p>
      )}
      <nav style={{ marginBottom: "1rem" }}>
        {(() => {
          const from = search.get("from");
          if (from === "care-phase") {
            const slug = search.get("phase");
            const name = search.get("phaseName") ?? slug ?? "";
            if (slug) {
              return (
                <>
                  <Link href={`/patients/${event.patient_id}/care-phases/${slug}`}>
                    &larr; {name ? t("phaseLabel", { name }) : t("phaseGeneric")}
                  </Link>
                  {" · "}
                  <Link
                    href={`/patients/${event.patient_id}?view=events`}
                    style={{ color: "var(--bv-muted)" }}
                  >
                    {t("timelineEvents")}
                  </Link>
                  {" · "}
                  <Link href={`/patients/${event.patient_id}`} style={{ color: "var(--bv-muted)" }}>
                    {t("patientRecord")}
                  </Link>
                </>
              );
            }
          }
          if (from === "timeline") {
            return (
              <>
                <Link href={`/patients/${event.patient_id}?view=events`}>
                  &larr; {t("timelineEvents")}
                </Link>
                {" · "}
                <Link href={`/patients/${event.patient_id}`} style={{ color: "var(--bv-muted)" }}>
                  {t("patientRecord")}
                </Link>
              </>
            );
          }
          return (
            <>
              <Link href={`/patients/${event.patient_id}`}>&larr; {t("patientRecord")}</Link>
              {" · "}
              <Link
                href={`/patients/${event.patient_id}?view=events`}
                style={{ color: "var(--bv-muted)" }}
              >
                {t("timelineEvents")}
              </Link>
            </>
          );
        })()}
      </nav>

      <header style={{ marginBottom: "1rem" }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: "1rem",
          }}
        >
          <h1 style={{ margin: 0 }}>{event.title}</h1>
          {event.kind !== "imaging_study" && (
            <button
              type="button"
              onClick={() => void handleDelete()}
              disabled={deleting}
              style={{
                background: "transparent",
                border: "1px solid #c00",
                color: "#c00",
                borderRadius: 4,
                padding: "0.35rem 0.75rem",
                cursor: deleting ? "wait" : "pointer",
                fontSize: "0.85rem",
                whiteSpace: "nowrap",
              }}
            >
              {deleting ? t("deleteBusy") : t("deleteConfirmBtn")}
            </button>
          )}
        </div>
        <p style={{ color: "var(--muted-fg, #666)", margin: "0.25rem 0" }}>
          {KIND_KEY[event.kind] ? t(KIND_KEY[event.kind]) : event.kind}
          {/* The anchor instant when the row has one, in the event's own
           * zone; the bare DATE only for rows that genuinely have no time.
           * Printing the raw ``event_date`` showed an ISO string and hid
           * the hour the appointment actually carries. */}
          {whenLabel ? ` — ${whenLabel}` : ""}
          {event.body_part ? t("districtSuffix", { bodyPart: event.body_part }) : ""}
        </p>

        <DescriptionBlock
          patientId={event.patient_id}
          narrative={event.narrative}
          editing={editing}
          draft={draft}
          onDraftChange={setDraft}
          onStartEdit={startEdit}
          onCancel={cancelEdit}
          onSave={handleSave}
          busy={saveBusy}
          linkErrors={linkErrors}
          saveError={saveError}
          hasNarrative={hasNarrative}
          t={t}
        />

        {!editing && lastProv && <LastEditedChip prov={lastProv} t={t} />}

        <p style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}>
          <Link href={`/provenance/clinical_event/${event.id}`}>{t("viewProvenance")}</Link>
        </p>
        {event.imaging_study_id && (
          <p style={{ fontSize: "0.85rem" }}>
            <Link href={`/patients/${event.patient_id}/studies/${event.imaging_study_id}`}>
              {t("openLinkedStudy")}
            </Link>
          </p>
        )}
      </header>

      <section>
        <h2>{t("synthesisHeading")}</h2>
        {synthesis.length === 0 ? (
          <p style={{ color: "var(--muted-fg, #666)" }}>{t("synthesisEmpty")}</p>
        ) : (
          synthesis.map((rc) => (
            <ReportContentDetail
              key={rc.id}
              rc={rc}
              onChanged={refresh}
              patientId={event.patient_id}
              eventId={event.id}
              highlight={rc.id === highlightRcId}
            />
          ))
        )}
      </section>

      <section>
        <h2>{t("originalReports", { n: originals.length })}</h2>
        {originals.length === 0 ? (
          <p style={{ color: "var(--muted-fg, #666)" }}>{t("originalsEmpty")}</p>
        ) : (
          originals.map((rc) => (
            <ReportContentDetail
              key={rc.id}
              rc={rc}
              onChanged={refresh}
              patientId={event.patient_id}
              eventId={event.id}
              highlight={rc.id === highlightRcId}
            />
          ))
        )}
      </section>

      {derived.length > 0 && (
        <section>
          <h2>{t("derivedHeading", { n: derived.length })}</h2>
          {derived.map((rc) => (
            <ReportContentDetail
              key={rc.id}
              rc={rc}
              onChanged={refresh}
              patientId={event.patient_id}
              eventId={event.id}
              highlight={rc.id === highlightRcId}
            />
          ))}
        </section>
      )}

      {stale.length > 0 && (
        <section>
          <h2>{t("staleHeading")}</h2>
          <details open={stale.some((rc) => rc.id === highlightRcId)}>
            <summary>{t("staleSummary", { n: stale.length })}</summary>
            {stale.map((rc) => (
              <ReportContentDetail
                key={rc.id}
                rc={rc}
                onChanged={refresh}
                patientId={event.patient_id}
                eventId={event.id}
                highlight={rc.id === highlightRcId}
              />
            ))}
          </details>
        </section>
      )}
    </main>
  );
}

interface DescriptionBlockProps {
  patientId: string;
  narrative: string | null;
  editing: boolean;
  draft: string;
  onDraftChange: (md: string) => void;
  onStartEdit: () => void;
  onCancel: () => void;
  onSave: () => void | Promise<void>;
  busy: boolean;
  linkErrors: EvidenceLinkViolation[];
  saveError: string | null;
  hasNarrative: boolean;
  t: (key: string, params?: Record<string, string | number>) => string;
}

function DescriptionBlock({
  patientId,
  narrative,
  editing,
  draft,
  onDraftChange,
  onStartEdit,
  onCancel,
  onSave,
  busy,
  linkErrors,
  saveError,
  hasNarrative,
  t,
}: DescriptionBlockProps) {
  if (editing) {
    return (
      <div style={{ marginTop: "0.6rem" }}>
        {saveError && (
          <p
            role="alert"
            style={{
              color: "#c00",
              background: "rgba(204,0,0,0.06)",
              border: "1px solid rgba(204,0,0,0.2)",
              padding: "0.5rem 0.75rem",
              borderRadius: 4,
              marginBottom: "0.5rem",
              fontSize: "0.88rem",
            }}
          >
            {saveError}
          </p>
        )}
        <EvidenceEditor
          value={draft}
          onChange={onDraftChange}
          onSave={onSave}
          onCancel={onCancel}
          busy={busy}
          saveLabel={t("saveDescription")}
          cancelLabel={t("cancelEdit")}
          saveBusyLabel={t("savingDescription")}
          errors={linkErrors}
          patientId={patientId}
        />
      </div>
    );
  }

  return (
    <div style={{ marginTop: "0.6rem" }}>
      {hasNarrative ? (
        <div style={{ lineHeight: 1.55 }}>
          <EvidenceContent patientId={patientId} body={narrative ?? ""} />
        </div>
      ) : (
        <p
          style={{
            color: "var(--bv-muted, #666)",
            fontStyle: "italic",
            margin: "0 0 0.4rem",
            fontSize: "0.92rem",
          }}
        >
          {t("descriptionEmpty")}
        </p>
      )}
      <button
        type="button"
        onClick={onStartEdit}
        style={{
          background: "transparent",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          color: "var(--bv-fg, #0f172a)",
          borderRadius: 4,
          padding: "0.25rem 0.6rem",
          cursor: "pointer",
          fontSize: "0.8rem",
          marginTop: "0.25rem",
        }}
      >
        {hasNarrative ? t("editDescription") : t("addDescription")}
      </button>
    </div>
  );
}

function LastEditedChip({
  prov,
  t,
}: {
  prov: ProvenanceEvent;
  t: (key: string, params?: Record<string, string | number>) => string;
}) {
  // Format the timestamp using the browser locale; falls back to the
  // raw ISO string if the input is malformed for any reason.
  let when = prov.recorded_at;
  try {
    const d = new Date(prov.recorded_at);
    if (!Number.isNaN(d.getTime())) {
      when = d.toLocaleString();
    }
  } catch {
    // keep raw
  }
  const key =
    prov.agent_kind === "agent"
      ? "lastEditedAgent"
      : prov.agent_kind === "system"
        ? "lastEditedSystem"
        : "lastEditedHuman";
  return (
    <p
      style={{
        marginTop: "0.4rem",
        fontSize: "0.78rem",
        color: "var(--bv-muted, #666)",
        fontStyle: prov.agent_kind === "agent" ? "italic" : "normal",
      }}
    >
      {t(key, { when })}
    </p>
  );
}
