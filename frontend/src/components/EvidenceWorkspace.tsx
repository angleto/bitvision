"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import AiAuthorBadge from "@/components/AiAuthorBadge";
import EvidenceContent from "@/components/EvidenceContent";
import EvidenceEditor from "@/components/EvidenceEditor";
import { useModal } from "@/components/ModalHost";
import {
  ApiError,
  type ClinicalNote,
  type Consultation,
  type Patient,
  type PatientDocument,
  type Study,
  consultationsApi,
  patientsApi,
  studiesApi,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { type EvidenceLinkViolation, parseEvidenceLinkError } from "@/lib/evidenceLinks";

const TARGET_KIND_KEY: Record<
  ClinicalNote["target_kind"],
  "study" | "series" | "document" | "consultation" | "folder"
> = {
  study: "study",
  series: "series",
  document: "document",
  consultation: "consultation",
  // The "patient" target is the synthetic root (the fascicolo itself);
  // reuse the folder label so the UI reads "Fascicolo" / "Health
  // record" without introducing a separate translation key.
  patient: "folder",
};

interface Props {
  patient: Patient;
}

/**
 * Aggregated evidence + synthesis workspace.
 *
 * UX model: the clinician writes per-item notes from anywhere in the
 * fascicolo (study viewer, document detail, etc.). Here those notes
 * surface as time-stamped evidence, each linking back to its source.
 * Above the evidence list there is an editable "Sintesi clinica"
 * panel: the same human, after reading their own evidence, drafts a
 * coherent narrative. The synthesis is persisted as a Consultation
 * (``status: draft``) so it can be later signed and turned into a
 * formal clinical artefact.
 */
export default function EvidenceWorkspace({ patient }: Props) {
  const patientId = patient.id;
  const { user } = useAuth();
  const tNp = useTranslations("notesPage");
  const synthesisTitle = tNp("synthesisTitle");

  const [notes, setNotes] = useState<ClinicalNote[]>([]);
  const [studyTitles, setStudyTitles] = useState<Record<string, string>>({});
  const [docTitles, setDocTitles] = useState<Record<string, string>>({});
  const [synthesis, setSynthesis] = useState<Consultation | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<NotesFilter>({
    hideAi: false,
    excludedModels: new Set<string>(),
  });

  const refresh = useCallback(async () => {
    if (!patientId) return;
    try {
      const [n, docs, conss] = await Promise.all([
        patientsApi.listNotes(patientId),
        patientsApi.listDocuments(patientId),
        consultationsApi.list(patientId, { status: "all" }),
      ]);
      setNotes(n);
      setDocTitles(Object.fromEntries(docs.map((d: PatientDocument) => [d.id, d.title])));
      // Pick the latest draft synthesis by the current user as the
      // "active" one. Drafts authored by other users stay listed but
      // we don't auto-edit them — would be confusing. Title matching
      // accepts either the locale's label or the historical IT/EN
      // strings so drafts created before the i18n cleanup keep loading.
      const synthesisTitleHistory = new Set([
        synthesisTitle,
        "Sintesi clinica del fascicolo",
        "Clinical synthesis of the health record",
      ]);
      const mine = conss.filter(
        (c) =>
          c.author_kind === "human" &&
          c.status === "draft" &&
          (user?.subject_id === c.author_subject_id || user?.is_admin) &&
          synthesisTitleHistory.has(c.title),
      );
      mine.sort((a, b) =>
        (b.updated_at || b.created_at).localeCompare(a.updated_at || a.created_at),
      );
      setSynthesis(mine[0] ?? null);

      const studyIds = Array.from(
        new Set(n.filter((x) => x.target_kind === "study").map((x) => x.target_id)),
      );
      if (studyIds.length > 0) {
        const studies = await Promise.all(
          studyIds.map((id) => studiesApi.detail(id).catch(() => null as Study | null)),
        );
        setStudyTitles(
          Object.fromEntries(
            studies
              .filter((s): s is Study => s !== null)
              .map((s) => [
                s.id,
                s.study_description || `Study ${s.study_instance_uid.slice(-12)}`,
              ]),
          ),
        );
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [patientId, user, synthesisTitle]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <>
      {err && <p className="error">{err}</p>}

      <SynthesisPanel patientId={patientId} existing={synthesis} onSaved={refresh} />

      <NotesFilterBar notes={notes} filter={filter} onFilterChange={setFilter} />

      <NotesAggregateView
        notes={notes}
        patient={patient}
        patientId={patientId}
        studyTitles={studyTitles}
        docTitles={docTitles}
        filter={filter}
        canMutate={(n) => user?.subject_id === n.author_subject_id || !!user?.is_admin}
        onUpdate={async (id, next) => {
          await patientsApi.updateNote(patientId, id, next);
          await refresh();
        }}
        onDelete={async (id) => {
          await patientsApi.deleteNote(patientId, id);
          await refresh();
        }}
      />
    </>
  );
}

// ============= Filter bar =============

interface NotesFilter {
  hideAi: boolean;
  excludedModels: Set<string>;
}

function NotesFilterBar({
  notes,
  filter,
  onFilterChange,
}: {
  notes: ClinicalNote[];
  filter: NotesFilter;
  onFilterChange: (next: NotesFilter) => void;
}) {
  const tNp = useTranslations("notesPage");
  // Distinct AI models that produced at least one note for this
  // patient. Sorted so the chip order is stable across reloads.
  const aiNotes = notes.filter((n) => n.is_ai_generated);
  const models = Array.from(
    new Set(aiNotes.map((n) => n.model_id).filter((m): m is string => !!m)),
  ).sort();
  const aiCount = aiNotes.length;

  if (aiCount === 0) return null;

  function toggleModel(m: string) {
    const next = new Set(filter.excludedModels);
    if (next.has(m)) next.delete(m);
    else next.add(m);
    onFilterChange({ ...filter, excludedModels: next });
  }

  return (
    <section
      style={{
        marginTop: "1rem",
        padding: "0.6rem 0.85rem",
        background: "var(--bv-card-bg)",
        border: "1px solid var(--bv-card-border)",
        borderRadius: "var(--bv-r-sm)",
        display: "flex",
        flexWrap: "wrap",
        gap: "0.6rem",
        alignItems: "center",
      }}
    >
      <span className="meta" style={{ fontSize: "0.82rem" }}>
        {tNp("filterTitle")}
      </span>

      <label
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "0.35rem",
          fontSize: "0.85rem",
        }}
      >
        <input
          type="checkbox"
          checked={filter.hideAi}
          onChange={(e) => onFilterChange({ ...filter, hideAi: e.target.checked })}
        />
        {tNp("filterHideAllAi", { n: aiCount })}
      </label>

      {models.length > 0 && !filter.hideAi && (
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.4rem",
            flexWrap: "wrap",
          }}
        >
          <span className="meta" style={{ fontSize: "0.78rem" }}>
            {tNp("filterByModel")}
          </span>
          {models.map((m) => {
            const excluded = filter.excludedModels.has(m);
            return (
              <button
                key={m}
                type="button"
                onClick={() => toggleModel(m)}
                title={
                  excluded
                    ? tNp("filterReinclude", { model: m })
                    : tNp("filterHideModel", { model: m })
                }
                style={{
                  fontSize: "0.74rem",
                  padding: "0.2rem 0.55rem",
                  borderRadius: 999,
                  border: `1px solid ${excluded ? "var(--bv-card-border)" : "var(--bv-warning)"}`,
                  background: excluded ? "var(--bv-card-bg)" : "var(--bv-warning-soft)",
                  color: excluded ? "var(--bv-muted)" : "var(--bv-warning)",
                  textDecoration: excluded ? "line-through" : "none",
                  cursor: "pointer",
                }}
              >
                {m}
              </button>
            );
          })}
        </span>
      )}
    </section>
  );
}

