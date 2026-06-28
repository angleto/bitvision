"use client";

// Side-panel that shows the full detail of one ClinicalEvent + action
// buttons enabled per the FSM transition rules (Confirm only when
// planned, Reschedule when planned/confirmed/missed, ...). Opening
// any action mounts the EventActionDialog with the corresponding
// preset.

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import AttachmentsManager from "@/components/AttachmentsManager";
import EditEventDialog from "@/components/EditEventDialog";
import EventActionDialog, { type ActionKind } from "@/components/EventActionDialog";
import EvidenceContent from "@/components/EvidenceContent";
import { ApiError, request } from "@/lib/api";
import {
  type CarePhaseDetail,
  type ClinicalEvent,
  type EventStatus,
  carePhasesApi,
} from "@/lib/api_records";
import { eventStatusStyle } from "@/lib/event_status_style";

interface Props {
  eventId: string;
  isOwner: boolean;
  onClose: () => void;
  onChanged: () => void;
}

const ALLOWED_FROM: Record<EventStatus, ActionKind[]> = {
  planned: ["confirm", "reschedule", "complete", "cancel", "markMissed"],
  confirmed: ["reschedule", "complete", "cancel", "markMissed"],
  completed: [],
  cancelled: [],
  missed: ["reschedule", "complete"],
  rescheduled: [],
};

