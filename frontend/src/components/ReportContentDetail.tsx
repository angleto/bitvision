"use client";

// Detail card for one ReportContent (original referto, AI-derived
// extraction, or canonical synthesis) attached to a ClinicalEvent.
//
// Read side renders ``narrative_md`` / ``findings_md`` /
// ``recommendations_md`` through ``EvidenceContent`` so the
// ``@kind:UUID`` mention DSL becomes clickable pills (the same way it
// works in clinical notes and the patient-event description). Plain
// markdown without mentions still round-trips correctly.
//
// Edit side reuses ``EvidenceEditor`` (TipTap WYSIWYG + raw-markdown
// toggle + patient-scoped @-autocomplete) for every editable markdown
// field. Editable fields by authority:
//
// * ``derived`` / ``original``: ``title`` + ``narrative_md`` only —
//   the strucured ``findings_md`` / ``recommendations_md`` are
//   reserved for the canonical synthesis (the backend silently drops
//   them on a non-canonical PATCH so showing the editors here would
//   be misleading).
// * ``canonical_synthesis``: ``title`` + ``narrative_md`` +
//   ``findings_md`` + ``recommendations_md``.
//
// Edit is hidden when the row is in a terminal status (``signed``,
// ``stale``, ``rejected``); the workflow buttons (endorse / reject /
// sign / supersede) keep the legacy semantics.
//
// Cross-patient guard is enforced server-side at PATCH time. A 422
// response with ``{code: 'cross_patient_or_missing_link', violations
// []}`` is surfaced inline above the editor so the user can locate
// and remove the offending mention(s).

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import EvidenceContent from "@/components/EvidenceContent";
import EvidenceEditor from "@/components/EvidenceEditor";
import { ApiError } from "@/lib/api";
import {
  type ReportContent,
  type ReportContentAuthority,
  type ReportContentStatus,
  reportContentsApi,
} from "@/lib/api_records";
import { type EvidenceLinkViolation, parseEvidenceLinkError } from "@/lib/evidenceLinks";

const AUTHORITY_COLOR: Record<ReportContentAuthority, string> = {
  original: "#059669",
  derived: "#d97706",
  canonical_synthesis: "#2563eb",
  stale: "#9ca3af",
};

const TERMINAL_STATUSES: ReadonlySet<ReportContentStatus> = new Set([
  "signed",
  "stale",
  "rejected",
]);

interface Props {
  rc: ReportContent;
  /** Called after a successful workflow action so the parent can refetch. */
  onChanged?: () => void;
  /** Patient that owns the parent event. Required: the read-side
   *  ``EvidenceContent`` resolves mentions against this patient and
   *  the inline edit's @-autocomplete is patient-scoped. */
  patientId: string;
  /** Event that produced this report. Forwarded as
   *  ``?from=event&event=<id>`` on document links so the destination
   *  page can render a back-link to the event. */
  eventId?: string;
  /** When true, scroll this card into view on mount and pulse its
   *  border for ~2 seconds. Used by ``ClinicalEventContent`` when the
   *  URL carries a ``#rc-<id>`` hash (deep-link from the Chiedi tab's
   *  citation chip) so the cited report jumps to the user's eye
   *  without searching the page. Mirrors the ``#note-<id>`` pattern
   *  in ``EvidenceWorkspace.NoteRow``. */
  highlight?: boolean;
}

