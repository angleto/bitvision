"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { ApiError, reportsApi } from "@/lib/api";

// Accepted MIME types for the optional attachment: PDF, DOCX, common images.
const ACCEPTED_MIME = new Set<string>([
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/tiff",
]);
const ACCEPT_ATTR =
  ".pdf,.doc,.docx,.png,.jpg,.jpeg,.webp,.gif,.tif,.tiff,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/*";

interface Props {
  studyId: string;
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}

export default function ReportUploadDialog({ studyId, open, onClose, onCreated }: Props) {
  const tA = useTranslations("actions");
  const tR = useTranslations("reportUpload");
  const [text, setText] = useState("");
  const [versionNote, setVersionNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileErr, setFileErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  if (!open) return null;

  function resetForm() {
    setText("");
    setVersionNote("");
    setFile(null);
    setFileErr(null);
    setErr(null);
    setSuccess(false);
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    if (!f) {
      setFile(null);
      setFileErr(null);
      return;
    }
    // Some browsers don't fill the MIME type for .docx — fall back to extension.
    const ext = f.name.toLowerCase().split(".").pop() ?? "";
    const extOk = [
      "pdf",
      "doc",
      "docx",
      "png",
      "jpg",
      "jpeg",
      "webp",
      "gif",
      "tif",
      "tiff",
    ].includes(ext);
    if (!ACCEPTED_MIME.has(f.type) && !extOk) {
      setFile(null);
      setFileErr(tR("fileTypeUnsupported", { type: f.type || ext }));
      return;
    }
    setFile(f);
    setFileErr(null);
  }

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!text.trim()) {
      setErr(tR("textRequired"));
      return;
    }
    setBusy(true);
    setErr(null);
    setSuccess(false);
    try {
      const fd = new FormData();
      // Prepend optional version note as a header line to the text body,
      // since the backend schema does not yet have a dedicated field.
      const body = versionNote.trim() ? `[${versionNote.trim()}]\n${text}` : text;
      fd.set("text", body);
      if (file) fd.set("file", file);
      await reportsApi.create(studyId, fd);
      setSuccess(true);
      onCreated();
      // Close the dialog after a short delay so the user sees the confirmation.
      setTimeout(() => {
        resetForm();
        onClose();
      }, 700);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : tR("uploadFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <NativeDialog open={open} onClose={onClose} ariaLabel={tR("title")} className="bv-dialog">
      <div
        style={{
          background: "var(--color-surface, #fff)",
          borderRadius: 8,
          padding: "1.25rem",
          maxWidth: 540,
          width: "calc(100% - 2rem)",
          maxHeight: "90vh",
          overflow: "auto",
          boxShadow: "0 10px 30px rgba(0,0,0,0.2)",
        }}
      >
        <h2 style={{ marginTop: 0 }}>{tR("title")}</h2>
        {err && <p className="error">{err}</p>}
        {success && (
          <p className="meta" style={{ color: "#047857" }}>
            {tR("uploadedOk")}
          </p>
        )}
        <form onSubmit={handleSubmit}>
          <label style={{ display: "block", marginBottom: "0.5rem" }}>
            <span className="meta">{tR("textLabel")}</span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              required
              rows={8}
              style={{ width: "100%" }}
              placeholder={tR("textPlaceholder")}
            />
          </label>
          <label style={{ display: "block", marginBottom: "0.5rem" }}>
            <span className="meta">{tR("versionNoteLabel")}</span>
            <input
              value={versionNote}
              onChange={(e) => setVersionNote(e.target.value)}
              placeholder={tR("versionNotePlaceholder")}
              style={{ width: "100%" }}
            />
          </label>
          <label style={{ display: "block", marginBottom: "0.5rem" }}>
            <span className="meta">{tR("attachmentLabel")}</span>
            <input type="file" accept={ACCEPT_ATTR} onChange={handleFileChange} />
            {file && (
              <div className="meta" style={{ fontSize: "0.8rem", marginTop: "0.2rem" }}>
                {file.name} ({Math.round(file.size / 1024)} KB)
              </div>
            )}
            {fileErr && <p className="error">{fileErr}</p>}
          </label>
          <div
            style={{
              marginTop: "1rem",
              display: "flex",
              justifyContent: "flex-end",
              gap: "0.5rem",
            }}
          >
            <button
              type="button"
              className="ghost"
              onClick={() => {
                resetForm();
                onClose();
              }}
              disabled={busy}
            >
              {tA("cancel")}
            </button>
            <button type="submit" disabled={busy || !text.trim()}>
              {busy ? tR("uploadingButton") : tR("submitButton")}
            </button>
          </div>
        </form>
      </div>
    </NativeDialog>
  );
}
