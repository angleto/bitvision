"use client";

// Care-phase editor overlay.
//
// Edit mode for the timeline:
//   - drag a dot from one phase chip onto another → assign endpoint
//   - drag a chip onto another chip's slot → reorder endpoint
//   - inline edit of name / colour / narrative on the chip
//   - "Cronologia modifiche" side panel listing revisions with restore
//
// IMPORTANT — IMPLEMENTATION NOTE
// -------------------------------
// The spec calls for ``@dnd-kit/core`` + ``@dnd-kit/sortable``. Those
// packages are not yet present in ``package.json``; adding the import
// without ``pnpm install`` would break ``tsc --noEmit`` and the Next
// build for everyone. To keep the feature shippable today the editor
// uses native HTML5 drag-and-drop (which the platform handles natively
// and which the tests can drive via ``DataTransfer`` events). The
// public API (props, behaviours, endpoints invoked) matches the
// spec; swapping to dnd-kit is a localised refactor of this file.

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import EvidenceEditor from "@/components/EvidenceEditor";
import {
  type CarePhase,
  type CarePhaseRevision,
  type CarePhaseUpdateIn,
  type CareTimeline,
  type ReorderItem,
  carePhasesApi,
} from "@/lib/api_records";

interface Props {
  patientId: string;
  timeline: CareTimeline;
  onClose: () => void;
  onChanged: () => void;
}

type DragPayload =
  | { kind: "event"; eventId: string; sourcePhaseId: string }
  | { kind: "phase"; phaseId: string };

const DRAG_MIME = "application/x-bvphoenix-care-phase";

