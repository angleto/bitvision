"use client";

// Dialog: attach an existing patient document to the study via
// ``DocumentStudyLink``. Companion of ``ReportUploadDialog`` — that
// one *creates* a new Report (v2 path), this one *links* a v3 Document
// already on the patient (a referto scanned weeks ago, a PDF that was
// uploaded in a different visit, etc.). The two are kept distinct so
// users don't accidentally double-upload the same paper twice.

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type PatientDocument,
  STUDY_DOCUMENT_LINK_KINDS,
  type StudyDocumentLinkKind,
  patientsApi,
  studyDocumentLinksApi,
} from "@/lib/api";

interface Props {
  patientId: string;
  studyId: string;
  open: boolean;
  onClose: () => void;
  onAttached: () => void;
}

/** Pull the structured ``detail`` out of an ApiError raised on a
 *  ``problem+json`` response. Returns ``null`` for non-structured
 *  errors (legacy string detail, network error, etc.). */
function structuredDetail(e: unknown): {
  type?: string;
  detail?: string;
  existing_document_id?: string;
} | null {
  if (!(e instanceof ApiError) || !e.detail || typeof e.detail !== "object") return null;
  const inner = (e.detail as { detail?: unknown }).detail;
  if (inner && typeof inner === "object") return inner as Record<string, string>;
  return null;
}