// ============= Aggregated view: pinned + tutte =============

interface AggregateProps {
  notes: ClinicalNote[];
  patient: Patient;
  patientId: string;
  studyTitles: Record<string, string>;
  docTitles: Record<string, string>;
  canMutate: (n: ClinicalNote) => boolean;
  onUpdate: (id: string, next: { body?: string; pinned?: boolean }) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  filter: NotesFilter;
}

function NotesAggregateView({
  notes,
  patient,
  patientId,
  studyTitles,
  docTitles,
  canMutate,
  onUpdate,
  onDelete,
  filter,
}: AggregateProps) {
  const modal = useModal();
  const tn = useTranslations("notesPanel");
  const tNp = useTranslations("notesPage");
  // Apply the AI filters from the toolbar before grouping.
  const visible = notes.filter((n) => {
    if (filter.hideAi && n.is_ai_generated) return false;
    if (n.is_ai_generated && n.model_id && filter.excludedModels.has(n.model_id)) return false;
    return true;
  });
  const hidden = notes.length - visible.length;

  // Always sort by created_at desc within each group. The pinned vs
  // not-pinned split is the only grouping; within a group it's pure
  // chronology (newest first) — never re-sort by pin date or anything
  // else, that's what the user explicitly asked for.
  const byDateDesc = (a: ClinicalNote, b: ClinicalNote) => b.created_at.localeCompare(a.created_at);
  const pinned = visible.filter((n) => n.pinned).sort(byDateDesc);
  const others = visible.filter((n) => !n.pinned).sort(byDateDesc);

  if (notes.length === 0) {
    return (
      <>
        <h2 style={{ marginTop: "1.5rem" }}>{tNp("evidenceCount", { n: 0 })}</h2>
        <p className="meta">{tNp("evidenceEmpty")}</p>
      </>
    );
  }
  if (visible.length === 0) {
    return (
      <>
        <h2 style={{ marginTop: "1.5rem" }}>
          {tNp("evidenceCountFiltered", { visible: 0, total: notes.length })}
        </h2>
        <p className="meta">{tNp("evidenceAllHidden", { n: hidden })}</p>
      </>
    );
  }

  const renderRow = (n: ClinicalNote) => (
    <NoteRow
      key={n.id}
      note={n}
      patientId={patientId}
      patientName={patient.display_name}
      studyTitle={studyTitles[n.target_id]}
      docTitle={docTitles[n.target_id]}
      canMutate={canMutate(n)}
      onUpdate={(next) => onUpdate(n.id, next)}
      onDelete={async () => {
        const ok = await modal.confirm({
          message: tn("confirmDelete"),
          destructive: true,
          confirmLabel: tn("delete"),
        });
        if (!ok) return;
        await onDelete(n.id);
      }}
      onTogglePin={() => onUpdate(n.id, { pinned: !n.pinned })}
    />
  );

  return (
    <>
      {pinned.length > 0 && (
        <section style={{ marginTop: "1.5rem" }}>
          <h2
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              marginBottom: "0.75rem",
            }}
          >
            <span aria-hidden style={{ color: "var(--bv-warning)" }}>
              ★
            </span>
            {tNp("pinnedSection", { n: pinned.length })}
          </h2>
          <p className="meta" style={{ fontSize: "0.82rem", marginTop: "-0.25rem" }}>
            {tNp("pinnedHint")}
          </p>
          <div>{pinned.map(renderRow)}</div>
        </section>
      )}

      <section style={{ marginTop: pinned.length > 0 ? "2rem" : "1.5rem" }}>
        <h2 style={{ marginBottom: "0.75rem" }}>
          {pinned.length > 0
            ? tNp("othersSection", { n: others.length })
            : tNp("evidenceCount", { n: others.length })}
        </h2>
        {others.length === 0 ? (
          <p className="meta">{tNp("othersEmpty")}</p>
        ) : (
          <div>{others.map(renderRow)}</div>
        )}
      </section>
    </>
  );
}