export default function CarePhaseEditor({ patientId, timeline, onClose, onChanged }: Props) {
  const t = useTranslations("carePhaseEditor");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revisionsFor, setRevisionsFor] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<CarePhaseRevision[]>([]);
  const [editingPhase, setEditingPhase] = useState<CarePhase | null>(null);

  // Native <dialog> handle. ``showModal()`` puts the element in the
  // browser's top-layer (above every fixed/sticky thing on the page,
  // including the site-header) without z-index gymnastics, traps
  // focus, and hooks Escape automatically — emitting a "close" event
  // we forward to ``onClose``. The previous ``div role=dialog`` lost
  // all of that and required hand-rolled keyboard / z-index handling.
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dlg = dialogRef.current;
    if (!dlg) return;
    if (!dlg.open) dlg.showModal();
    const onCancel = (e: Event) => {
      // Native Escape fires "cancel" then "close". Block default close
      // when a nested editor (e.g. EvidenceEditor inline edit) is
      // open — same guard the previous keydown listener applied.
      if (editingPhase) {
        e.preventDefault();
        return;
      }
    };
    const onClose_ = () => onClose();
    dlg.addEventListener("cancel", onCancel);
    dlg.addEventListener("close", onClose_);
    return () => {
      dlg.removeEventListener("cancel", onCancel);
      dlg.removeEventListener("close", onClose_);
      // The dialog element is being unmounted; closing it explicitly
      // avoids a "removed-from-DOM-while-open" warning in some
      // browsers and ensures the top-layer is released.
      if (dlg.open) dlg.close();
    };
    // ``editingPhase`` participates so the cancel handler captures
    // the latest value; ``onClose`` is stable from the parent.
  }, [onClose, editingPhase]);

  // ----- drop handlers -----------------------------------------------
  async function handleDrop(payload: DragPayload, targetPhaseId: string) {
    setBusy(true);
    setError(null);
    try {
      if (payload.kind === "event") {
        if (payload.sourcePhaseId === targetPhaseId) return;
        await carePhasesApi.assignEvent(patientId, targetPhaseId, payload.eventId, {});
        onChanged();
      } else {
        // Reorder: move the dragged phase to the position currently
        // held by the target phase in the displayed (reversed: most
        // recent first) order, then map back to ascending ordinals
        // expected by the backend (lower ordinal = older phase).
        const display = [...timeline.phases].sort((a, b) => b.ordinal - a.ordinal);
        const fromIdx = display.findIndex((p) => p.id === payload.phaseId);
        const toIdx = display.findIndex((p) => p.id === targetPhaseId);
        if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return;
        const [moved] = display.splice(fromIdx, 1);
        display.splice(toIdx, 0, moved);
        const ascending = [...display].reverse();
        const ordinals: ReorderItem[] = ascending.map((p, i) => ({
          phase_id: p.id,
          ordinal: i,
        }));
        await carePhasesApi.reorder(patientId, { ordinals });
        onChanged();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "operation failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleUnassign(phaseId: string, eventId: string) {
    setBusy(true);
    setError(null);
    try {
      await carePhasesApi.unassignEvent(patientId, phaseId, eventId);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "unassign failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleSavePhase(p: CarePhase, patch: CarePhaseUpdateIn) {
    setBusy(true);
    setError(null);
    try {
      await carePhasesApi.update(patientId, p.id, p.etag, patch);
      setEditingPhase(null);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "update failed");
    } finally {
      setBusy(false);
    }
  }

  // ----- revisions panel --------------------------------------------
  useEffect(() => {
    if (!revisionsFor) {
      setRevisions([]);
      return;
    }
    let cancelled = false;
    carePhasesApi
      .revisions(patientId, revisionsFor)
      .then((r) => {
        if (!cancelled) setRevisions(r);
      })
      .catch(() => !cancelled && setRevisions([]));
    return () => {
      cancelled = true;
    };
  }, [patientId, revisionsFor]);

  async function handleRestore(phaseId: string, revisionNo: number) {
    setBusy(true);
    setError(null);
    try {
      await carePhasesApi.restoreRevision(patientId, phaseId, revisionNo);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "restore failed");
    } finally {
      setBusy(false);
    }
  }

  // ----- render ------------------------------------------------------
  return (
    <dialog
      ref={dialogRef}
      aria-label={t("openLabel")}
      className="care-phase-editor"
      style={{
        // Native <dialog> + showModal() puts the element in the
        // browser top-layer, so we no longer fight z-index against the
        // sticky site-header. The browser also paints the ::backdrop
        // pseudo-element automatically, so the dim is not a property
        // of this style. We still layout the dialog itself flexbox-wise
        // so the inner header / grid stack vertically as before.
        padding: 0,
        margin: 0,
        border: "none",
        width: "100vw",
        maxWidth: "100vw",
        height: "100vh",
        maxHeight: "100vh",
        background: "transparent",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <header
        style={{
          padding: "0.75rem 1rem",
          background: "var(--bv-card-bg)",
          borderBottom: "1px solid var(--bv-card-border)",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
        }}
      >
        <strong>{t("title")}</strong>
        <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>{t("intro")}</span>
        <span style={{ flex: 1 }} />
        {busy && <span className="meta">{t("saving")}</span>}
        {error && <span style={{ color: "var(--bv-danger, #c00)" }}>{error}</span>}
        <button type="button" className="ghost" onClick={onClose}>
          {t("close")}
        </button>
      </header>

      <div
        style={{
          flex: 1,
          display: "grid",
          gridTemplateColumns: "1fr 320px",
          gap: 0,
          overflow: "hidden",
        }}
      >
        <section
          style={{
            overflow: "auto",
            padding: "1rem",
            background: "var(--bv-bg, #fff)",
          }}
          aria-label={t("phasesAndEventsLabel")}
        >
          <ol style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {[...timeline.phases]
              .sort((a, b) => b.ordinal - a.ordinal)
              .map((phase) => (
                <li
                  key={phase.id}
                  draggable
                  onDragStart={(e) => {
                    const p: DragPayload = { kind: "phase", phaseId: phase.id };
                    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(p));
                    e.dataTransfer.effectAllowed = "move";
                  }}
                  onDragOver={(e) => {
                    if (e.dataTransfer.types.includes(DRAG_MIME)) {
                      e.preventDefault();
                      e.dataTransfer.dropEffect = "move";
                    }
                  }}
                  onDrop={(e) => {
                    const raw = e.dataTransfer.getData(DRAG_MIME);
                    if (!raw) return;
                    e.preventDefault();
                    let payload: DragPayload;
                    try {
                      payload = JSON.parse(raw) as DragPayload;
                    } catch {
                      return;
                    }
                    void handleDrop(payload, phase.id);
                  }}
                  style={{
                    borderRadius: 8,
                    border: "1px solid var(--bv-card-border)",
                    background: `color-mix(in srgb, ${phase.color_hex} 6%, var(--bv-card-bg))`,
                    padding: "0.6rem 0.7rem",
                    marginBottom: "0.6rem",
                    cursor: "grab",
                  }}
                  data-phase-id={phase.id}
                  data-phase-slug={phase.slug}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "0.5rem",
                    }}
                  >
                    <span
                      aria-hidden
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: "50%",
                        background: phase.color_hex,
                      }}
                    />
                    <strong style={{ flex: 1 }}>{phase.name}</strong>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => setEditingPhase(phase)}
                      style={{ fontSize: "0.72rem" }}
                    >
                      {t("edit")}
                    </button>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => setRevisionsFor(phase.id)}
                      style={{ fontSize: "0.72rem" }}
                    >
                      {t("revisions")}
                    </button>
                  </div>
                  <ul style={{ listStyle: "none", padding: 0, margin: "0.4rem 0 0" }}>
                    {phase.events.map((ev) => (
                      <li
                        key={ev.id}
                        draggable
                        onDragStart={(e) => {
                          e.stopPropagation();
                          const p: DragPayload = {
                            kind: "event",
                            eventId: ev.id,
                            sourcePhaseId: phase.id,
                          };
                          e.dataTransfer.setData(DRAG_MIME, JSON.stringify(p));
                          e.dataTransfer.effectAllowed = "move";
                        }}
                        data-event-id={ev.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: "0.5rem",
                          padding: "0.2rem 0.3rem",
                          fontSize: "0.82rem",
                          cursor: "grab",
                        }}
                      >
                        <span
                          aria-hidden
                          style={{
                            width: 8,
                            height: 8,
                            borderRadius: "50%",
                            background: phase.color_hex,
                          }}
                        />
                        <span style={{ flex: 1 }}>
                          {ev.event_date ?? "—"} · {ev.title}
                        </span>
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => handleUnassign(phase.id, ev.id)}
                          style={{ fontSize: "0.7rem" }}
                          title={t("removeAssignment")}
                        >
                          ×
                        </button>
                      </li>
                    ))}
                    {phase.events.length === 0 && (
                      <li
                        style={{
                          fontSize: "0.75rem",
                          color: "var(--bv-fg-muted, #888)",
                          padding: "0.2rem 0.3rem",
                        }}
                      >
                        {t("dropEventsHere")}
                      </li>
                    )}
                  </ul>
                </li>
              ))}
          </ol>

          {timeline.unassigned_events.length > 0 && (
            <section
              aria-label={t("unassignedLabel")}
              style={{
                marginTop: "1rem",
                border: "1px dashed var(--bv-card-border)",
                borderRadius: 8,
                padding: "0.6rem",
              }}
            >
              <strong style={{ fontSize: "0.85rem" }}>{t("unassignedHeading")}</strong>
              <ul style={{ listStyle: "none", padding: 0, margin: "0.4rem 0 0" }}>
                {timeline.unassigned_events.map((ev) => (
                  <li
                    key={ev.id}
                    draggable
                    onDragStart={(e) => {
                      const p: DragPayload = {
                        kind: "event",
                        eventId: ev.id,
                        sourcePhaseId: "",
                      };
                      e.dataTransfer.setData(DRAG_MIME, JSON.stringify(p));
                      e.dataTransfer.effectAllowed = "move";
                    }}
                    data-event-id={ev.id}
                    style={{
                      padding: "0.2rem 0.3rem",
                      fontSize: "0.82rem",
                      cursor: "grab",
                    }}
                  >
                    {ev.event_date ?? "—"} · {ev.title}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </section>

        <aside
          aria-label={t("historyAriaLabel")}
          style={{
            background: "var(--bv-card-bg)",
            borderLeft: "1px solid var(--bv-card-border)",
            padding: "1rem",
            overflow: "auto",
          }}
        >
          <h3 style={{ marginTop: 0, fontSize: "1rem" }}>{t("historyTitle")}</h3>
          {!revisionsFor && (
            <p className="meta" style={{ fontSize: "0.82rem" }}>
              {t("historyEmpty")}
            </p>
          )}
          {revisionsFor && revisions.length === 0 && (
            <p className="meta" style={{ fontSize: "0.82rem" }}>
              {t("noRevisions")}
            </p>
          )}
          <ol style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {revisions.map((r) => (
              <li
                key={r.id}
                style={{
                  padding: "0.4rem 0.2rem",
                  borderBottom: "1px solid var(--bv-card-border)",
                  fontSize: "0.78rem",
                }}
              >
                <div style={{ display: "flex", gap: "0.4rem" }}>
                  <span style={{ fontWeight: 600 }}>r{r.revision_no}</span>
                  <span style={{ color: "var(--bv-fg-soft)" }}>{r.change_kind}</span>
                  {r.author_kind === "agent" && (
                    <span
                      style={{
                        fontSize: "0.65rem",
                        background: "var(--bv-accent-soft, #eef2ff)",
                        color: "var(--bv-accent, #4f46e5)",
                        padding: "0 0.3rem",
                        borderRadius: 4,
                      }}
                    >
                      AI
                    </span>
                  )}
                </div>
                {r.diff_summary && (
                  <div style={{ color: "var(--bv-fg-soft)" }}>{r.diff_summary}</div>
                )}
                <div
                  style={{
                    marginTop: "0.25rem",
                    display: "flex",
                    gap: "0.4rem",
                  }}
                >
                  <span className="meta" style={{ fontSize: "0.7rem", flex: 1 }}>
                    {new Date(r.created_at).toLocaleString()}
                  </span>
                  <button
                    type="button"
                    className="ghost"
                    style={{ fontSize: "0.7rem" }}
                    onClick={() => handleRestore(revisionsFor as string, r.revision_no)}
                  >
                    {t("restore")}
                  </button>
                </div>
              </li>
            ))}
          </ol>
        </aside>
      </div>

      {editingPhase && (
        <PhaseInlineEditor
          phase={editingPhase}
          patientId={patientId}
          onCancel={() => setEditingPhase(null)}
          onSave={(patch) => handleSavePhase(editingPhase, patch)}
        />
      )}
    </dialog>
  );
}

function PhaseInlineEditor({
  phase,
  patientId,
  onCancel,
  onSave,
}: {
  phase: CarePhase;
  patientId: string;
  onCancel: () => void;
  onSave: (patch: CarePhaseUpdateIn) => void;
}) {
  const t = useTranslations("carePhaseEditor");
  const [name, setName] = useState(phase.name);
  const [color, setColor] = useState(phase.color_hex);
  const [narrative, setNarrative] = useState(phase.narrative_md ?? "");

  // Native nested <dialog>. Browsers stack ``showModal()`` calls in
  // a top-layer LIFO, so this dialog appears above the parent
  // CarePhaseEditor without z-index coordination.
  const innerRef = useRef<HTMLDialogElement | null>(null);
  useEffect(() => {
    const dlg = innerRef.current;
    if (!dlg) return;
    if (!dlg.open) dlg.showModal();
    const onClose_ = () => onCancel();
    dlg.addEventListener("close", onClose_);
    return () => {
      dlg.removeEventListener("close", onClose_);
      if (dlg.open) dlg.close();
    };
  }, [onCancel]);

  return (
    <dialog
      ref={innerRef}
      aria-label={t("editPhaseAriaLabel")}
      className="care-phase-inner-editor"
      style={{
        padding: 0,
        margin: 0,
        border: "none",
        background: "transparent",
      }}
    >
      <form
        className="card"
        onSubmit={(e) => {
          e.preventDefault();
          onSave({
            name,
            color_hex: color,
            narrative_md: narrative || null,
          });
        }}
        style={{
          background: "var(--bv-card-bg)",
          padding: "1rem",
          borderRadius: 8,
          minWidth: 360,
          maxWidth: 520,
        }}
      >
        <h3 style={{ marginTop: 0 }}>{t("editPhaseTitle")}</h3>
        <label style={{ display: "block", marginBottom: "0.5rem" }}>
          <span className="meta">{t("fieldName")}</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ width: "100%" }}
            required
          />
        </label>
        <label style={{ display: "block", marginBottom: "0.5rem" }}>
          <span className="meta">{t("fieldColor")}</span>
          <input
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value.toUpperCase())}
          />
        </label>
        <div style={{ display: "block", marginBottom: "0.5rem" }}>
          <span className="meta">{t("fieldNarrative")}</span>
          <div style={{ marginTop: "0.25rem" }}>
            <EvidenceEditor
              value={narrative}
              onChange={setNarrative}
              embedded
              patientId={patientId}
            />
          </div>
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
          }}
        >
          <button type="button" className="ghost" onClick={onCancel}>
            {t("cancel")}
          </button>
          <button type="submit">{t("save")}</button>
        </div>
      </form>
    </dialog>
  );
}
