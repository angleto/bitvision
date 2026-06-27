"use client";

// Sticky compact preview of ``patient.notes`` (the static "clinical
// notes" sticky reference about the patient — distinct from the
// scattered Evidence notes attached to studies/documents and from the
// Synthesis draft consultation, both of which live inside the
// "Sintesi & Evidenze" tab of the Health Record).
//
// Why sticky: the previous layout buried ``patient.notes`` inside the
// patient header, between demographics and contacts. Clinicians lost
// it on every page mount and had to scroll to find it. The sticky
// keeps it under the eye while the user navigates the Health Record
// folders below.
//
// Why a fade-out: the field is free-form markdown and can grow long.
// Showing it all eats the viewport; truncating without a fade hides
// the fact that more content exists. The mask-image fade is a strong
// "more here, click expand" affordance that matches Material/Linear
// patterns.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import EvidenceContent from "@/components/EvidenceContent";
import EvidenceEditor from "@/components/EvidenceEditor";
import { ApiError, type Patient, patientsApi } from "@/lib/api";

interface Props {
  patient: Patient;
  isOwner: boolean;
  /** Called after a successful save so the parent can refresh its
   *  ``patient`` snapshot (etag, updated_at). */
  onUpdated: () => void | Promise<void>;
}

// Collapsed preview height. Roughly 5 lines of body text at the
// project's default line-height. The exact px is intentional: a
// rem-relative cap would jitter when the user changes the root font
// size mid-session, and the fade-out mask reads more cleanly with a
// known box height.
const COLLAPSED_MAX_PX = 140;

// localStorage key for the per-patient expanded preference. Scoping
// by patient_id keeps an "expanded for the chronic case I'm working
// up" state from leaking into a quick consult on a different
// patient. Storage is best-effort: a quota error or SSR pass falls
// back to ``false`` (collapsed).
const EXPANDED_STORAGE_KEY = "bvp.clinicalNotes.expanded.v1";

function readPersistedExpanded(patientId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = window.localStorage.getItem(EXPANDED_STORAGE_KEY);
    if (!raw) return false;
    const parsed = JSON.parse(raw) as Record<string, boolean>;
    return !!parsed[patientId];
  } catch {
    return false;
  }
}

/**
 * Italian-friendly relative-time renderer for the "edited by" cue.
 * Hand-rolled (no Intl.RelativeTimeFormat) so the strings stay in
 * the central ``messages/*.json`` catalogue rather than the runtime
 * locale data — keeps the IT/EN switch consistent with everything
 * else on the page. Falls back to absolute date on ranges past 30
 * days.
 */
function formatRelativeTime(
  date: Date,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const ms = Date.now() - date.getTime();
  const minutes = Math.round(ms / 60_000);
  if (minutes < 1) return t("relJustNow");
  if (minutes < 60) return t("relMinutes", { n: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return t("relHours", { n: hours });
  const days = Math.round(hours / 24);
  if (days < 30) return t("relDays", { n: days });
  return date.toLocaleDateString();
}

function writePersistedExpanded(patientId: string, value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    const raw = window.localStorage.getItem(EXPANDED_STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Record<string, boolean>) : {};
    if (value) parsed[patientId] = true;
    else delete parsed[patientId];
    window.localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify(parsed));
  } catch {
    // Quota exceeded / private mode: no-op. The component still
    // works for the current session, the preference just doesn't
    // survive a reload.
  }
}