export default function AttachExistingReportDialog({
  patientId,
  studyId,
  open,
  onClose,
  onAttached,
}: Props) {
  const tA = useTranslations("actions");
  const t = useTranslations("attachReport");
  const [documents, setDocuments] = useState<PatientDocument[] | null>(null);
  const [filter, setFilter] = useState("");
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null);
  const [linkKind, setLinkKind] = useState<StudyDocumentLinkKind>("primary_report");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [errSlug, setErrSlug] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  // Fetch the patient's document list each time the dialog opens.
  // The list can be large; we render it client-side filtered so the
  // user can type "tac torace" and see matches without a server
  // round-trip per keystroke.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setDocuments(null);
    setSelectedDocId(null);
    setFilter("");
    setLinkKind("primary_report");
    setErr(null);
    setErrSlug(null);
    setSuccess(false);
    const run = async () => {
      try {
        const data = await patientsApi.listDocuments(patientId);
        if (!cancelled) setDocuments(data);
      } catch (e) {
        if (!cancelled) setErr(e instanceof ApiError ? e.message : t("loadFailed"));
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [open, patientId, t]);

  const filtered = useMemo(() => {
    if (!documents) return [];
    const needle = filter.trim().toLowerCase();
    if (!needle) return documents;
    return documents.filter((d) => {
      const hay = `${d.title ?? ""} ${d.document_type ?? ""} ${d.kind_id ?? ""} ${
        d.document_date ?? ""
      } ${(d.text ?? "").slice(0, 500)}`.toLowerCase();
      return hay.includes(needle);
    });
  }, [documents, filter]);

  if (!open) return null;

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!selectedDocId) return;
    setBusy(true);
    setErr(null);
    setErrSlug(null);
    setSuccess(false);
    try {
      await studyDocumentLinksApi.create(patientId, selectedDocId, studyId, linkKind);
      setSuccess(true);
      onAttached();
      setTimeout(() => onClose(), 700);
    } catch (e) {
      const sd = structuredDetail(e);
      const slug = sd?.type?.split("/").pop() ?? null;
      setErrSlug(slug);
      setErr(e instanceof ApiError ? e.message : t("attachFailed"));
    } finally {
      setBusy(false);
    }
  }

  // Shortcut: when the 409 conflict is about a duplicate primary,
  // offer a one-click "use addendum instead" button so the user
  // doesn't have to find the dropdown manually.
  const showDemoteShortcut =
    errSlug === "primary_report_already_set" && linkKind === "primary_report";

  return (
    <NativeDialog open={open} onClose={onClose} ariaLabel={t("title")} className="bv-dialog">
      <div
        style={{
          background: "var(--color-surface, #fff)",
          borderRadius: 8,
          padding: "1.25rem",
          maxWidth: 720,
          width: "calc(100% - 2rem)",
          maxHeight: "90vh",
          display: "flex",
          flexDirection: "column",
          boxShadow: "0 10px 30px rgba(0,0,0,0.2)",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{t("title")}</h2>
        <p className="meta" style={{ fontSize: "0.85rem" }}>
          {t("subtitle")}
        </p>
        {err && (
          <p className="error" style={{ marginTop: 0 }}>
            {err}
            {showDemoteShortcut && (
              <>
                {" · "}
                <button
                  type="button"
                  className="ghost"
                  onClick={() => {
                    setLinkKind("addendum");
                    setErr(null);
                    setErrSlug(null);
                  }}
                >
                  {t("useAddendumInstead")}
                </button>
              </>
            )}
          </p>
        )}
        {success && (
          <p className="meta" style={{ color: "#047857" }}>
            {t("attachedOk")}
          </p>
        )}
        <form
          onSubmit={handleSubmit}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.75rem",
            flex: 1,
            minHeight: 0,
          }}
        >
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder={t("filterPlaceholder")}
            aria-label={t("filterPlaceholder")}
            style={{ width: "100%" }}
          />
          <fieldset
            aria-label={t("documentListLabel")}
            style={{
              flex: 1,
              minHeight: 200,
              maxHeight: 360,
              overflowY: "auto",
              border: "1px solid var(--bv-card-border, #d0d5dd)",
              borderRadius: 6,
              padding: 0,
              margin: 0,
            }}
          >
            {!documents && (
              <p className="meta" style={{ padding: "0.75rem" }}>
                {t("loading")}
              </p>
            )}
            {documents && filtered.length === 0 && (
              <p className="meta" style={{ padding: "0.75rem" }}>
                {filter ? t("noMatches") : t("emptyList")}
              </p>
            )}
            {filtered.map((d) => {
              const checked = d.id === selectedDocId;
              return (
                <label
                  key={d.id}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "0.5rem",
                    padding: "0.5rem 0.75rem",
                    cursor: "pointer",
                    background: checked ? "var(--bv-accent-soft, #fff7ed)" : undefined,
                    borderBottom: "1px solid var(--bv-card-border, #eef0f3)",
                  }}
                >
                  <input
                    type="radio"
                    name="document"
                    value={d.id}
                    checked={checked}
                    onChange={() => setSelectedDocId(d.id)}
                    style={{ marginTop: 4 }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 500 }}>{d.title || t("untitled")}</div>
                    <div className="meta" style={{ fontSize: "0.78rem" }}>
                      <span className="badge">{d.kind_id || d.document_type || "document"}</span>
                      {d.document_date && (
                        <span style={{ marginLeft: "0.4rem" }}>{d.document_date}</span>
                      )}
                      {d.file_s3_key && (
                        <span className="badge" style={{ marginLeft: "0.4rem" }}>
                          {t("attachmentBadge")}
                        </span>
                      )}
                    </div>
                    {d.text && (
                      <div
                        className="meta"
                        style={{
                          fontSize: "0.78rem",
                          marginTop: "0.2rem",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                        }}
                      >
                        {d.text}
                      </div>
                    )}
                  </div>
                </label>
              );
            })}
          </fieldset>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <span className="meta">{t("linkKindLabel")}</span>
            <select
              value={linkKind}
              onChange={(e) => setLinkKind(e.target.value as StudyDocumentLinkKind)}
              style={{ maxWidth: 280 }}
            >
              {STUDY_DOCUMENT_LINK_KINDS.map((k) => (
                <option key={k} value={k}>
                  {t(`linkKind.${k}`)}
                </option>
              ))}
            </select>
            <span className="meta" style={{ fontSize: "0.75rem" }}>
              {t(`linkKindHelp.${linkKind}`)}
            </span>
          </label>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem" }}>
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {tA("cancel")}
            </button>
            <button type="submit" disabled={busy || !selectedDocId}>
              {busy ? t("attaching") : t("attach")}
            </button>
          </div>
        </form>
      </div>
    </NativeDialog>
  );
}
