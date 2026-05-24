"use client";

// Real-binary attachments manager for a ClinicalEvent.
//
// Three call sites:
//   - PlanEventDialog: ``eventId`` is null (event not created yet); we
//     accumulate File objects in ``pending`` and the parent uploads
//     them after create.
//   - EditEventDialog: ``eventId`` is set; pending files upload
//     immediately on submit, and the existing list is editable
//     (delete / promote).
//   - EventDrawer (read-only mode): ``readOnly`` true; only the list +
//     download + promote actions are visible.
//
// Why not URL-list: an event attachment is usually a referral letter,
// prescription, prep sheet, anonymous photo. The user wants drag &
// drop or file-picker, not "paste a Drive URL". URL references stay
// on the ``links`` field for portals / external services.

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ApiError, authedDownload } from "@/lib/api";
import type { ClinicalEventAttachment } from "@/lib/api_records";
import { calendarApi } from "@/lib/calendar_api";

interface Props {
  // When null the manager is in "pending only" mode (create flow).
  eventId: string | null;
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
  pending,
  onPendingChange,
  readOnly = false,
}: Props) {
  const t = useTranslations("eventActions");
  const [existing, setExisting] = useState<ClinicalEventAttachment[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyAtt, setBusyAtt] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!eventId) {
      setExisting([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    calendarApi
      .listAttachments(eventId)
      .then((rows) => {
        if (!cancelled) setExisting(rows);
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
      // Upload immediately when the event exists.
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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {!readOnly && (
        <div>
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
              {a.promoted_to_document_id ? (
                <span style={{ ...meta, color: "var(--bv-status-confirmed-border, #1e8e3e)" }}>
                  ✓ {t("attachmentPromoted")}
                </span>
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
