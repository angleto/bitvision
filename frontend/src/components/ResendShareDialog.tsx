"use client";

// Confirmation + options dialog for "re-invia" on a share-link.
//
// User feedback: the bare "Re-invia" button in the shares table fired
// notifyShare immediately on click — no chance to add a custom note,
// no chance to bail if it was a stray click. Now it opens this small
// dialog: recipient (read-only, since notify uses link.recipient_email
// and the user cannot rewrite the recipient on an existing share),
// optional custom message, locale toggle, security reminder that the
// password is NOT included in the email.
//
// On submit it calls studiesApi.notifyShare with the same parameters
// the SendStudyDialog uses on the email-delivery branch, so the two
// surfaces produce identical emails — no behavioural drift between
// "send for the first time" and "re-send later".

import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import EmailDeliveryNotice from "@/components/EmailDeliveryNotice";
import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type ShareLink,
  type ShareNotifyResult,
  shareNotifyFailure,
  studiesApi,
} from "@/lib/api";

interface Props {
  link: ShareLink;
  open: boolean;
  onClose: () => void;
  /** Called once the backend reported a terminal delivery outcome
   *  (sent, queued or failed) so the parent table can refresh. Read
   *  ``info.status`` before claiming anything to the user: ``sent`` is
   *  the only outcome where the message actually left the pod. */
  onSent?: (info: ShareNotifyResult) => void;
}