export default function ReportContentDetail({
  rc,
  onChanged,
  patientId,
  eventId,
  highlight = false,
}: Props) {
  const t = useTranslations("reportContent");
  const tAuthority = useTranslations("reportContent.authority");
  const tStatus = useTranslations("reportContent.status");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmOp, setConfirmOp] = useState<"endorse" | "reject" | "sign" | "supersede" | null>(
    null,
  );
  const [opInput, setOpInput] = useState("");

  // Edit-in-place state. ``editing`` flips the body into editors;
  // each editable field has its own draft string so we can compute
  // the diff on save and send only the changed fields.
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(rc.title ?? "");
  const [draftNarrative, setDraftNarrative] = useState(rc.narrative_md ?? "");
  const [draftFindings, setDraftFindings] = useState(rc.findings_md ?? "");
  const [draftRecs, setDraftRecs] = useState(rc.recommendations_md ?? "");
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [linkErrors, setLinkErrors] = useState<EvidenceLinkViolation[]>([]);

  async function run(op: typeof confirmOp) {
    if (!op) return;
    setError(null);
    setBusy(true);
    try {
      switch (op) {
        case "endorse":
          await reportContentsApi.endorse(rc.id, rc.etag);
          break;
        case "reject":
          if (!opInput.trim()) {
            throw new Error(t("errorRejectReasonRequired"));
          }
          await reportContentsApi.reject(rc.id, rc.etag, opInput.trim());
          break;
        case "sign":
          if (opInput.trim() !== (rc.title ?? "").trim()) {
            throw new Error(t("errorTitleMismatch"));
          }
          await reportContentsApi.sign(rc.id, rc.etag, opInput.trim());
          break;
        case "supersede":
          if (!opInput.trim()) {
            throw new Error(t("errorSupersedeReasonRequired"));
          }
          await reportContentsApi.supersede(rc.id, rc.etag, {
            reason: opInput.trim(),
          });
          break;
      }
      setConfirmOp(null);
      setOpInput("");
      onChanged?.();
    } catch (e: unknown) {
      setError(
        e instanceof ApiError
          ? `${e.status}: ${e.message}`
          : e instanceof Error
            ? e.message
            : t("errorGeneric"),
      );
    } finally {
      setBusy(false);
    }
  }

  function startEdit() {
    setDraftTitle(rc.title ?? "");
    setDraftNarrative(rc.narrative_md ?? "");
    setDraftFindings(rc.findings_md ?? "");
    setDraftRecs(rc.recommendations_md ?? "");
    setEditError(null);
    setLinkErrors([]);
    setEditing(true);
  }

  function cancelEdit() {
    setEditing(false);
    setEditError(null);
    setLinkErrors([]);
  }

  async function saveEdit() {
    setEditBusy(true);
    setEditError(null);
    setLinkErrors([]);
    try {
      const patch: {
        title?: string;
        narrative_md?: string;
        findings_md?: string;
        recommendations_md?: string;
      } = {};
      const trimTitle = draftTitle.trim();
      if (trimTitle !== (rc.title ?? "")) patch.title = trimTitle;
      const trimNarrative = draftNarrative.trim();
      if (trimNarrative !== (rc.narrative_md ?? "")) patch.narrative_md = trimNarrative;
      // Only the canonical synthesis persists findings + recommendations;
      // for original/derived we omit them from the diff regardless of
      // user input so we don't send a payload the backend would silently
      // drop.
      if (rc.authority === "canonical_synthesis") {
        const trimF = draftFindings.trim();
        if (trimF !== (rc.findings_md ?? "")) patch.findings_md = trimF;
        const trimR = draftRecs.trim();
        if (trimR !== (rc.recommendations_md ?? "")) patch.recommendations_md = trimR;
      }
      if (Object.keys(patch).length === 0) {
        setEditing(false);
        return;
      }
      await reportContentsApi.update(rc.id, rc.etag, patch);
      setEditing(false);
      onChanged?.();
    } catch (e: unknown) {
      if (e instanceof ApiError) {
        if (e.status === 412) {
          setEditError(t("concurrentEdit"));
        } else if (e.status === 422) {
          const parsed = parseEvidenceLinkError(e.detail);
          if (parsed) {
            setLinkErrors(parsed.violations);
          } else {
            setEditError(`${e.status}: ${e.message}`);
          }
        } else if (e.status === 409) {
          setEditError(`${e.status}: ${e.message}`);
        } else {
          setEditError(`${e.status}: ${e.message}`);
        }
      } else {
        setEditError(t("saveError"));
      }
    } finally {
      setEditBusy(false);
    }
  }

  const canEndorse =
    (rc.authority === "original" || rc.authority === "derived") && rc.status === "extracted_auto";
  const canReject =
    rc.authority === "canonical_synthesis" && (rc.status === "draft" || rc.status === "final");
  const canSign = rc.authority === "canonical_synthesis" && rc.status === "final";
  const canSupersede = rc.status !== "stale" && rc.authority !== "stale";
  const canEdit = !TERMINAL_STATUSES.has(rc.status) && rc.authority !== "stale";

  // Deep-link arrival: when ``highlight`` flips true, scroll the card
  // into view and turn on a 2-second pulse so the eye finds the right
  // row without scanning the page. Effect is mount-only for the
  // current hash target; ``highlight`` is stable for the lifetime of
  // a given navigation so the deps are correct.
  const articleRef = useRef<HTMLElement | null>(null);
  const [pulse, setPulse] = useState(false);
  useEffect(() => {
    if (!highlight) return;
    const el = articleRef.current;
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setPulse(true);
    const t = window.setTimeout(() => setPulse(false), 2200);
    return () => window.clearTimeout(t);
  }, [highlight]);

  return (
    <article
      id={`rc-${rc.id}`}
      ref={articleRef}
      className="report-content-detail"
      style={{
        border: `1px solid ${pulse ? "var(--bv-accent, #2563eb)" : `${AUTHORITY_COLOR[rc.authority]}33`}`,
        borderLeft: `4px solid ${AUTHORITY_COLOR[rc.authority]}`,
        borderRadius: "0.25rem",
        padding: "1rem",
        marginBottom: "1rem",
        background: "var(--card-bg, #ffffff)",
        scrollMarginTop: "calc(var(--header-h, 56px) + 1rem)",
        boxShadow: pulse
          ? "0 0 0 3px var(--bv-accent-soft, rgba(37,99,235,0.18)), 0 6px 18px rgba(0,0,0,0.12)"
          : undefined,
        transition: "box-shadow 0.4s ease, border-color 0.4s ease",
      }}
    >
      <header style={{ display: "flex", gap: "0.5rem", alignItems: "baseline" }}>
        <strong style={{ flex: 1 }}>
          {rc.title ?? t("untitled", { authority: tAuthority(rc.authority) })}
        </strong>
        <span
          style={{
            fontSize: "0.75rem",
            padding: "0.125rem 0.5rem",
            borderRadius: "999px",
            background: AUTHORITY_COLOR[rc.authority],
            color: "white",
          }}
        >
          {tAuthority(rc.authority)}
        </span>
        <span
          style={{
            fontSize: "0.75rem",
            padding: "0.125rem 0.5rem",
            borderRadius: "999px",
            background: "var(--muted-bg, #e5e7eb)",
          }}
        >
          {tStatus(rc.status)}
        </span>
        {rc.is_ai_generated && (
          <span
            title={[
              rc.provider ? `provider: ${rc.provider}` : null,
              rc.model_id ? `model: ${rc.model_id}` : null,
              `extracted: ${new Date(rc.created_at).toLocaleString()}`,
              rc.endorsed_at ? `endorsed: ${new Date(rc.endorsed_at).toLocaleString()}` : null,
              rc.signed_at ? `signed: ${new Date(rc.signed_at).toLocaleString()}` : null,
            ]
              .filter(Boolean)
              .join("\n")}
            style={{
              fontSize: "0.75rem",
              padding: "0.125rem 0.5rem",
              borderRadius: "999px",
              border: "1px dashed #d97706",
              color: "#92400e",
              cursor: "help",
            }}
          >
            AI{rc.model_id ? ` · ${rc.model_id}` : rc.provider ? ` · ${rc.provider}` : ""}
          </span>
        )}
      </header>

      {editing ? (
        <EditPanel
          patientId={patientId}
          authority={rc.authority}
          draftTitle={draftTitle}
          setDraftTitle={setDraftTitle}
          draftNarrative={draftNarrative}
          setDraftNarrative={setDraftNarrative}
          draftFindings={draftFindings}
          setDraftFindings={setDraftFindings}
          draftRecs={draftRecs}
          setDraftRecs={setDraftRecs}
          busy={editBusy}
          editError={editError}
          linkErrors={linkErrors}
          onSave={saveEdit}
          onCancel={cancelEdit}
          t={t}
        />
      ) : (
        <ReadBody
          patientId={patientId}
          rc={rc}
          tFindings={t("findings")}
          tRecommendations={t("recommendations")}
        />
      )}

      {patientId && rc.linked_documents && rc.linked_documents.length > 0 && (
        <div style={{ marginTop: "0.6rem" }}>
          <em style={{ fontSize: "0.85rem", color: "var(--muted-fg, #666)" }}>
            {t("sourceDocuments")}
          </em>
          <ul
            style={{
              margin: "0.25rem 0 0",
              paddingLeft: "1.25em",
              fontSize: "0.88rem",
              lineHeight: 1.5,
            }}
          >
            {rc.linked_documents.map((d) => {
              const back = eventId ? `?from=event&event=${eventId}` : "?from=event";
              return (
                <li key={d.id}>
                  <Link
                    href={`/patients/${patientId}/documents/${d.id}${back}`}
                    title={[
                      d.kind_id ? `tipo: ${d.kind_id}` : null,
                      d.document_date ? `data: ${d.document_date}` : null,
                      d.role ? `ruolo: ${d.role}` : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  >
                    {d.title || t("documentFallback", { id: d.id.slice(0, 8) })}
                  </Link>
                  {d.role && (
                    <span className="meta" style={{ marginLeft: "0.4rem", fontSize: "0.78rem" }}>
                      ({d.role})
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <footer
        style={{
          marginTop: "1rem",
          fontSize: "0.85rem",
          color: "var(--muted-fg, #666)",
        }}
      >
        <div>
          {new Date(rc.created_at).toLocaleString()}
          {rc.endorsed_at &&
            t("endorsedSuffix", { date: new Date(rc.endorsed_at).toLocaleString() })}
          {rc.signed_at && t("signedSuffix", { date: new Date(rc.signed_at).toLocaleString() })}
        </div>
        {rc.rejected_reason && (
          <div style={{ color: "#c00" }}>
            <strong>{t("rejectedLabel")}</strong> {rc.rejected_reason}
          </div>
        )}
        {rc.superseded_by_id && (
          <div>
            {t("supersededBy")} <code>{rc.superseded_by_id}</code>
            {rc.supersede_reason && ` — ${rc.supersede_reason}`}
          </div>
        )}
      </footer>

      {!editing && (canEdit || canEndorse || canReject || canSign || canSupersede) && (
        <div
          style={{
            marginTop: "0.75rem",
            paddingTop: "0.75rem",
            borderTop: "1px solid var(--border, #e5e7eb)",
            display: "flex",
            gap: "0.5rem",
            flexWrap: "wrap",
          }}
        >
          {canEdit && (
            <button
              type="button"
              disabled={busy}
              onClick={startEdit}
              aria-label={t("editAriaLabel")}
            >
              {t("editButton")}
            </button>
          )}
          {canEndorse && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirmOp("endorse");
                setOpInput("");
                setError(null);
              }}
            >
              {t("endorse")}
            </button>
          )}
          {canReject && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirmOp("reject");
                setOpInput("");
                setError(null);
              }}
            >
              {t("reject")}
            </button>
          )}
          {canSign && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirmOp("sign");
                setOpInput("");
                setError(null);
              }}
              style={{
                background: AUTHORITY_COLOR.canonical_synthesis,
                color: "white",
              }}
            >
              {t("sign")}
            </button>
          )}
          {canSupersede && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirmOp("supersede");
                setOpInput("");
                setError(null);
              }}
            >
              {t("supersede")}
            </button>
          )}
        </div>
      )}

      {confirmOp && (
        // biome-ignore lint/a11y/useSemanticElements: inline confirmation panel, not a centered modal — anchored under the action that triggered it. Native <dialog> would teleport to the viewport center.
        <div
          role="dialog"
          aria-label={t("confirmAriaLabel", { op: confirmOp })}
          style={{
            marginTop: "1rem",
            padding: "1rem",
            background: "var(--muted-bg, #fef3c7)",
            borderRadius: "0.25rem",
          }}
        >
          {confirmOp === "endorse" && <p style={{ margin: 0 }}>{t("endorsePrompt")}</p>}
          {confirmOp === "reject" && (
            <label>
              {t("rejectReasonLabel")}
              <textarea
                value={opInput}
                onChange={(e) => setOpInput(e.target.value)}
                rows={3}
                style={{ width: "100%", marginTop: "0.25rem" }}
              />
            </label>
          )}
          {confirmOp === "sign" && (
            <>
              <p>
                <strong>{t("legalSignTitle")}</strong> {t("legalSignBody")}
              </p>
              <label>
                {t("verbatimLabel")} <code>{rc.title}</code>
                <input
                  type="text"
                  value={opInput}
                  onChange={(e) => setOpInput(e.target.value)}
                  style={{ width: "100%", marginTop: "0.25rem" }}
                />
              </label>
            </>
          )}
          {confirmOp === "supersede" && (
            <label>
              {t("supersedeReasonLabel")}
              <textarea
                value={opInput}
                onChange={(e) => setOpInput(e.target.value)}
                rows={3}
                style={{ width: "100%", marginTop: "0.25rem" }}
              />
            </label>
          )}

          {error && (
            <p role="alert" style={{ color: "#c00", marginTop: "0.5rem" }}>
              {error}
            </p>
          )}

          <div style={{ marginTop: "0.5rem", display: "flex", gap: "0.5rem" }}>
            <button type="button" disabled={busy} onClick={() => void run(confirmOp)}>
              {busy ? t("confirmBusy") : t("confirm")}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setConfirmOp(null);
                setOpInput("");
                setError(null);
              }}
            >
              {t("cancel")}
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

