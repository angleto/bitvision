"use client";

// Shared "what actually happened to that email" panel.
//
// Until 2026-07-31 both share dialogs rendered a green "email inviata
// a X" the moment POST /share-links/{id}/notify came back 2xx — and
// the endpoint hard-coded sent=true, so a pod that could not reach the
// SMTP port at all still produced a green panel. Outbound mail was
// broken in production for hours without a single visible symptom.
//
// The backend now reports the real delivery outcome (sent / queued /
// failed + a discriminated error_code). This component renders the two
// NON-success outcomes so the two dialogs cannot drift apart again:
//
//   queued -> neutral panel: retriable SMTP failure, the dispatch
//             ledger will retry. Explicitly NOT green: nothing has
//             left the pod yet.
//   failed -> error panel with a human-readable reason derived from
//             error_code, plus the technical code for the operator.
//
// ``sent`` renders nothing: each dialog keeps its own success copy
// (different wording, different surrounding layout).

import { useTranslations } from "next-intl";

import { type ShareNotifyResult, shareNotifyErrorKey } from "@/lib/api";

interface Props {
  outcome: ShareNotifyResult;
  /** Recipient to name in the copy. Falls back to ``outcome.to``; the
   *  caller passes the address it knows (the 502 envelope may travel
   *  without it). */
  to?: string | null;
  /** When provided, a "retry send" button is rendered on the failed
   *  panel (the resend dialog keeps its own Send button instead). */
  onRetry?: () => void;
  retrying?: boolean;
}

export default function EmailDeliveryNotice({ outcome, to, onRetry, retrying }: Props) {
  const t = useTranslations("emailDelivery");
  if (outcome.status === "sent") return null;

  const addr = to?.trim() || outcome.to || "";
  const failed = outcome.status === "failed";

  return (
    <div
      role={failed ? "alert" : "status"}
      style={{
        border: `1px solid ${failed ? "var(--bv-danger, #b91c1c)" : "var(--border, #d1d5db)"}`,
        background: failed ? "var(--bv-danger-soft, #fef2f2)" : "var(--bv-card-bg-soft, #f3f4f6)",
        color: "var(--bv-fg, #1f2937)",
        borderRadius: 6,
        padding: "0.55rem 0.75rem",
        margin: "0.5rem 0",
        fontSize: "0.85rem",
      }}
    >
      <div
        style={{
          fontWeight: 600,
          marginBottom: "0.25rem",
          color: failed ? "var(--bv-danger, #b91c1c)" : "var(--bv-fg, #1f2937)",
        }}
      >
        {failed ? t("failedTitle") : t("queuedTitle")}
      </div>
      <p style={{ margin: 0 }}>
        {failed ? t("failedBody", { to: addr }) : t("queuedBody", { to: addr })}
      </p>
      {failed && (
        <p style={{ margin: "0.35rem 0 0" }}>
          <strong>{t("reasonLabel")}:</strong>{" "}
          {t(`reason.${shareNotifyErrorKey(outcome.error_code)}`)}
        </p>
      )}
      <p style={{ margin: "0.35rem 0 0", opacity: 0.8 }}>
        {failed ? t("failedHint") : t("queuedHint")}
      </p>
      {(outcome.error_code || outcome.delivery_id) && (
        <p style={{ margin: "0.35rem 0 0", fontSize: "0.75rem", opacity: 0.7 }}>
          {outcome.error_code ? t("codeLabel", { code: outcome.error_code }) : ""}
          {outcome.error_code && outcome.delivery_id ? " · " : ""}
          {outcome.delivery_id ? t("deliveryIdLabel", { id: outcome.delivery_id }) : ""}
        </p>
      )}
      {failed && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          disabled={retrying}
          style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}
        >
          {retrying ? t("retrying") : t("retry")}
        </button>
      )}
    </div>
  );
}