export default function EventDrawer({ eventId, isOwner, onClose, onChanged }: Props) {
  const t = useTranslations("eventActions");
  const tStatus = useTranslations("eventStatus");
  const locale = useLocale();
  const [event, setEvent] = useState<ClinicalEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionOpen, setActionOpen] = useState<ActionKind | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  // Care-phase assignment lives here so the user can (re)assign an event
  // to a phase from the UI on any device — the only other path is the
  // CareTimeline edit-mode drag-and-drop, which is invisible/unusable on
  // touch. The individual-event endpoint omits phase_id, so we derive the
  // current phase from the timeline's phase membership.
  const [phases, setPhases] = useState<CarePhaseDetail[] | null>(null);
  const [currentPhaseId, setCurrentPhaseId] = useState<string | null>(null);
  const [phaseBusy, setPhaseBusy] = useState(false);
  const [phaseError, setPhaseError] = useState<string | null>(null);
  // Drag-out protection on the slide-over backdrop (selecting text
  // and releasing outside the drawer would otherwise dismiss it).
  const mouseDownOnBackdrop = useRef(false);

  useEffect(() => {
    let cancelled = false;
    request<ClinicalEvent>(`/api/clinical-events/${eventId}`)
      .then((ev) => {
        if (!cancelled) setEvent(ev);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [eventId]);

  // Load care phases + this event's current phase once the event (hence
  // patient_id) is known.
  useEffect(() => {
    const patientId = event?.patient_id;
    if (!patientId) return;
    let cancelled = false;
    carePhasesApi
      .timeline(patientId, { lang: locale })
      .then((tl) => {
        if (cancelled) return;
        setPhases(tl.phases);
        const cur = tl.phases.find((p) => p.events.some((e) => e.id === eventId));
        setCurrentPhaseId(cur?.id ?? null);
      })
      .catch(() => {
        if (!cancelled) setPhases([]);
      });
    return () => {
      cancelled = true;
    };
  }, [event?.patient_id, eventId, locale]);

  async function changePhase(nextPhaseId: string): Promise<void> {
    const patientId = event?.patient_id;
    if (!patientId || phaseBusy || nextPhaseId === (currentPhaseId ?? "")) return;
    setPhaseBusy(true);
    setPhaseError(null);
    try {
      if (nextPhaseId === "") {
        if (currentPhaseId) await carePhasesApi.unassignEvent(patientId, currentPhaseId, eventId);
        setCurrentPhaseId(null);
      } else {
        // PUT upserts the assignment, so reassigning from another phase
        // needs no explicit unassign first.
        await carePhasesApi.assignEvent(patientId, nextPhaseId, eventId);
        setCurrentPhaseId(nextPhaseId);
      }
      onChanged();
    } catch (e) {
      setPhaseError(
        e instanceof ApiError ? `${e.status}: ${String(e.detail)}` : t("phaseAssignError"),
      );
    } finally {
      setPhaseBusy(false);
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const currentStatus: EventStatus = event?.event_status ?? "completed";
  const allowed: ActionKind[] = event ? ALLOWED_FROM[currentStatus] : [];
  const sStyle = event ? eventStatusStyle(currentStatus) : null;

  return (
    // biome-ignore lint/a11y/useSemanticElements: native <dialog> doesn't give us the right-edge slide-over with backdrop dismissal; the modal pattern with role="dialog" + aria-modal is the established a11y workaround.
    // biome-ignore lint/a11y/useKeyWithClickEvents: backdrop click handler is a UX nicety, not the primary close affordance; Esc key is wired in the useEffect above and the explicit ✕ button is keyboard-focusable.
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Event detail"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.35)",
        display: "flex",
        justifyContent: "flex-end",
        zIndex: 1000,
      }}
      onMouseDown={(e) => {
        mouseDownOnBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        const wasBackdropDown = mouseDownOnBackdrop.current;
        mouseDownOnBackdrop.current = false;
        if (wasBackdropDown && e.target === e.currentTarget) onClose();
      }}
    >
      <aside
        style={{
          width: "min(520px, 100%)",
          height: "100%",
          background: "var(--bv-card-bg, #fff)",
          padding: "1rem",
          overflow: "auto",
          boxShadow: "-2px 0 12px rgba(0,0,0,0.18)",
        }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.6rem",
          }}
        >
          <h2 style={{ margin: 0, fontSize: "1.1rem" }}>{event?.title ?? "…"}</h2>
          <button
            type="button"
            className="ghost"
            onClick={onClose}
            aria-label="Close"
            style={{ fontSize: "1rem" }}
          >
            ✕
          </button>
        </header>
        {error && <p style={{ color: "var(--bv-danger, #c00)" }}>{error}</p>}
        {event && sStyle && (
          <>
            <div
              style={{
                display: "inline-block",
                padding: "0.2rem 0.5rem",
                borderRadius: 999,
                fontSize: "0.75rem",
                fontWeight: 600,
                background: sStyle.badgeBg,
                color: sStyle.badgeFg,
                marginBottom: "0.6rem",
              }}
            >
              {sStyle.glyph} {tStatus(currentStatus)}
            </div>
            <dl style={dlStyle}>
              <Row label="Kind">{event.kind}</Row>
              {event.event_date && <Row label="Date">{event.event_date}</Row>}
              {event.planned_start_at && (
                <Row label="Planned">{new Date(event.planned_start_at).toLocaleString()}</Row>
              )}
              {event.actual_start_at && (
                <Row label="Actual">{new Date(event.actual_start_at).toLocaleString()}</Row>
              )}
              {event.timezone && <Row label="TZ">{event.timezone}</Row>}
              {event.location_struct && (
                <Row label="Location">
                  {[
                    event.location_struct.facility,
                    event.location_struct.room,
                    event.location_struct.city,
                  ]
                    .filter(Boolean)
                    .join(", ")}
                </Row>
              )}
              {event.status_change_reason && <Row label="Reason">{event.status_change_reason}</Row>}
              {event.parent_event_id && <Row label="Parent">{event.parent_event_id}</Row>}
            </dl>
            <section style={{ marginTop: "0.6rem" }}>
              <h4 style={sectionHeading}>{t("phaseLabel")}</h4>
              {phases === null ? (
                <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-fg-soft)" }}>…</p>
              ) : isOwner ? (
                <>
                  <select
                    value={currentPhaseId ?? ""}
                    onChange={(e) => void changePhase(e.target.value)}
                    disabled={phaseBusy}
                    aria-label={t("phaseLabel")}
                    style={phaseSelectStyle}
                  >
                    <option value="">{t("phaseUnassigned")}</option>
                    {phases.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                  {phaseBusy && (
                    <span style={{ marginLeft: 8, fontSize: "0.8rem", color: "var(--bv-fg-soft)" }}>
                      {t("phaseSaving")}
                    </span>
                  )}
                  {phaseError && (
                    <p
                      style={{
                        color: "var(--bv-danger)",
                        fontSize: "0.8rem",
                        margin: "0.3rem 0 0",
                      }}
                    >
                      {phaseError}
                    </p>
                  )}
                </>
              ) : (
                <p style={{ margin: 0, fontSize: "0.88rem" }}>
                  {phases.find((p) => p.id === currentPhaseId)?.name ?? t("phaseUnassigned")}
                </p>
              )}
            </section>
            {event.narrative && (
              <section style={{ marginTop: "0.6rem" }}>
                <h4 style={sectionHeading}>{t("noteLabel")}</h4>
                <EvidenceContent
                  patientId={event.patient_id}
                  body={event.narrative}
                  ctx={`event:${event.id}`}
                />
              </section>
            )}
            {event.meeting_url && (
              <a
                href={event.meeting_url}
                target="_blank"
                rel="noreferrer noopener"
                style={{
                  display: "inline-block",
                  marginTop: "0.5rem",
                  padding: "0.4rem 0.8rem",
                  background: "var(--bv-accent, #4f46e5)",
                  color: "#fff",
                  borderRadius: 6,
                  fontSize: "0.85rem",
                  fontWeight: 600,
                  textDecoration: "none",
                }}
              >
                📹 {t("joinMeeting")}
              </a>
            )}
            {(event.links?.length ?? 0) > 0 && (
              <section style={{ marginTop: "0.8rem" }}>
                <h4 style={sectionHeading}>{t("linksLabel")}</h4>
                <ul style={listReset}>
                  {(event.links ?? []).map((l, i) => (
                    <li key={`${l.url}-${i}`}>
                      <a href={l.url} target="_blank" rel="noreferrer noopener" style={linkRow}>
                        🔗 {l.label || l.url}
                      </a>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section style={{ marginTop: "0.8rem" }}>
              <h4 style={sectionHeading}>{t("attachmentsLabel")}</h4>
              <AttachmentsManager eventId={event.id} patientId={event.patient_id} />
            </section>
            {isOwner && (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 6,
                  marginTop: "1rem",
                  paddingTop: "0.8rem",
                  borderTop: "1px solid var(--bv-card-border, #e5e7eb)",
                }}
              >
                {/* Edit is always available regardless of status; the
                 * server's PATCH only touches metadata, never status. */}
                <button
                  type="button"
                  onClick={() => setEditOpen(true)}
                  style={{ fontSize: "0.82rem", padding: "0.3rem 0.7rem" }}
                >
                  {t("edit")}
                </button>
                {allowed.map((a) => (
                  <button
                    key={a}
                    type="button"
                    onClick={() => setActionOpen(a)}
                    style={{ fontSize: "0.82rem", padding: "0.3rem 0.7rem" }}
                  >
                    {t(a)}
                  </button>
                ))}
              </div>
            )}
            {actionOpen && (
              <EventActionDialog
                event={event}
                action={actionOpen}
                onClose={() => setActionOpen(null)}
                onSuccess={() => {
                  setActionOpen(null);
                  onChanged();
                }}
              />
            )}
            {editOpen && (
              <EditEventDialog
                event={event}
                onClose={() => setEditOpen(false)}
                onSaved={(updated) => {
                  setEditOpen(false);
                  setEvent(updated);
                  onChanged();
                }}
              />
            )}
          </>
        )}
      </aside>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt
        style={{
          fontSize: "0.7rem",
          textTransform: "uppercase",
          letterSpacing: "0.04em",
          color: "var(--bv-fg-soft)",
        }}
      >
        {label}
      </dt>
      <dd style={{ margin: "0 0 0.5rem 0", fontSize: "0.88rem" }}>{children}</dd>
    </>
  );
}

const dlStyle = {
  display: "grid",
  gridTemplateColumns: "max-content 1fr",
  columnGap: "1rem",
  rowGap: 0,
  marginBottom: 0,
} as const;

const sectionHeading = {
  fontSize: "0.72rem",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  color: "var(--bv-fg-soft)",
  margin: "0 0 0.3rem 0",
} as const;

const phaseSelectStyle = {
  fontSize: "0.88rem",
  padding: "0.35rem 0.5rem",
  border: "1px solid var(--bv-card-border, #d0d5dd)",
  borderRadius: 6,
  background: "var(--bv-card-bg, transparent)",
  color: "var(--bv-fg)",
  maxWidth: "100%",
} as const;

const listReset = {
  listStyle: "none",
  padding: 0,
  margin: 0,
  display: "flex",
  flexDirection: "column",
  gap: 4,
} as const;

const linkRow = {
  fontSize: "0.85rem",
  textDecoration: "none",
  color: "var(--bv-fg)",
  display: "inline-block",
  padding: "2px 6px",
  borderRadius: 4,
  background: "var(--bv-card-bg-soft, #f9fafb)",
  border: "1px solid var(--bv-card-border, #e5e7eb)",
} as const;