function ReadBody({
  patientId,
  rc,
  tFindings,
  tRecommendations,
}: {
  patientId: string;
  rc: ReportContent;
  tFindings: string;
  tRecommendations: string;
}) {
  return (
    <>
      {rc.narrative_md && (
        <div
          style={{
            background: "var(--muted-bg, #f3f4f6)",
            padding: "0.5rem 0.7rem",
            borderRadius: "0.25rem",
            margin: "0.5rem 0",
            fontSize: "0.95em",
            lineHeight: 1.55,
          }}
        >
          <EvidenceContent patientId={patientId} body={rc.narrative_md} />
        </div>
      )}

      {rc.findings_md && (
        <div style={{ marginTop: "0.5rem" }}>
          <em>{tFindings}</em>
          <div
            style={{
              background: "var(--muted-bg, #f3f4f6)",
              padding: "0.5rem 0.7rem",
              borderRadius: "0.25rem",
              fontSize: "0.9em",
              lineHeight: 1.55,
              marginTop: "0.25rem",
            }}
          >
            <EvidenceContent patientId={patientId} body={rc.findings_md} />
          </div>
        </div>
      )}

      {rc.recommendations_md && (
        <div style={{ marginTop: "0.5rem" }}>
          <em>{tRecommendations}</em>
          <div
            style={{
              background: "var(--muted-bg, #f3f4f6)",
              padding: "0.5rem 0.7rem",
              borderRadius: "0.25rem",
              fontSize: "0.9em",
              lineHeight: 1.55,
              marginTop: "0.25rem",
            }}
          >
            <EvidenceContent patientId={patientId} body={rc.recommendations_md} />
          </div>
        </div>
      )}
    </>
  );
}

