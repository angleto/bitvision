"use client";

// "Documenti collegati" panel for the study detail page. Lists every
// document attached to the study via ``DocumentStudyLink`` (forward
// direction) and lets a writer attach a new one or detach an existing
// link. Distinct from the v2 ``ReportsList`` which renders the legacy
// ``Report`` rows; the two surfaces are kept side-by-side until the
// v2 path is migrated away.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import AttachExistingReportDialog from "@/components/AttachExistingReportDialog";
import { useModal } from "@/components/ModalHost";
import { ApiError, type StudyDocumentLink, studyDocumentLinksApi } from "@/lib/api";

interface Props {
  patientId: string;
  studyId: string;
  canWrite: boolean;
  /** When the attach button lives in the parent (so it sits next to
   *  "Add report" in the Reports heading) the parent owns the dialog
   *  open state and passes it down. When omitted, the component
   *  falls back to a self-owned button + dialog. */
  externalDialogOpen?: boolean;
  onExternalDialogClose?: () => void;
}

export default function StudyAttachedDocuments({
  patientId,
  studyId,
  canWrite,
  externalDialogOpen,
  onExternalDialogClose,
}: Props) {
  const t = useTranslations("studyAttachedDocuments");
  const tLink = useTranslations("attachReport");
  const modal = useModal();
  const [links, setLinks] = useState<StudyDocumentLink[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [internalDialogOpen, setInternalDialogOpen] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);

  const externallyControlled = externalDialogOpen !== undefined;
  const dialogOpen = externallyControlled ? !!externalDialogOpen : internalDialogOpen;
  const closeDialog = () =>
    externallyControlled ? onExternalDialogClose?.() : setInternalDialogOpen(false);

  const refresh = useCallback(async () => {
    try {
      const data = await studyDocumentLinksApi.list(patientId, studyId);
      setLinks(data);
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [patientId, studyId, t]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleRemove(link: StudyDocumentLink) {
    const ok = await modal.confirm({
      message: t("removeConfirm", { title: link.document_title }),
      destructive: true,
    });
    if (!ok) return;
    setRemovingId(`${link.document_id}:${link.link_kind}`);
    try {
      await studyDocumentLinksApi.remove(patientId, link.document_id, studyId, link.link_kind);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("removeFailed"));
    } finally {
      setRemovingId(null);
    }
  }

  return (
    <section style={{ marginTop: "2rem" }}>
      <h2 style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
        {t("heading")}
        {canWrite && !externallyControlled && (
          <button
            type="button"
            className="ghost"
            style={{ fontSize: "0.85rem" }}
            onClick={() => setInternalDialogOpen(true)}
          >
            {t("attachExistingButton")}
          </button>
        )}
      </h2>
      {err && <p className="error">{err}</p>}
      {!links && !err && <p className="meta">{t("loading")}</p>}
      {links && links.length === 0 && <p className="meta">{t("empty")}</p>}
      {links && links.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {links.map((l) => (
            <li
              key={`${l.document_id}:${l.link_kind}`}
              className="card"
              style={{ marginBottom: "0.5rem" }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  justifyContent: "space-between",
                  gap: "0.75rem",
                  flexWrap: "wrap",
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
                    <span className="badge" title={tLink(`linkKindHelp.${l.link_kind}` as never)}>
                      {tLink(`linkKind.${l.link_kind}` as never)}
                    </span>
                    {l.has_attachment && <span className="badge">{tLink("attachmentBadge")}</span>}
                  </div>
                  <h3 style={{ margin: "0.3rem 0 0.1rem", fontSize: "0.95rem" }}>
                    <Link
                      href={`/patients/${patientId}/documents/${l.document_id}`}
                      style={{ color: "inherit" }}
                    >
                      {l.document_title}
                    </Link>
                  </h3>
                  <div className="meta" style={{ fontSize: "0.78rem" }}>
                    <span className="badge">{l.document_kind}</span>
                    {l.document_date && (
                      <span style={{ marginLeft: "0.4rem" }}>{l.document_date}</span>
                    )}
                  </div>
                  {l.document_text_preview && (
                    <p
                      className="meta"
                      style={{
                        marginTop: "0.4rem",
                        fontSize: "0.82rem",
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {l.document_text_preview}
                    </p>
                  )}
                </div>
                {canWrite && (
                  <button
                    type="button"
                    className="ghost"
                    style={{ fontSize: "0.78rem" }}
                    disabled={removingId === `${l.document_id}:${l.link_kind}`}
                    onClick={() => handleRemove(l)}
                    title={t("removeTitle")}
                  >
                    {removingId === `${l.document_id}:${l.link_kind}` ? t("removing") : t("remove")}
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {canWrite && (
        <AttachExistingReportDialog
          patientId={patientId}
          studyId={studyId}
          open={dialogOpen}
          onClose={closeDialog}
          onAttached={refresh}
        />
      )}
    </section>
  );
}
