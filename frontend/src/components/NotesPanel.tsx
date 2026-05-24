"use client";

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import AiAuthorBadge from "@/components/AiAuthorBadge";
import { useModal } from "@/components/ModalHost";
import { ApiError, type ClinicalNote, patientsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

interface Props {
  patientId: string;
  /**
   * Polymorphic target. The panel filters its query to this single
   * item — use ``patient`` + the patient id to attach a note to the
   * record itself rather than a specific study / document.
   */
  targetKind: ClinicalNote["target_kind"];
  targetId: string;
  /** Optional page-level title shown above the input. Overrides the
   * default localized "Notes about this <kind>" header. */
  title?: string;
  /**
   * Optional callback invoked at create-time. Whatever string it
   * returns is prepended to the note body before it is saved. Used by
   * the in-viewer mount to anchor each note to the active viewport
   * crosshair (e.g. ``[crosshair 128, 240, 47]``). The prefix is
   * stored verbatim in the body — keeps NotesPanel modular (no extra
   * column, no payload schema).
   */
  getBodyPrefix?: () => string;
}

/**
 * Per-item clinical notes panel.
 *
 * Renders a list of existing notes (newest first, pinned on top) plus a
 * compact textarea to add a new one. The same component is mounted on
 * the study detail, document detail, consultation detail pages, and on
 * the viewer right-sidebar — the difference is which (target_kind,
 * target_id) pair the parent passes in. The aggregated view at
 * ``/patients/<id>/notes`` lists everything across the fascicolo
 * without filtering.
 */
export default function NotesPanel({
  patientId,
  targetKind,
  targetId,
  title,
  getBodyPrefix,
}: Props) {
  const { user } = useAuth();
  const t = useTranslations("notesPanel");
  const modal = useModal();
  const locale = useLocale();
  const [notes, setNotes] = useState<ClinicalNote[]>([]);
  const [body, setBody] = useState("");
  const [pinned, setPinned] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await patientsApi.listNotes(patientId, {
        target_kind: targetKind,
        target_id: targetId,
      });
      setNotes(data);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [patientId, targetKind, targetId, t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    const text = body.trim();
    if (!text) return;
    setBusy(true);
    setErr(null);
    try {
      const prefix = getBodyPrefix?.() ?? "";
      const finalBody = prefix ? `${prefix}${text}` : text;
      await patientsApi.createNote(patientId, {
        target_kind: targetKind,
        target_id: targetId,
        body: finalBody,
        pinned,
      });
      setBody("");
      setPinned(false);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("saveFailed"));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(note: ClinicalNote) {
    const ok = await modal.confirm({
      message: t("confirmDelete"),
      destructive: true,
      confirmLabel: t("delete"),
    });
    if (!ok) return;
    try {
      await patientsApi.deleteNote(patientId, note.id);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("deleteFailed"));
    }
  }

  async function togglePin(note: ClinicalNote) {
    try {
      await patientsApi.updateNote(patientId, note.id, { pinned: !note.pinned });
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("updateFailed"));
    }
  }

  // Resolve the localized header. Caller can override via the ``title``
  // prop; otherwise we build "Notes about this <kind>" from the i18n
  // catalogue. next-intl falls back to the key path when the entry
  // is missing — we keep that as the forward-compat behaviour for
  // unknown kinds, and just feed the result into the parametrised
  // header.
  const targetLabel = t(`targetLabel.${targetKind}` as Parameters<typeof t>[0]);
  const headerText = title ?? t("headerWithTarget", { target: targetLabel });

  return (
    <section className="card" style={{ padding: "1rem 1.25rem", marginTop: "1rem" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: "0.5rem",
          marginBottom: "0.4rem",
        }}
      >
        <h3 style={{ margin: 0 }}>{headerText}</h3>
        <Link
          href={`/patients/${patientId}`}
          className="meta"
          style={{ fontSize: "0.78rem" }}
          title={t("evidenceLinkTitle")}
        >
          {t("evidenceLink")}
        </Link>
      </div>
      {err && <p className="error">{err}</p>}

      <form
        onSubmit={onCreate}
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "0.5rem",
          marginBottom: "0.75rem",
        }}
      >
        <textarea
          rows={3}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder={t("placeholder")}
          disabled={busy}
          style={{ width: "100%" }}
        />
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "0.5rem",
          }}
        >
          <label
            className="meta"
            style={{ display: "inline-flex", gap: "0.4rem", alignItems: "center" }}
          >
            <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
            {t("pinnedLabel")}
          </label>
          <button type="submit" disabled={busy || !body.trim()}>
            {busy ? "…" : t("addNote")}
          </button>
        </div>
      </form>

      {notes.length === 0 ? (
        <p className="meta">{t("empty")}</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {notes.map((n) => (
            <NoteRow
              key={n.id}
              note={n}
              locale={locale}
              canMutate={user?.subject_id === n.author_subject_id || !!user?.is_admin}
              onPinToggle={() => togglePin(n)}
              onDelete={() => onDelete(n)}
              onUpdate={async (next) => {
                try {
                  await patientsApi.updateNote(patientId, n.id, next);
                  await refresh();
                } catch (e) {
                  setErr(e instanceof ApiError ? e.message : t("saveFailed"));
                  throw e;
                }
              }}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

interface NoteRowProps {
  note: ClinicalNote;
  locale: string;
  canMutate: boolean;
  onPinToggle: () => Promise<void> | void;
  onDelete: () => Promise<void> | void;
  onUpdate: (input: { body?: string; pinned?: boolean }) => Promise<void>;
}

/**
 * Single note row with inline edit support. Click "Edit" to swap
 * the body for a textarea + save/cancel; Esc cancels, Cmd/Ctrl+Enter
 * saves. The same component is reused on the aggregated evidence page
 * via the same prop shape — keep them in sync.
 */
function NoteRow({ note, locale, canMutate, onPinToggle, onDelete, onUpdate }: NoteRowProps) {
  const t = useTranslations("notesPanel");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note.body);
  const [busy, setBusy] = useState(false);

  // Map next-intl locale to BCP-47 for Date.toLocaleString.
  const intlLocale = locale === "it" ? "it-IT" : "en-US";

  async function save() {
    const body = draft.trim();
    if (!body || body === note.body) {
      setEditing(false);
      setDraft(note.body);
      return;
    }
    setBusy(true);
    try {
      await onUpdate({ body });
      setEditing(false);
    } catch {
      // parent surfaced the error already
    } finally {
      setBusy(false);
    }
  }

  return (
    <li
      style={{
        padding: "0.65rem 0",
        borderTop: "1px solid var(--bv-divider)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: "0.5rem",
          marginBottom: "0.2rem",
          flexWrap: "wrap",
        }}
      >
        <span
          className="meta"
          style={{
            fontSize: "0.8rem",
            display: "inline-flex",
            alignItems: "center",
            gap: "0.4rem",
            flexWrap: "wrap",
          }}
        >
          {note.is_ai_generated && <AiAuthorBadge note={note} />}
          {note.pinned && <span className="badge badge--llm">{t("pinnedBadge")}</span>}
          <span>
            {new Date(note.created_at).toLocaleString(intlLocale, {
              day: "2-digit",
              month: "short",
              year: "numeric",
              hour: "2-digit",
              minute: "2-digit",
              timeZoneName: "short",
            })}
          </span>
          {note.updated_at !== note.created_at && (
            <span style={{ opacity: 0.7 }}>
              {t("editedAt", {
                date: new Date(note.updated_at).toLocaleDateString(intlLocale),
              })}
            </span>
          )}
          <NoteOriginLink note={note} />
        </span>
        {canMutate && !editing && (
          <span style={{ display: "inline-flex", gap: "0.35rem" }}>
            <button
              type="button"
              className="ghost"
              onClick={() => setEditing(true)}
              style={{ fontSize: "0.75rem", padding: "0.2rem 0.55rem" }}
            >
              {t("edit")}
            </button>
            <button
              type="button"
              className="ghost"
              onClick={onPinToggle}
              style={{ fontSize: "0.75rem", padding: "0.2rem 0.55rem" }}
            >
              {note.pinned ? t("unpin") : t("pin")}
            </button>
            <button
              type="button"
              className="ghost"
              onClick={onDelete}
              style={{
                fontSize: "0.75rem",
                padding: "0.2rem 0.55rem",
                color: "var(--bv-danger)",
              }}
            >
              {t("delete")}
            </button>
          </span>
        )}
      </div>

      {editing ? (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
          <textarea
            // biome-ignore lint/a11y/noAutofocus: textarea mounts only when the user clicks edit; focus is the obvious continuation of that click.
            autoFocus
            rows={Math.min(10, Math.max(3, draft.split("\n").length + 1))}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setEditing(false);
                setDraft(note.body);
              } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void save();
              }
            }}
            disabled={busy}
            style={{ width: "100%" }}
          />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.4rem" }}>
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setEditing(false);
                setDraft(note.body);
              }}
              disabled={busy}
              style={{ fontSize: "0.8rem" }}
            >
              {t("cancel")}
            </button>
            <button
              type="button"
              onClick={save}
              disabled={busy || !draft.trim()}
              style={{ fontSize: "0.8rem" }}
            >
              {busy ? "…" : t("save")}
            </button>
          </div>
        </div>
      ) : (
        <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.55 }}>{note.body}</div>
      )}
    </li>
  );
}

