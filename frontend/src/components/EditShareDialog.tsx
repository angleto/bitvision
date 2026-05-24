"use client";

// Inline editor for an existing share-link.
//
// Surfaces the three properties the grantor most often wants to
// change after the fact: the human-readable label (so a bare URL in
// the management table becomes "Consulto Bianchi 2026-05"), the
// expiry window (push out the deadline without revoking + re-issuing
// — the recipient's URL keeps working), and the password (rotate it,
// add one to a previously-public link, or strip it entirely).
//
// "Strip password" is the case where we deliberately use the
// distinction the backend exposes: PATCH with ``password = ""``
// clears the password, ``password = null`` (or unset) leaves it
// untouched. We surface that as an explicit "Rimuovi password"
// checkbox so the user never has to type ``""`` to mean "no
// password".

import { useTranslations } from "next-intl";
import { useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { ApiError, type ShareLink, patientsApi } from "@/lib/api";

interface Props {
  link: ShareLink;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}

type ExpiryChoice = "keep" | "week" | "month1" | "month3" | "month6" | "year";

const EXPIRY_HOURS: Record<Exclude<ExpiryChoice, "keep">, number> = {
  week: 24 * 7,
  month1: 24 * 30,
  month3: 24 * 90,
  month6: 24 * 180,
  year: 24 * 365,
};

export default function EditShareDialog({ link, open, onClose, onSaved }: Props) {
  const t = useTranslations("editShareDialog");
  const [label, setLabel] = useState(link.label ?? "");
  const [expiry, setExpiry] = useState<ExpiryChoice>("keep");
  const [pwMode, setPwMode] = useState<"keep" | "set" | "clear">("keep");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const patch: Parameters<typeof patientsApi.updateShare>[1] = {};
      if (label.trim() !== (link.label ?? "")) {
        patch.label = label.trim() || null;
      }
      if (expiry !== "keep") {
        patch.expires_in_hours = EXPIRY_HOURS[expiry];
      }
      if (pwMode === "set" && newPassword) {
        patch.password = newPassword;
      } else if (pwMode === "clear") {
        // Empty string is the explicit "remove password" sentinel
        // expected by the PATCH endpoint (null = leave alone).
        patch.password = "";
      }
      if (Object.keys(patch).length === 0) {
        onClose();
        return;
      }
      await patientsApi.updateShare(link.id, patch);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "update failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <NativeDialog open={open} onClose={onClose} ariaLabel={t("title")}>
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, #111)",
          borderRadius: 8,
          minWidth: 420,
          maxWidth: 520,
          padding: "1.25rem",
          border: "1px solid var(--border, #ccc)",
          boxShadow: "0 12px 40px rgba(0,0,0,0.18)",
        }}
      >
        <h2 style={{ marginTop: 0, marginBottom: "0.5rem", fontSize: "1.05rem" }}>{t("title")}</h2>
        <p style={{ marginTop: 0, fontSize: "0.85rem", opacity: 0.75 }}>{t("subtitle")}</p>

        <label style={{ display: "block", fontSize: "0.85rem", margin: "0.75rem 0 0.5rem" }}>
          <span style={{ display: "block", marginBottom: "0.25rem" }}>{t("labelField")}</span>
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            maxLength={120}
            placeholder={t("labelPlaceholder")}
            style={{ width: "100%", padding: "0.4rem" }}
          />
        </label>

        <fieldset
          style={{
            margin: "0.75rem 0",
            padding: "0.5rem 0.75rem",
            border: "1px solid var(--border, #ddd)",
            borderRadius: 6,
          }}
        >
          <legend style={{ fontSize: "0.85rem", padding: "0 0.4rem" }}>{t("expirySection")}</legend>
          {(["keep", "week", "month1", "month3", "month6", "year"] as ExpiryChoice[]).map((e) => (
            <label
              key={e}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
                fontSize: "0.85rem",
                margin: "0.2rem 0",
              }}
            >
              <input
                type="radio"
                checked={expiry === e}
                onChange={() => setExpiry(e)}
                name="expiry"
              />
              <span>{t(`expiry.${e}`)}</span>
            </label>
          ))}
        </fieldset>

        <fieldset
          style={{
            margin: "0.75rem 0",
            padding: "0.5rem 0.75rem",
            border: "1px solid var(--border, #ddd)",
            borderRadius: 6,
          }}
        >
          <legend style={{ fontSize: "0.85rem", padding: "0 0.4rem" }}>
            {t("passwordSection")}
          </legend>
          <p style={{ fontSize: "0.78rem", opacity: 0.7, margin: "0 0 0.4rem" }}>
            {link.requires_password ? t("currentlyProtected") : t("currentlyOpen")}
          </p>
          {(["keep", "set", "clear"] as const).map((m) => (
            <label
              key={m}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
                fontSize: "0.85rem",
                margin: "0.2rem 0",
              }}
            >
              <input
                type="radio"
                checked={pwMode === m}
                onChange={() => setPwMode(m)}
                name="pwmode"
              />
              <span>{t(`pwMode.${m}`)}</span>
            </label>
          ))}
          {pwMode === "set" && (
            <input
              type="text"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={t("newPasswordPlaceholder")}
              style={{ width: "100%", marginTop: "0.4rem", padding: "0.4rem" }}
            />
          )}
        </fieldset>

        {error && (
          <p className="error" style={{ fontSize: "0.85rem", color: "var(--bv-danger, #b91c1c)" }}>
            {error}
          </p>
        )}

        <footer
          style={{
            display: "flex",
            gap: "0.5rem",
            justifyContent: "flex-end",
            marginTop: "0.75rem",
          }}
        >
          <button type="button" onClick={onClose} disabled={busy} className="ghost">
            {t("cancel")}
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={busy || (pwMode === "set" && !newPassword)}
            className="primary"
          >
            {busy ? t("saving") : t("save")}
          </button>
        </footer>
      </div>
    </NativeDialog>
  );
}