export default function ResendShareDialog({ link, open, onClose, onSent }: Props) {
  const t = useTranslations("resendShare");
  const td = useTranslations("emailDelivery");
  const locale = useLocale();
  const [customMessage, setCustomMessage] = useState("");
  const [emailLocale, setEmailLocale] = useState<"it" | "en">(locale === "en" ? "en" : "it");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Real delivery outcome from the backend, not a "the request did not
  // throw" proxy for it. ``null`` until the first submit resolves.
  const [outcome, setOutcome] = useState<ShareNotifyResult | null>(null);

  useEffect(() => {
    if (!open) return;
    setCustomMessage("");
    setError(null);
    setOutcome(null);
  }, [open]);

  async function submit() {
    setBusy(true);
    setError(null);
    setOutcome(null);
    try {
      // HTTP 200 (sent) and HTTP 202 (queued for retry) both resolve
      // here — the status field discriminates them.
      const out = await studiesApi.notifyShare(link.id, customMessage.trim() || null, emailLocale);
      setOutcome(out);
      onSent?.(out);
    } catch (e) {
      // HTTP 502 (permanent SMTP failure) reaches this branch because
      // ``request`` throws on every non-2xx; the delivery envelope
      // survives on ApiError.detail and carries the error_code.
      const failure = shareNotifyFailure(e);
      if (failure) {
        setOutcome(failure);
        onSent?.(failure);
      } else {
        setError(e instanceof ApiError ? e.message : td("genericError"));
      }
    } finally {
      setBusy(false);
    }
  }

  const sent = outcome?.status === "sent";
  // The form stays mounted on a failed delivery so the Send button
  // doubles as "retry" once the operator fixed the mail server.
  const formVisible = !outcome || outcome.status === "failed";

  if (!link.recipient_email) {
    // Defensive: the parent only opens the dialog when the share has
    // a recipient on file. If somehow it doesn't, render an inline
    // error instead of an unusable form.
    return (
      <NativeDialog open={open} onClose={onClose} ariaLabel={t("title")}>
        <div className="bv-dialog-card">
          <h2 style={{ marginTop: 0 }}>{t("title")}</h2>
          <p className="error">{t("noRecipientEmail")}</p>
          <footer style={{ display: "flex", justifyContent: "flex-end", marginTop: "0.5rem" }}>
            <button type="button" onClick={onClose} className="ghost">
              {t("close")}
            </button>
          </footer>
        </div>
      </NativeDialog>
    );
  }

  return (
    <NativeDialog open={open} onClose={onClose} ariaLabel={t("title")}>
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, #111)",
          borderRadius: 8,
          minWidth: 460,
          maxWidth: 560,
          padding: "1.25rem",
          border: "1px solid var(--border, #ccc)",
          boxShadow: "0 12px 40px rgba(0,0,0,0.18)",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.05rem", marginBottom: "0.4rem" }}>{t("title")}</h2>
        <p style={{ marginTop: 0, fontSize: "0.85rem", opacity: 0.75 }}>{t("subtitle")}</p>

        {formVisible && (
          <>
            <div
              style={{
                background: "var(--bv-card-bg-soft, #f9fafb)",
                border: "1px solid var(--border, #e5e7eb)",
                borderRadius: 6,
                padding: "0.5rem 0.75rem",
                fontSize: "0.85rem",
                margin: "0.5rem 0 0.75rem",
              }}
            >
              <div>
                <strong>{t("recipientLabel")}</strong>{" "}
                {link.recipient_name ? `${link.recipient_name} ` : ""}
                <span style={{ opacity: 0.85 }}>&lt;{link.recipient_email}&gt;</span>
              </div>
              {link.label && (
                <div style={{ marginTop: "0.25rem", fontSize: "0.8rem", opacity: 0.85 }}>
                  <strong>{t("shareLabel")}</strong> {link.label}
                </div>
              )}
            </div>

            <label style={{ display: "block", fontSize: "0.85rem", marginBottom: "0.5rem" }}>
              <span style={{ display: "block", marginBottom: "0.25rem" }}>
                {t("customMessage")}
              </span>
              <textarea
                value={customMessage}
                onChange={(e) => setCustomMessage(e.target.value)}
                placeholder={t("customMessagePlaceholder")}
                rows={3}
                maxLength={1000}
                style={{
                  width: "100%",
                  padding: "0.4rem",
                  font: "inherit",
                  resize: "vertical",
                }}
              />
            </label>

            <fieldset
              style={{
                border: "1px solid var(--border, #ddd)",
                borderRadius: 6,
                padding: "0.4rem 0.75rem",
                margin: "0.5rem 0",
              }}
            >
              <legend style={{ fontSize: "0.8rem", padding: "0 0.4rem" }}>
                {t("emailLanguage")}
              </legend>
              <label
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.3rem",
                  marginRight: "1rem",
                  fontSize: "0.85rem",
                }}
              >
                <input
                  type="radio"
                  checked={emailLocale === "it"}
                  onChange={() => setEmailLocale("it")}
                />
                {t("langIt")}
              </label>
              <label
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "0.3rem",
                  fontSize: "0.85rem",
                }}
              >
                <input
                  type="radio"
                  checked={emailLocale === "en"}
                  onChange={() => setEmailLocale("en")}
                />
                {t("langEn")}
              </label>
            </fieldset>

            {link.requires_password && (
              <p
                style={{
                  fontSize: "0.78rem",
                  background: "var(--bv-warning-soft, #fef3c7)",
                  border: "1px solid var(--bv-warning, #b45309)",
                  borderRadius: 6,
                  padding: "0.4rem 0.6rem",
                  margin: "0.4rem 0",
                  color: "var(--bv-fg, #1f2937)",
                }}
              >
                {t("passwordSecurityNote")}
              </p>
            )}

            {error && (
              <p
                className="error"
                style={{ fontSize: "0.85rem", color: "var(--bv-danger, #b91c1c)" }}
              >
                {error}
              </p>
            )}
          </>
        )}

        {sent && outcome && (
          <p
            style={{
              fontSize: "0.9rem",
              color: "var(--bv-success, #047857)",
              background: "var(--bv-success-soft, #ecfdf5)",
              border: "1px solid var(--bv-success, #047857)",
              borderRadius: 6,
              padding: "0.5rem 0.75rem",
              margin: "0.5rem 0",
            }}
          >
            {t("sentTo", { to: outcome.to || link.recipient_email || "" })}
          </p>
        )}

        {/* queued (HTTP 202) and failed (HTTP 502): neutral / error
            panel, never the green one. */}
        {outcome && !sent && <EmailDeliveryNotice outcome={outcome} to={link.recipient_email} />}

        <footer
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.75rem",
          }}
        >
          <button type="button" onClick={onClose} disabled={busy} className="ghost">
            {outcome && !formVisible ? t("close") : t("cancel")}
          </button>
          {formVisible && (
            <button
              type="button"
              onClick={() => void submit()}
              disabled={busy}
              style={{
                padding: "0.4rem 0.9rem",
                background: "var(--bv-accent, #2563eb)",
                color: "#fff",
                border: "1px solid var(--bv-accent, #2563eb)",
                borderRadius: 4,
                cursor: busy ? "wait" : "pointer",
                fontWeight: 500,
              }}
            >
              {busy ? t("sending") : t("send")}
            </button>
          )}
        </footer>
      </div>
    </NativeDialog>
  );
}