/**
 * Origin pill — clickable shortcut back to the entity the note is
 * attached to. Critical when the same note shows up in the aggregated
 * patient evidence page: without this, a series-bound finding loses
 * its provenance the moment the viewer is closed. Resolves to:
 *   - ``series`` → the viewer for that series (with the anchor
 *     crosshair, when present, set on landing — handled separately
 *     by the viewer page).
 *   - ``study`` → the study detail page.
 *   - ``document`` → the document preview page.
 *   - ``consultation`` → the consultation page.
 *   - ``patient`` → suppressed (the link would loop back to the
 *     same fascicolo the user is already on).
 */
function NoteOriginLink({ note }: { note: ClinicalNote }) {
  if (note.target_kind === "patient") return null;
  let href: string | null = null;
  let label: string;
  switch (note.target_kind) {
    case "series":
      href = `/viewer/series/${note.target_id}`;
      label = "Open series";
      break;
    case "study":
      href = `/patients/${note.patient_id}/studies/${note.target_id}`;
      label = "Open study";
      break;
    case "document":
      href = `/patients/${note.patient_id}/documents/${note.target_id}`;
      label = "Open document";
      break;
    case "consultation":
      href = `/patients/${note.patient_id}/consultations/${note.target_id}`;
      label = "Open consultation";
      break;
    default:
      return null;
  }
  return (
    <Link
      href={href}
      className="badge"
      style={{
        textDecoration: "none",
        fontSize: "0.7rem",
        padding: "0.05rem 0.35rem",
      }}
      title={`This note is attached to a ${note.target_kind}. Click to open it.`}
    >
      → {label}
    </Link>
  );
}
