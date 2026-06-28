"use client";

// Real-binary attachments manager for a ClinicalEvent.
//
// Two ways to attach:
//   - from the PC: a raw file (a receipt, a prep sheet, a photo). On
//     upload the backend computes its content hash and auto-reconciles
//     it against the patient Drive — if the exact bytes are already a
//     curated document, the row shows "Open in Drive" instead of being
//     an isolated blob.
//   - from the Drive ("Allega dal Drive"): pick an already-curated
//     document (the referto you uploaded earlier) and reference it on
//     the event without re-uploading. No second copy.
//
// Three call sites:
//   - PlanEventDialog: ``eventId`` is null (event not created yet); we
//     accumulate File objects in ``pending`` and the parent uploads
//     them after create. "Attach from Drive" needs a persisted event,
//     so it is hidden here.
//   - EditEventDialog / EventDrawer: ``eventId`` + ``patientId`` set;
//     uploads, promote, attach-from-Drive and unlink are all live.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { ApiError, type PatientDocument, authedDownload, patientsApi } from "@/lib/api";
import type { ClinicalEventAttachment, EventDocument } from "@/lib/api_records";
import { calendarApi } from "@/lib/calendar_api";

interface Props {
  // When null the manager is in "pending only" mode (create flow).
  eventId: string | null;
  // Patient owning the event — required to enable "attach from Drive".
  patientId?: string;
  // Local file queue for the create flow. Parent owns the state so it
  // can upload after the event has been created.
  pending?: File[];
  onPendingChange?: (files: File[]) => void;
  readOnly?: boolean;
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

export default function AttachmentsManager({
  eventId,
  patientId,
  pending,
  onPendingChange,
  readOnly = false,
}: Props) {
  const t = useTranslations("eventActions");
  const [existing, setExisting] = useState<ClinicalEventAttachment[]>([]);
  const [linkedDocs, setLinkedDocs] = useState<EventDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyAtt, setBusyAtt] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Drive picker state.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerDocs, setPickerDocs] = useState<PatientDocument[] | null>(null);
  const [pickerLoading, setPickerLoading] = useState(false);
  const [pickerFilter, setPickerFilter] = useState("");

  useEffect(() => {
    if (!eventId) {
      setExisting([]);
      setLinkedDocs([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    Promise.all([calendarApi.listAttachments(eventId), calendarApi.listEventDocuments(eventId)])
      .then(([atts, docs]) => {
        if (cancelled) return;
        setExisting(atts);
        setLinkedDocs(docs);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "load failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [eventId]);

  function handlePicked(files: FileList | null): void {
    if (!files || files.length === 0) return;
    const list = Array.from(files);
    if (eventId && !readOnly) {
      void uploadNow(list);
    } else if (onPendingChange) {
      onPendingChange([...(pending ?? []), ...list]);
    }
  }

  async function uploadNow(files: File[]): Promise<void> {
    if (!eventId) return;
    setError(null);
    for (const f of files) {
      try {
        const row = await calendarApi.uploadAttachment(eventId, f);
        setExisting((cur) => [row, ...cur]);
      } catch (e) {
        setError(
          e instanceof ApiError
            ? `${e.status}: ${String(e.detail)}`
            : e instanceof Error
              ? e.message
              : "upload failed",
        );
        return;
      }
    }
  }

  function removePending(idx: number): void {
    if (!onPendingChange || !pending) return;
    onPendingChange(pending.filter((_, j) => j !== idx));
  }

  async function deleteExisting(att: ClinicalEventAttachment): Promise<void> {
    if (!eventId) return;
    setBusyAtt(att.id);
    try {
      await calendarApi.deleteAttachment(eventId, att.id);
      setExisting((cur) => cur.filter((x) => x.id !== att.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "delete failed");
    } finally {
      setBusyAtt(null);
    }
  }

  async function promote(att: ClinicalEventAttachment): Promise<void> {
    if (!eventId) return;
    setBusyAtt(att.id);
    try {
      const updated = await calendarApi.promoteAttachment(eventId, att.id);
      setExisting((cur) => cur.map((x) => (x.id === att.id ? updated : x)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "promote failed");
    } finally {
      setBusyAtt(null);
    }
  }

  async function openPicker(): Promise<void> {
    setPickerOpen(true);
    if (pickerDocs !== null || !patientId) return;
    setPickerLoading(true);
    try {
      const docs = await patientsApi.listDocuments(patientId);
      setPickerDocs(docs);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load documents failed");
    } finally {
      setPickerLoading(false);
    }
  }

  async function linkFromDrive(doc: PatientDocument): Promise<void> {
    if (!eventId) return;
    setBusyAtt(doc.id);
    try {
      const link = await calendarApi.linkEventDocument(eventId, doc.id);
      // Idempotent backend: drop any existing row for the same document
      // before prepending so a double-click doesn't duplicate the row.
      setLinkedDocs((cur) => [link, ...cur.filter((x) => x.document_id !== link.document_id)]);
      setPickerOpen(false);
      setPickerFilter("");
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.status}: ${String(e.detail)}`
          : e instanceof Error
            ? e.message
            : "link failed",
      );
    } finally {
      setBusyAtt(null);
    }
  }

  async function unlinkDoc(link: EventDocument): Promise<void> {
    if (!eventId) return;
    setBusyAtt(link.id);
    try {
      await calendarApi.unlinkEventDocument(eventId, link.id);
      setLinkedDocs((cur) => cur.filter((x) => x.id !== link.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "unlink failed");
    } finally {
      setBusyAtt(null);
    }
  }

  // Pure "attach from Drive" references; reconciled raw uploads already
  // appear in the attachments list (with an "Open in Drive" link), so
  // exclude them here to avoid showing the same document twice.
  const referenceDocs = linkedDocs.filter((d) => d.source_attachment_id === null);
  const filteredPicker = (pickerDocs ?? []).filter((d) =>
    (d.title || "").toLowerCase().includes(pickerFilter.trim().toLowerCase()),
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {!readOnly && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            onChange={(e) => handlePicked(e.target.files)}
            style={{ display: "none" }}
          />
          <button
            type="button"
            className="ghost"
            onClick={() => fileInputRef.current?.click()}
            style={{ fontSize: "0.82rem", padding: "0.3rem 0.7rem" }}
          >
            📎 {t("addAttachmentFile")}
          </button>
          {eventId && patientId && (
            <button
              type="button"
              className="ghost"
              onClick={() => void openPicker()}
              style={{ fontSize: "0.82rem", padding: "0.3rem 0.7rem" }}
            >
              🗂️ {t("addFromDrive")}
            </button>
          )}
        </div>
      )}

      {pickerOpen && (
        <div style={pickerStyle}>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="text"
              value={pickerFilter}
              onChange={(e) => setPickerFilter(e.target.value)}
              placeholder={t("pickerSearchPlaceholder")}
              style={pickerSearch}
            />
            <button
              type="button"
              className="ghost"
              onClick={() => setPickerOpen(false)}
              aria-label={t("pickerClose")}
              style={iconBtn}
            >
              ✕
            </button>
          </div>
          {pickerLoading && (
            <p style={{ color: "var(--bv-fg-soft)", fontSize: "0.78rem", margin: 0 }}>…</p>
          )}
          {!pickerLoading && filteredPicker.length === 0 && (
            <p style={{ color: "var(--bv-fg-soft)", fontSize: "0.78rem", margin: 0 }}>
              {t("pickerEmpty")}
            </p>
          )}
          {!pickerLoading && filteredPicker.length > 0 && (
            <ul style={{ ...listReset, maxHeight: 220, overflowY: "auto" }}>
              {filteredPicker.map((d) => (
                <li key={d.id} style={rowStyle}>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => void linkFromDrive(d)}
                    disabled={busyAtt === d.id}
                    style={{
                      flex: 1,
                      textAlign: "left",
                      background: "none",
                      border: "none",
                      padding: 0,
                      cursor: "pointer",
                      color: "var(--bv-fg)",
                      fontSize: "0.82rem",
                    }}
                  >
                    📄 {d.title || t("untitledDocument")}
                  </button>
                  {d.document_date && <span style={meta}>{d.document_date}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {loading && <p style={{ color: "var(--bv-fg-soft)", fontSize: "0.78rem" }}>…</p>}
      {error && <p style={{ color: "var(--bv-danger, #c00)", fontSize: "0.78rem" }}>{error}</p>}

      {pending && pending.length > 0 && (
        <ul style={listReset}>
          {pending.map((f, i) => (
            <li key={`${f.name}-${i}-${f.lastModified}`} style={rowStyle}>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
                📎 {f.name}
              </span>
              <span style={meta}>{fmtSize(f.size)}</span>
              <span style={{ ...meta, color: "var(--bv-accent, #4f46e5)" }}>
                {t("attachmentPending")}
              </span>
              {!readOnly && (
                <button
                  type="button"
                  className="ghost"
                  onClick={() => removePending(i)}
                  aria-label={t("removeAttachment")}
                  style={iconBtn}
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {existing.length > 0 && (
        <ul style={listReset}>
          {existing.map((a) => (
            <li key={a.id} style={rowStyle}>
              <button
                type="button"
                className="ghost"
                onClick={() =>
                  authedDownload(
                    calendarApi.attachmentDownloadUrl(a.event_id, a.id),
                    a.filename,
                  ).catch((e: unknown) => {
                    setError(
                      e instanceof ApiError
                        ? `${e.status}: ${String(e.detail)}`
                        : e instanceof Error
                          ? e.message
                          : "download failed",
                    );
                  })
                }
                style={{
                  flex: 1,
                  textAlign: "left",
                  color: "var(--bv-fg)",
                  textDecoration: "none",
                  background: "none",
                  border: "none",
                  padding: 0,
                  cursor: "pointer",
                  fontSize: "0.82rem",
                }}
              >
                📎 {a.filename}
              </button>
              <span style={meta}>{fmtSize(a.size_bytes)}</span>
              {a.document_id ? (
                <Link
                  href={`/patients/${a.patient_id}/documents/${a.document_id}`}
                  style={driveLink}
                >
                  ↗ {t("attachmentInDrive")}
                </Link>
              ) : (
                !readOnly && (
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => promote(a)}
                    disabled={busyAtt === a.id}
                    title={t("promoteToDocumentTip")}
                    style={smallBtn}
                  >
                    ↗ {t("promoteToDocument")}
                  </button>
                )
              )}
              {!readOnly && (
                <button
                  type="button"
                  className="ghost"
                  onClick={() => deleteExisting(a)}
                  disabled={busyAtt === a.id}
                  aria-label={t("removeAttachment")}
                  style={iconBtn}
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {referenceDocs.length > 0 && (
        <>
          <span style={{ ...meta, marginTop: 2 }}>{t("linkedDocsLabel")}</span>
          <ul style={listReset}>
            {referenceDocs.map((d) => (
              <li key={d.id} style={rowStyle}>
                <Link
                  href={`/patients/${d.patient_id}/documents/${d.document_id}`}
                  style={{
                    flex: 1,
                    textAlign: "left",
                    color: "var(--bv-fg)",
                    textDecoration: "none",
                    fontSize: "0.82rem",
                  }}
                >
                  🗂️ {d.document_title || t("untitledDocument")}
                </Link>
                {d.document_date && <span style={meta}>{d.document_date}</span>}
                {!readOnly && (
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => void unlinkDoc(d)}
                    disabled={busyAtt === d.id}
                    aria-label={t("unlinkDocument")}
                    title={t("unlinkDocument")}
                    style={iconBtn}
                  >
                    ✕
                  </button>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

const listReset = {
  listStyle: "none",
  padding: 0,
  margin: 0,
  display: "flex",
  flexDirection: "column",
  gap: 4,
} as const;

const rowStyle = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "4px 6px",
  border: "1px solid var(--bv-card-border, #e5e7eb)",
  borderRadius: 6,
  fontSize: "0.82rem",
  background: "var(--bv-card-bg-soft, #f9fafb)",
} as const;

const meta = {
  color: "var(--bv-fg-soft)",
  fontSize: "0.72rem",
  fontVariantNumeric: "tabular-nums" as const,
};

const smallBtn = {
  fontSize: "0.7rem",
  padding: "0.15rem 0.45rem",
  borderRadius: 6,
};

const iconBtn = {
  width: 24,
  height: 24,
  fontSize: "0.85rem",
  padding: 0,
  borderRadius: 6,
};

const driveLink = {
  fontSize: "0.7rem",
  padding: "0.15rem 0.45rem",
  borderRadius: 6,
  color: "var(--bv-status-confirmed-border, #1e8e3e)",
  textDecoration: "none",
  whiteSpace: "nowrap" as const,
};

const pickerStyle = {
  display: "flex",
  flexDirection: "column" as const,
  gap: 6,
  padding: 8,
  border: "1px solid var(--bv-card-border, #e5e7eb)",
  borderRadius: 8,
  background: "var(--bv-card-bg, #fff)",
};

const pickerSearch = {
  flex: 1,
  padding: "0.3rem 0.5rem",
  fontSize: "0.82rem",
  borderRadius: 6,
  border: "1px solid var(--bv-card-border, #e5e7eb)",
  background: "var(--bv-input-bg, #fff)",
  color: "var(--bv-fg)",
};