export default function ClinicalNotesSticky({ patient, isOwner, onUpdated }: Props) {
  const t = useTranslations("clinicalNotesSticky");
  const sectionRef = useRef<HTMLElement | null>(null);
  // Initialiser runs once on mount; thereafter the persistence
  // round-trip is driven by setExpanded wrapper below so a re-mount
  // (tab switch) restores the user's last choice on this patient.
  const [expanded, setExpandedState] = useState<boolean>(() => readPersistedExpanded(patient.id));
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(patient.notes ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const setExpanded = useCallback(
    (next: boolean | ((prev: boolean) => boolean)) => {
      setExpandedState((prev) => {
        const resolved = typeof next === "function" ? next(prev) : next;
        writePersistedExpanded(patient.id, resolved);
        return resolved;
      });
    },
    [patient.id],
  );

  const hasContent = !!(patient.notes && patient.notes.trim().length > 0);

  function startEdit() {
    setDraft(patient.notes ?? "");
    setErr(null);
    setEditing(true);
    setExpanded(true);
  }

  function cancelEdit() {
    setEditing(false);
    setErr(null);
    setDraft(patient.notes ?? "");
  }

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      // Empty draft → null on the wire so the column persists ``NULL``
      // rather than an empty string (matches every other nullable
      // patient field; saves a downstream ``? "" : null`` everywhere
      // we render).
      const body = draft.trim();
      await patientsApi.update(patient.id, { notes: body || null }, patient.etag);
      setEditing(false);
      await onUpdated();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveError"));
    } finally {
      setBusy(false);
    }
  }

  // Keyboard shortcuts:
  //   - Esc while editing → cancel (drops the draft, like other
  //     in-place editors in the app).
  //   - Esc while expanded (not editing) → collapse, so the operator
  //     can dismiss without aiming the chevron toggle.
  //   - Cmd/Ctrl+Enter while editing → save without leaving the
  //     keyboard. TipTap's StarterKit ignores this combo for new
  //     paragraphs (it uses Shift+Enter for soft breaks), so we don't
  //     fight the editor's own bindings.
  // The handler defers to ``editing`` first so an in-flight typist
  // never accidentally collapses the panel mid-sentence.
  useEffect(() => {
    if (!expanded && !editing) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        if (editing) {
          e.preventDefault();
          cancelEdit();
        } else if (expanded) {
          e.preventDefault();
          setExpanded(false);
        }
        return;
      }
      if (editing && (e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        if (!busy) void save();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // ``save`` and ``cancelEdit`` are stable closures over ``draft``
    // and ``patient``; we intentionally don't include them in deps to
    // avoid re-attaching the listener on every keystroke. The handler
    // reads the latest ``draft`` because save() captures it.
    // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above.
  }, [expanded, editing, busy, setExpanded]);

  // Click-outside collapses the panel when the user clicks a
  // non-interactive area of the page (read mode only — we never
  // collapse mid-edit because that would lose the draft).
  //
  // Why we skip interactive targets: the user clicking a button
  // (e.g. "Invia studio", "Modifica") is already starting another
  // task; collapsing the notes underneath them surprises the user
  // and, worse, may cause a layout shift just before the modal
  // opens. The pattern matches Apple's expanded-card UX (clicks on
  // controls do their thing, clicks on background dismiss).
  //
  // Pointerdown is preferred over click so a drag that started
  // outside doesn't close the panel just because the mouseup
  // landed on the toggle button. Open modal dialogs always get a
  // free pass.
  useEffect(() => {
    if (!expanded || editing) return;
    function isInteractiveTarget(el: HTMLElement): boolean {
      // Closest covers the case where the click landed on an icon /
      // span inside a real interactive ancestor (a button with an
      // emoji label, a link with a thumbnail child).
      return !!el.closest(
        'button, a, input, textarea, select, [role="button"], [role="link"], [role="menuitem"], [role="tab"], [contenteditable="true"]',
      );
    }
    function onPointerDown(e: PointerEvent) {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      if (sectionRef.current?.contains(target)) return;
      if (target.closest('[role="dialog"]')) return;
      if (isInteractiveTarget(target)) return;
      setExpanded(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [expanded, editing, setExpanded]);

  // Anchor-link support: ``/patients/<id>#notes`` lands the operator
  // (or anyone receiving a deep-link in a chat thread) directly on
  // the notes panel — auto-expanded and scrolled into view. The hash
  // is read once on mount; thereafter the user's own toggle wins.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.location.hash !== "#notes") return;
    setExpanded(true);
    // The browser's default hash scroll lands at the section top
    // edge; ``smooth`` makes the jump feel intentional rather than
    // a layout glitch. Defer one frame so the expand state has
    // applied before we measure the new scroll target.
    const id = window.requestAnimationFrame(() => {
      sectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => window.cancelAnimationFrame(id);
    // Empty deps: hash resolution is a single-shot side effect.
    // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above.
  }, []);

  // Dirty-state warning: if the user is editing AND the draft
  // differs from the persisted body, the browser must not let them
  // lose work to an accidental tab close / refresh / typed URL
  // navigation. The native ``beforeunload`` prompt is the only API
  // that survives across all those vectors; client-side route
  // changes inside the SPA stay covered by the explicit
  // Esc-cancels-edit affordance below.
  useEffect(() => {
    if (!editing) return;
    const isDirty = draft !== (patient.notes ?? "");
    if (!isDirty) return;
    function onBeforeUnload(e: BeforeUnloadEvent) {
      e.preventDefault();
      // Modern browsers ignore the custom string and show a generic
      // "leave site?" prompt; keeping it for legacy compliance.
      e.returnValue = "";
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [editing, draft, patient.notes]);

  // Render-side helpers.
  const isDirty = editing && draft !== (patient.notes ?? "");
  const editorName = patient.notes_updated_by_display_name?.trim() || null;
  const editedAt = patient.notes_updated_at ? new Date(patient.notes_updated_at) : null;

  return (
    <section
      ref={sectionRef}
      id="notes"
      data-clinical-notes-sticky=""
      aria-label={t("ariaRegion")}
      style={{
        // Sticky only when collapsed: the compact preview anchors at
        // the top of the viewport so the operator never loses sight
        // of patient.notes while navigating folders. When expanded,
        // we drop the sticky so the long body scrolls away naturally
        // and the folder grid / Health Record tabs below stay
        // accessible — otherwise the expanded card would cover the
        // tab strip and stay there until collapsed.
        position: expanded || editing ? "relative" : "sticky",
        top: expanded || editing ? "auto" : "calc(var(--header-h, 56px) + 0.5rem)",
        // z-index + isolation are only needed while the memo is the
        // collapsed STICKY overlay (it must float above the folder grid /
        // tile peeks below). When expanded it returns to normal flow, so we
        // drop both: a stacking context here would let the expanded card
        // PAINT OVER the tabs/content below instead of pushing them down
        // (the mobile-only overlap report). Modal dialogs sit at 1000.
        zIndex: expanded || editing ? "auto" : 100,
        isolation: expanded || editing ? "auto" : "isolate",
        marginTop: "1rem",
        marginBottom: "1rem",
        border: "1px solid var(--bv-card-border)",
        borderRadius: "var(--bv-r-md)",
        background: "var(--bv-card-bg)",
        boxShadow: "var(--bv-shadow-1, 0 1px 2px rgba(0,0,0,0.04))",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          padding: "0.5rem 0.85rem",
          borderBottom: hasContent || editing ? "1px solid var(--bv-card-border)" : "none",
        }}
      >
        <span
          aria-hidden
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: "var(--bv-accent, #e96b1f)",
            flexShrink: 0,
          }}
        />
        <strong style={{ fontSize: "0.92rem" }}>{t("label")}</strong>
        {/* Provenance cue. Only renders when the backend set both
            sides of the pair, so legacy rows stay visually clean.
            ``aria-live=off`` (default) avoids announcing every
            timestamp tick on screen readers — the value is
            informational, not status. */}
        {editedAt && (
          <span
            data-clinical-notes-meta=""
            style={{
              fontSize: "0.74rem",
              color: "var(--bv-fg-soft, #475569)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              minWidth: 0,
            }}
            title={editedAt.toLocaleString()}
          >
            {t("editedBy", {
              name: editorName ?? t("editedByUnknown"),
              when: formatRelativeTime(editedAt, t),
            })}
          </span>
        )}
        {/* Dirty-state pill. Surfaces "modifiche non salvate" so the
            user has a visible cue before they hit Esc / leave the
            page — the beforeunload prompt is a fallback for hard
            navigations. */}
        {isDirty && (
          // biome-ignore lint/a11y/useSemanticElements: <output> is for form-derived computations; this is an informational status badge — role="status" + aria-live keeps assistive tech in sync without semantic mismatch.
          <span
            role="status"
            aria-live="polite"
            style={{
              fontSize: "0.7rem",
              fontWeight: 600,
              padding: "1px 6px",
              borderRadius: 999,
              background: "var(--bv-warning-soft, #fef3c7)",
              color: "var(--bv-warning, #b45309)",
              whiteSpace: "nowrap",
            }}
          >
            {t("dirty")}
          </span>
        )}
        <div style={{ marginLeft: "auto", display: "inline-flex", gap: "0.35rem" }}>
          {hasContent && !editing && (
            <button
              type="button"
              className="ghost"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls="bv-clinical-notes-body"
              title={expanded ? t("collapseTitle") : t("expandTitle")}
              style={{ fontSize: "0.78rem", padding: "0.2rem 0.55rem" }}
            >
              {expanded ? "▾" : "▸"} {expanded ? t("collapse") : t("expand")}
            </button>
          )}
          {isOwner && !editing && (
            <button
              type="button"
              className="ghost"
              onClick={startEdit}
              title={t("editTitle")}
              style={{ fontSize: "0.78rem", padding: "0.2rem 0.55rem" }}
            >
              {t("edit")}
            </button>
          )}
        </div>
      </header>

      {editing ? (
        <div style={{ padding: "0.6rem 0.85rem" }}>
          {err && (
            <p className="error" style={{ margin: "0 0 0.4rem" }}>
              {err}
            </p>
          )}
          <EvidenceEditor
            value={draft}
            onChange={setDraft}
            onSave={save}
            onCancel={cancelEdit}
            patientId={patient.id}
            busy={busy}
            saveLabel={t("save")}
            saveBusyLabel={t("saveBusy")}
            cancelLabel={t("cancel")}
            saveTitle={t("saveTitle")}
            cancelTitle={t("cancelTitle")}
          />
        </div>
      ) : hasContent ? (
        <>
          <div
            id="bv-clinical-notes-body"
            data-clinical-notes-sticky-body=""
            style={{
              position: "relative",
              padding: "0.6rem 0.85rem",
              maxHeight: expanded ? "none" : COLLAPSED_MAX_PX,
              overflow: expanded ? "visible" : "hidden",
              // Soft fade-out on the bottom 28% of the collapsed box so
              // the cut isn't a hard line. ``mask-image`` is widely
              // supported (incl. Safari 16+) and degrades to "no fade,
              // hard cut" on older browsers without a layout shift.
              maskImage: expanded
                ? "none"
                : "linear-gradient(to bottom, black 60%, transparent 100%)",
              WebkitMaskImage: expanded
                ? "none"
                : "linear-gradient(to bottom, black 60%, transparent 100%)",
              // Only animate the COLLAPSE. Transitioning max-height to
              // ``none`` is unreliable on WebKit (iOS/mobile Safari): the
              // value stays stuck at the collapsed 140px while overflow
              // flips to visible, so the body spills out and (with the old
              // z-index) painted over the elements below — the mobile-only
              // "expanded memo overflows over other elements" bug. Disabling
              // the transition while expanded makes ``none`` apply at once.
              transition: expanded ? "none" : "max-height 0.2s ease",
              lineHeight: 1.5,
              fontSize: "0.9rem",
            }}
          >
            <EvidenceContent
              patientId={patient.id}
              body={patient.notes ?? ""}
              ctx={`evidence:patient:${patient.id}`}
            />
          </div>
          {/* Full-width footer toggle: a top-right "Espandi" was the only
              affordance and clinicians complained it was awkward to reach.
              The big bottom strip is the primary control now; the chevron
              rotates 180° on expand for a clear state cue. */}
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-controls="bv-clinical-notes-body"
            title={expanded ? t("collapseTitle") : t("expandTitle")}
            style={{
              width: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "0.4rem",
              padding: "0.45rem 0.85rem",
              border: "none",
              borderTop: "1px solid var(--bv-card-border)",
              background: "transparent",
              color: "inherit",
              cursor: "pointer",
              fontSize: "0.78rem",
              borderBottomLeftRadius: "var(--bv-r-md)",
              borderBottomRightRadius: "var(--bv-r-md)",
              transition: "background 0.15s ease",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = "var(--bv-hover-bg, rgba(127,127,127,0.08))";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "transparent";
            }}
          >
            <span
              aria-hidden
              style={{
                display: "inline-block",
                fontSize: "0.85rem",
                lineHeight: 1,
                transition: "transform 0.2s ease",
                transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
              }}
            >
              ▾
            </span>
            <span>{expanded ? t("collapse") : t("expand")}</span>
          </button>
        </>
      ) : (
        <p
          className="meta"
          style={{
            margin: 0,
            padding: "0.6rem 0.85rem",
            fontSize: "0.85rem",
            fontStyle: "italic",
          }}
        >
          {isOwner ? t("empty") : t("emptyReadOnly")}
        </p>
      )}
    </section>
  );
}