// ============= Synthesis panel =============

interface SynthesisProps {
  patientId: string;
  existing: Consultation | null;
  onSaved: () => Promise<void>;
}

function SynthesisPanel({ patientId, existing, onSaved }: SynthesisProps) {
  const tNp = useTranslations("notesPage");
  const synthesisTitle = tNp("synthesisTitle");
  const [body, setBody] = useState(existing?.summary_md ?? "");
  const [editing, setEditing] = useState(!existing);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Cross-patient violations returned by the consultations endpoint
  // when the synthesis body contains a forbidden ``@kind:UUID``
  // mention. Cleared on every successful save so the banner doesn't
  // linger after the user fixes the offending span.
  const [linkErrors, setLinkErrors] = useState<EvidenceLinkViolation[]>([]);

  // Whenever the parent reloads (e.g. after save), keep our editor
  // in sync with the latest server state but don't blow away an
  // in-progress edit.
  useEffect(() => {
    if (!editing) {
      setBody(existing?.summary_md ?? "");
    }
  }, [existing, editing]);

  async function save() {
    const text = body.trim();
    if (!text) return;
    setBusy(true);
    setErr(null);
    try {
      if (existing) {
        await consultationsApi.update(existing.id, { summary_md: text });
      } else {
        await consultationsApi.create({
          patient_id: patientId,
          title: synthesisTitle,
          summary_md: text,
          author_kind: "human",
          status: "draft",
        });
      }
      setEditing(false);
      setLinkErrors([]);
      await onSaved();
    } catch (e) {
      // Same shape as NoteRow: HTTP 422 with our structured ``detail``
      // becomes inline EvidenceLinkViolations rendered above the editor.
      if (e instanceof ApiError && e.status === 422) {
        const parsed = parseEvidenceLinkError(e.detail);
        if (parsed && parsed.violations.length > 0) {
          setLinkErrors(parsed.violations);
          setBusy(false);
          return;
        }
      }
      setErr(e instanceof ApiError ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="card"
      style={{
        padding: "1rem 1.25rem",
        borderColor: existing ? "var(--bv-card-border)" : "var(--bv-accent)",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "0.5rem",
          gap: "0.5rem",
          flexWrap: "wrap",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1.05rem" }}>
          {synthesisTitle}{" "}
          {existing && (
            <span className="badge" style={{ marginLeft: "0.4rem", verticalAlign: "middle" }}>
              {tNp("synthesisDraftBadge")}
            </span>
          )}
        </h2>
        {!editing && existing && (
          <span style={{ display: "inline-flex", gap: "0.4rem" }}>
            <Link
              href={`/patients/${patientId}/consultations/${existing.id}`}
              className="ghost"
              style={{
                fontSize: "0.8rem",
                padding: "0.3rem 0.7rem",
                border: "1px solid var(--bv-card-border)",
                borderRadius: 6,
                textDecoration: "none",
              }}
            >
              {tNp("synthesisOpenAsConsultation")}
            </Link>
            <button
              type="button"
              className="ghost"
              onClick={() => setEditing(true)}
              style={{ fontSize: "0.8rem" }}
            >
              {tNp("synthesisEdit")}
            </button>
          </span>
        )}
      </div>

      <p className="meta" style={{ fontSize: "0.78rem", margin: "0 0 0.6rem" }}>
        {tNp("synthesisCaption")}
      </p>

      {err && <p className="error">{err}</p>}

      {editing ? (
        <>
          <EvidenceEditor
            value={body}
            onChange={setBody}
            onSave={save}
            patientId={patientId}
            onCancel={() => {
              if (existing) {
                setEditing(false);
                setBody(existing.summary_md ?? "");
              }
              setLinkErrors([]);
            }}
            busy={busy}
            saveLabel={existing ? tNp("synthesisSave") : tNp("synthesisCreate")}
            saveBusyLabel={tNp("synthesisBusy")}
            cancelLabel={tNp("synthesisCancel")}
            errors={linkErrors}
          />
          <p className="meta" style={{ fontSize: "0.78rem", marginTop: "0.4rem" }}>
            {existing ? tNp("synthesisHelpExisting") : tNp("synthesisHelpNew")}
          </p>
        </>
      ) : existing ? (
        <div style={{ lineHeight: 1.6 }}>
          <EvidenceContent
            patientId={patientId}
            body={existing.summary_md ?? ""}
            ctx={`evidence:consultation:${existing.id}`}
          />
          <p className="meta" style={{ marginTop: "0.75rem", fontSize: "0.78rem" }}>
            {tNp("synthesisLastEdit", {
              when: new Date(existing.updated_at || existing.created_at).toLocaleString(),
            })}
          </p>
        </div>
      ) : (
        <p className="meta">{tNp("synthesisEmpty")}</p>
      )}
    </section>
  );
}

// ============= Note row =============

interface NoteRowProps {
  note: ClinicalNote;
  patientId: string;
  patientName: string;
  studyTitle?: string;
  docTitle?: string;
  canMutate: boolean;
  onUpdate: (input: { body?: string; pinned?: boolean }) => Promise<void>;
  onDelete: () => Promise<void>;
  onTogglePin: () => Promise<void>;
}

function NoteRow({
  note,
  patientId,
  patientName,
  studyTitle,
  docTitle,
  canMutate,
  onUpdate,
  onDelete,
  onTogglePin,
}: NoteRowProps) {
  const tNp = useTranslations("notesPage");
  const tFasc = useTranslations("fascicolo");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note.body);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLElement | null>(null);
  const [highlight, setHighlight] = useState(false);
  // Cross-patient violations returned by the server when the body
  // contains a forbidden ``@kind:UUID`` mention. Cleared on every
  // successful save so the banner doesn't linger after the user
  // fixes the offending span.
  const [linkErrors, setLinkErrors] = useState<EvidenceLinkViolation[]>([]);

  // When the user comes back from a source page (e.g. document or
  // study they jumped into from this note), the URL carries a
  // ``#note-<id>`` hash. We scroll the matching row into view and
  // pulse a 2-second highlight so the eye finds it without searching.
  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-only flash for the hash-targeted note; ``note.id`` doesn't change for the lifetime of this row.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const hash = window.location.hash;
    if (hash !== `#note-${note.id}`) return;
    const el = ref.current;
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlight(true);
    const t = window.setTimeout(() => setHighlight(false), 2200);
    return () => window.clearTimeout(t);
  }, []);

  // ``?from=notes`` lets the destination page render a "Torna alle
  // evidenze" back link instead of the generic patient/study back, so
  // the clinician keeps their place in the evidence list when they
  // jump out to inspect the underlying source.
  const targetUrl = (() => {
    const q = `?from=notes&note=${note.id}`;
    switch (note.target_kind) {
      case "study":
      case "series":
        return `/patients/${patientId}/studies/${note.target_id}${q}`;
      case "document":
        return `/patients/${patientId}/documents/${note.target_id}${q}`;
      case "consultation":
        return `/patients/${patientId}/consultations/${note.target_id}${q}`;
      case "patient":
        return `/patients/${patientId}${q}`;
    }
  })();

  const targetLabel = (() => {
    if (note.target_kind === "study" && studyTitle) return studyTitle;
    if (note.target_kind === "document" && docTitle) return docTitle;
    if (note.target_kind === "patient") return patientName;
    return `${tFasc(`kind.${TARGET_KIND_KEY[note.target_kind]}`)} ${note.target_id.slice(0, 8)}`;
  })();

  const stamp = new Date(note.created_at).toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

  async function save() {
    const body = draft.trim();
    if (!body || body === note.body) {
      setEditing(false);
      setDraft(note.body);
      setLinkErrors([]);
      return;
    }
    setBusy(true);
    try {
      await onUpdate({ body });
      setEditing(false);
      setLinkErrors([]);
    } catch (e) {
      // The backend rejects cross-patient mentions with HTTP 422 and
      // a structured detail. Surface every violation inline so the
      // user can find and fix the offending span(s) before retrying.
      // Other errors propagate (the parent shows them).
      if (e instanceof ApiError && e.status === 422) {
        // FastAPI wraps HTTPException.detail in ``{"detail": ...}``;
        // unwrap once before handing it to the structured parser.
        const parsed = parseEvidenceLinkError(e.detail);
        if (parsed && parsed.violations.length > 0) {
          setLinkErrors(parsed.violations);
          return;
        }
      }
      throw e;
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      id={`note-${note.id}`}
      ref={ref}
      className="card"
      style={{
        padding: "1rem 1.25rem",
        marginBottom: "0.85rem",
        // Pulse highlight when the user returns to this row via the
        // ``#note-<id>`` hash. Smooth fade after ~2s.
        boxShadow: highlight ? "0 0 0 3px var(--bv-accent-soft), var(--bv-shadow-2)" : undefined,
        borderColor: highlight ? "var(--bv-accent)" : undefined,
        transition: "box-shadow 0.4s ease, border-color 0.4s ease",
        scrollMarginTop: "calc(var(--header-h, 56px) + 1rem)",
      }}
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.5rem",
          marginBottom: "0.5rem",
        }}
      >
        {note.is_ai_generated && <AiAuthorBadge note={note} size="sm" />}
        {note.pinned && <span className="badge badge--llm">{tNp("rowPinnedBadge")}</span>}
        <span className="badge">{tFasc(`kind.${TARGET_KIND_KEY[note.target_kind]}`)}</span>
        <Link
          href={targetUrl}
          style={{
            fontWeight: 500,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            maxWidth: "100%",
          }}
        >
          {targetLabel}
        </Link>
        <span className="meta" style={{ marginLeft: "auto", fontSize: "0.8rem" }}>
          {stamp}
          {note.updated_at !== note.created_at && (
            <span style={{ opacity: 0.7, marginLeft: "0.4rem" }}>{tNp("rowEdited")}</span>
          )}
        </span>
      </div>

      {editing ? (
        <EvidenceEditor
          value={draft}
          onChange={setDraft}
          onSave={save}
          patientId={patientId}
          onCancel={() => {
            setEditing(false);
            setDraft(note.body);
            setLinkErrors([]);
          }}
          busy={busy}
          saveLabel={tNp("rowSave")}
          saveBusyLabel={tNp("rowBusy")}
          cancelLabel={tNp("rowCancel")}
          errors={linkErrors}
        />
      ) : (
        <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.55 }}>
          <EvidenceContent
            patientId={patientId}
            body={note.body}
            ctx={`evidence:note:${note.id}`}
          />
        </div>
      )}

      {canMutate && !editing && (
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.35rem",
            marginTop: "0.5rem",
          }}
        >
          <button
            type="button"
            className="ghost"
            onClick={() => setEditing(true)}
            style={{ fontSize: "0.75rem", padding: "0.2rem 0.55rem" }}
          >
            {tNp("rowEdit")}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={onTogglePin}
            style={{ fontSize: "0.75rem", padding: "0.2rem 0.55rem" }}
          >
            {note.pinned ? tNp("rowUnpin") : tNp("rowPin")}
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
            {tNp("rowDelete")}
          </button>
        </div>
      )}
    </article>
  );
}