interface EditPanelProps {
  patientId: string;
  authority: ReportContentAuthority;
  draftTitle: string;
  setDraftTitle: (s: string) => void;
  draftNarrative: string;
  setDraftNarrative: (s: string) => void;
  draftFindings: string;
  setDraftFindings: (s: string) => void;
  draftRecs: string;
  setDraftRecs: (s: string) => void;
  busy: boolean;
  editError: string | null;
  linkErrors: EvidenceLinkViolation[];
  onSave: () => void | Promise<void>;
  onCancel: () => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}

function EditPanel({
  patientId,
  authority,
  draftTitle,
  setDraftTitle,
  draftNarrative,
  setDraftNarrative,
  draftFindings,
  setDraftFindings,
  draftRecs,
  setDraftRecs,
  busy,
  editError,
  linkErrors,
  onSave,
  onCancel,
  t,
}: EditPanelProps) {
  const showFindings = authority === "canonical_synthesis";
  const showRecs = authority === "canonical_synthesis";
  return (
    <div style={{ marginTop: "0.5rem" }}>
      {editError && (
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
          {editError}
        </p>
      )}
      {linkErrors.length > 0 && (
        <div
          role="alert"
          style={{
            margin: "0 0 0.5rem",
            padding: "8px 10px",
            background: "var(--bv-danger-soft, #fef2f2)",
            color: "var(--bv-danger, #b91c1c)",
            border: "1px solid var(--bv-danger, #b91c1c)",
            borderRadius: 4,
            fontSize: "0.82rem",
          }}
        >
          <strong style={{ display: "block", marginBottom: 4 }}>{t("linkErrorTitle")}</strong>
          <p style={{ margin: "0 0 4px" }}>{t("linkErrorBody")}</p>
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {linkErrors.map((v) => (
              <li key={v.raw}>
                <code>{v.raw}</code> ({v.reason})
              </li>
            ))}
          </ul>
        </div>
      )}
      <label style={{ display: "block", marginBottom: "0.5rem" }}>
        <span className="meta" style={{ fontSize: "0.78rem" }}>
          {t("fieldTitle")}
        </span>
        <input
          type="text"
          value={draftTitle}
          onChange={(e) => setDraftTitle(e.target.value)}
          maxLength={255}
          style={{ width: "100%", marginTop: "0.2rem" }}
        />
      </label>
      <div style={{ marginBottom: "0.5rem" }}>
        <span className="meta" style={{ fontSize: "0.78rem" }}>
          {t("fieldNarrative")}
        </span>
        <div style={{ marginTop: "0.2rem" }}>
          <EvidenceEditor
            value={draftNarrative}
            onChange={setDraftNarrative}
            embedded
            patientId={patientId}
          />
        </div>
      </div>
      {showFindings && (
        <div style={{ marginBottom: "0.5rem" }}>
          <span className="meta" style={{ fontSize: "0.78rem" }}>
            {t("fieldFindings")}
          </span>
          <div style={{ marginTop: "0.2rem" }}>
            <EvidenceEditor
              value={draftFindings}
              onChange={setDraftFindings}
              embedded
              patientId={patientId}
            />
          </div>
        </div>
      )}
      {showRecs && (
        <div style={{ marginBottom: "0.5rem" }}>
          <span className="meta" style={{ fontSize: "0.78rem" }}>
            {t("fieldRecommendations")}
          </span>
          <div style={{ marginTop: "0.2rem" }}>
            <EvidenceEditor
              value={draftRecs}
              onChange={setDraftRecs}
              embedded
              patientId={patientId}
            />
          </div>
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem" }}>
        <button type="button" disabled={busy} onClick={onCancel}>
          {t("cancel")}
        </button>
        <button type="button" disabled={busy} onClick={() => void onSave()}>
          {busy ? t("saveBusy") : t("save")}
        </button>
      </div>
    </div>
  );
}
