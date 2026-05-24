"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useState } from "react";

import NativeDialog from "@/components/NativeDialog";

interface Props {
  open: boolean;
  initialName: string;
  initialDescription: string | null;
  /** Current ISO timestamp of the folder. Used to seed the date
   *  input so the user sees the existing value before editing. */
  initialCreatedAt?: string | null;
  busy?: boolean;
  err?: string | null;
  onSubmit: (patch: {
    name: string;
    description: string | null;
    createdAt: string | null;
  }) => void;
  onClose: () => void;
}

const DESCRIPTION_MAX = 500;

// "YYYY-MM-DD" extracted from an ISO timestamp; "" if missing.
// ``input[type=date]`` only accepts day-precision strings, so we
// strip the time portion at seed time and re-attach midnight UTC at
// submit time.
function toDateInput(iso: string | null | undefined): string {
  if (!iso) return "";
  return iso.length >= 10 ? iso.slice(0, 10) : "";
}

export default function EditFolderDialog({
  open,
  initialName,
  initialDescription,
  initialCreatedAt,
  busy,
  err,
  onSubmit,
  onClose,
}: Props) {
  const t = useTranslations("fascicolo.editFolder");
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription ?? "");
  const [createdDate, setCreatedDate] = useState(toDateInput(initialCreatedAt));
  // Snapshot of what the input was seeded with, to detect "user
  // didn't touch the field" and skip the patch (so we don't bump
  // updated_at on a no-op save).
  const [seedDate, setSeedDate] = useState(toDateInput(initialCreatedAt));

  useEffect(() => {
    if (open) {
      setName(initialName);
      setDescription(initialDescription ?? "");
      const seeded = toDateInput(initialCreatedAt);
      setCreatedDate(seeded);
      setSeedDate(seeded);
    }
  }, [open, initialName, initialDescription, initialCreatedAt]);

  if (!open) return null;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) return;
    const trimmedDesc = description.trim();
    // Convert the date-only input to a midnight-UTC ISO timestamp
    // so the backend stores a deterministic ``created_at``. Send
    // ``null`` (omit) when the user didn't change the value, to
    // avoid bumping updated_at for a no-op.
    const createdAt = createdDate && createdDate !== seedDate ? `${createdDate}T00:00:00Z` : null;
    onSubmit({
      name: trimmedName,
      description: trimmedDesc.length === 0 ? null : trimmedDesc,
      createdAt,
    });
  }

  return (
    <NativeDialog open={open} onClose={onClose} ariaLabel={t("title")} className="bv-dialog">
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
        <h2 style={{ marginTop: 0 }}>{t("title")}</h2>
        {err && <p className="error">{err}</p>}
        <form onSubmit={handleSubmit}>
          <label style={{ display: "block", marginBottom: "0.75rem" }}>
            <span className="meta">{t("fieldName")}</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={255}
              style={{ width: "100%" }}
            />
          </label>
          <label style={{ display: "block", marginBottom: "0.75rem" }}>
            <span className="meta">{t("fieldCreatedAt")}</span>
            <input
              type="date"
              value={createdDate}
              onChange={(e) => setCreatedDate(e.target.value)}
              style={{ width: "100%" }}
            />
          </label>
          <label style={{ display: "block", marginBottom: "0.5rem" }}>
            <span className="meta">{t("fieldDescription")}</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={4}
              maxLength={DESCRIPTION_MAX}
              style={{ width: "100%", resize: "vertical" }}
            />
            <div
              className="meta"
              style={{
                fontSize: "0.75rem",
                display: "flex",
                justifyContent: "space-between",
                marginTop: "0.2rem",
              }}
            >
              <span>{t("descriptionHint")}</span>
              <span>
                {description.length}/{DESCRIPTION_MAX}
              </span>
            </div>
          </label>
          <div
            style={{
              marginTop: "1rem",
              display: "flex",
              justifyContent: "flex-end",
              gap: "0.5rem",
            }}
          >
            <button type="button" className="ghost" onClick={onClose} disabled={busy}>
              {t("cancel")}
            </button>
            <button type="submit" disabled={busy || !name.trim()}>
              {busy ? t("saveBusy") : t("save")}
            </button>
          </div>
        </form>
      </div>
    </NativeDialog>
  );
}
