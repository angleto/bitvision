"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type ContactDelegateResponse,
  type Patient,
  type PatientContact,
  patientsApi,
} from "@/lib/api";

interface Props {
  patient: Patient;
  isOwner: boolean;
  /** Called after a successful promote / revoke so the parent can
   *  refresh the patient row and pick up the new ``delegation_*``
   *  fields on the contact. */
  onChanged: () => void;
}

/**
 * Fascicolo header section listing every contact attached to the
 * patient (family members, caregivers, GP, ...) with the option to
 * promote any of them to a real delegate of the fascicolo.
 *
 * The data model intentionally folds "informational contact" and
 * "delegated user" into the same row — `delegation_share_link_id` on
 * the JSONB entry is the discriminant. This keeps the UI as a single
 * list of "people connected to this fascicolo" rather than two
 * disjoint sections (the older "Other contacts" + "Shares" split).
 */
export default function PatientContactsPanel({ patient, isOwner, onChanged }: Props) {
  const t = useTranslations("patient");
  const [openDialogFor, setOpenDialogFor] = useState<string | null>(null);
  const [openChannelsFor, setOpenChannelsFor] = useState<string | null>(null);
  const [pendingResult, setPendingResult] = useState<ContactDelegateResponse | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  if (!patient.contacts || patient.contacts.length === 0) return null;

  const handleRevoke = async (contactId: string) => {
    setActionErr(null);
    try {
      await patientsApi.revokeContactDelegation(patient.id, contactId);
      onChanged();
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : t("delegation.errorGeneric"));
    }
  };

  return (
    <div style={{ marginTop: "0.75rem" }}>
      <p className="meta" style={{ marginBottom: "0.35rem", fontSize: "0.78rem" }}>
        {t("otherContacts")}
      </p>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.5rem",
        }}
      >
        {patient.contacts.map((c) => (
          <ContactChip
            key={c.id ?? `${c.label}-${c.email ?? ""}`}
            contact={c}
            isOwner={isOwner}
            onPromote={() => c.id && setOpenDialogFor(c.id)}
            onRevoke={() => c.id && handleRevoke(c.id)}
            onOpenChannels={() => c.id && setOpenChannelsFor(c.id)}
          />
        ))}
      </div>
      {actionErr && (
        <p style={{ color: "var(--bv-error, #cf6e6e)", fontSize: "0.8rem", marginTop: "0.4rem" }}>
          {actionErr}
        </p>
      )}
      {openDialogFor && (
        <DelegateContactDialog
          patientId={patient.id}
          contact={patient.contacts.find((c) => c.id === openDialogFor) ?? null}
          onClose={() => setOpenDialogFor(null)}
          onSuccess={(result) => {
            setOpenDialogFor(null);
            setPendingResult(result);
            onChanged();
          }}
        />
      )}
      {pendingResult && (
        <DelegationResultBanner result={pendingResult} onClose={() => setPendingResult(null)} />
      )}
      {openChannelsFor && (
        <ContactChannelsDialog
          patientId={patient.id}
          contact={patient.contacts.find((c) => c.id === openChannelsFor) ?? null}
          onClose={() => setOpenChannelsFor(null)}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}

// --- Per-contact chip --------------------------------------------------

function ContactChip({
  contact,
  isOwner,
  onPromote,
  onRevoke,
  onOpenChannels,
}: {
  contact: PatientContact;
  isOwner: boolean;
  onPromote: () => void;
  onRevoke: () => void;
  onOpenChannels: () => void;
}) {
  const t = useTranslations("patient");
  const isDelegated = !!contact.delegation_share_link_id;
  const level = contact.delegation_level;

  return (
    <div
      style={{
        padding: "0.4rem 0.65rem",
        background: isDelegated
          ? "color-mix(in srgb, var(--bv-accent, #e96b1f) 8%, var(--bv-card-bg))"
          : "var(--bv-card-bg)",
        border: isDelegated
          ? "1px solid color-mix(in srgb, var(--bv-accent, #e96b1f) 35%, transparent)"
          : "1px solid var(--bv-card-border)",
        borderRadius: "var(--bv-r-sm)",
        fontSize: "0.85rem",
        display: "inline-flex",
        alignItems: "center",
        gap: "0.5rem",
        flexWrap: "wrap",
      }}
    >
      <strong>{contact.label}</strong>
      {contact.relationship && (
        <span className="meta" style={{ fontSize: "0.78rem" }}>
          ({contact.relationship})
        </span>
      )}
      {contact.phone && (
        <a href={`tel:${contact.phone}`} style={{ fontSize: "0.82rem" }}>
          {contact.phone}
        </a>
      )}
      {contact.email && (
        <a href={`mailto:${contact.email}`} style={{ fontSize: "0.82rem" }}>
          {contact.email}
        </a>
      )}
      {isDelegated && level && (
        <span
          style={{
            fontSize: "0.72rem",
            padding: "1px 6px",
            borderRadius: 4,
            background: "var(--bv-accent, #e96b1f)",
            color: "#fff",
            letterSpacing: "0.02em",
            textTransform: "uppercase",
          }}
          title={t(`delegation.levelTitle.${level}` as const)}
        >
          {t(`delegation.levelBadge.${level}` as const)}
        </span>
      )}
      {contact.telegram_chat_id && (
        <span
          title="Telegram collegato"
          style={{
            fontSize: "0.7rem",
            padding: "1px 6px",
            borderRadius: 4,
            background: "var(--bv-info-soft, #eef4ff)",
            color: "var(--bv-info, #1e40af)",
          }}
        >
          ✈ TG
        </span>
      )}
      {contact.email_delivery_state === "bounced" && (
        <span
          title="Email bounced — controlla l'indirizzo"
          style={{
            fontSize: "0.7rem",
            padding: "1px 6px",
            borderRadius: 4,
            background: "var(--bv-danger-soft, #fef2f2)",
            color: "var(--bv-danger, #b91c1c)",
          }}
        >
          ⚠ Bounce
        </span>
      )}
      {isOwner && contact.id && (
        <>
          <button
            type="button"
            className="ghost"
            onClick={onOpenChannels}
            style={{
              fontSize: "0.74rem",
              padding: "2px 8px",
              marginLeft: "0.25rem",
            }}
            title={t("channels.openTitle")}
          >
            {t("channels.openAction")}
          </button>
          <button
            type="button"
            className="ghost"
            onClick={isDelegated ? onRevoke : onPromote}
            style={{
              fontSize: "0.74rem",
              padding: "2px 8px",
            }}
            title={isDelegated ? t("delegation.revokeTitle") : t("delegation.promoteTitle")}
          >
            {isDelegated ? t("delegation.revokeAction") : t("delegation.promoteAction")}
          </button>
        </>
      )}
    </div>
  );
}

// --- Promote dialog ----------------------------------------------------

function DelegateContactDialog({
  patientId,
  contact,
  onClose,
  onSuccess,
}: {
  patientId: string;
  contact: PatientContact | null;
  onClose: () => void;
  onSuccess: (result: ContactDelegateResponse) => void;
}) {
  const t = useTranslations("patient");
  const [accessLevel, setAccessLevel] = useState<"viewer" | "editor" | "manager">("editor");
  const [expiresInDays, setExpiresInDays] = useState<string>(""); // empty = permanent
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!contact) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!contact.id) return;
    setErr(null);
    setSubmitting(true);
    try {
      const expires_in_hours =
        expiresInDays.trim() === "" ? null : Math.max(1, Number.parseInt(expiresInDays, 10) * 24);
      const result = await patientsApi.delegateContact(patientId, contact.id, {
        access_level: accessLevel,
        expires_in_hours,
        autogen_password: true,
      });
      onSuccess(result);
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : t("delegation.errorGeneric"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <NativeDialog open onClose={onClose} className="bv-dialog">
      <form
        onSubmit={submit}
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 480,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1rem" }}>
          {t("delegation.dialogTitle", { name: contact.label })}
        </h3>
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-fg-soft)" }}>
          {t("delegation.dialogIntro")}
        </p>

        <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
          <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>
            {t("delegation.levelLabel")}
          </span>
          <select
            value={accessLevel}
            onChange={(e) => setAccessLevel(e.target.value as typeof accessLevel)}
            disabled={submitting}
          >
            <option value="viewer">{t("delegation.levelOption.viewer")}</option>
            <option value="editor">{t("delegation.levelOption.editor")}</option>
            <option value="manager">{t("delegation.levelOption.manager")}</option>
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
          <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>
            {t("delegation.expiresLabel")}
          </span>
          <input
            type="number"
            min={1}
            placeholder={t("delegation.expiresPlaceholder")}
            value={expiresInDays}
            onChange={(e) => setExpiresInDays(e.target.value)}
            disabled={submitting}
          />
          <span style={{ fontSize: "0.72rem", color: "var(--bv-fg-soft)" }}>
            {t("delegation.expiresHint")}
          </span>
        </label>

        {err && (
          <p style={{ color: "var(--bv-error, #cf6e6e)", fontSize: "0.82rem", margin: 0 }}>{err}</p>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.4rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose} disabled={submitting}>
            {t("delegation.cancel")}
          </button>
          <button type="submit" disabled={submitting}>
            {submitting ? t("delegation.submitting") : t("delegation.submit")}
          </button>
        </div>
      </form>
    </NativeDialog>
  );
}

// --- Result banner (one-time password + magic link) -------------------

function DelegationResultBanner({
  result,
  onClose,
}: {
  result: ContactDelegateResponse;
  onClose: () => void;
}) {
  const t = useTranslations("patient");
  const [copiedField, setCopiedField] = useState<"url" | "password" | null>(null);

  const copy = async (text: string, field: "url" | "password") => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedField(field);
      window.setTimeout(() => setCopiedField(null), 1800);
    } catch {
      // no-op — modern browsers grant clipboard write inside a user
      // gesture, but headless / restricted contexts may block it. The
      // text is still selectable in the input field as a fallback.
    }
  };

  return (
    <NativeDialog open onClose={onClose} className="bv-dialog">
      <div
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 540,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1rem" }}>{t("delegation.successTitle")}</h3>
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-fg-soft)" }}>
          {t("delegation.successIntro")}
        </p>

        <CopyRow
          label={t("delegation.shareUrlLabel")}
          value={result.share_url}
          onCopy={() => copy(result.share_url, "url")}
          copied={copiedField === "url"}
        />
        {result.generated_password && (
          <CopyRow
            label={t("delegation.passwordLabel")}
            value={result.generated_password}
            onCopy={() => copy(result.generated_password ?? "", "password")}
            copied={copiedField === "password"}
            warn={t("delegation.passwordOnceWarning")}
          />
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.4rem",
          }}
        >
          <button type="button" onClick={onClose}>
            {t("delegation.successClose")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}

function CopyRow({
  label,
  value,
  onCopy,
  copied,
  warn,
}: {
  label: string;
  value: string;
  onCopy: () => void;
  copied: boolean;
  warn?: string;
}) {
  const t = useTranslations("patient");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      <span style={{ fontSize: "0.78rem", color: "var(--bv-fg-soft)" }}>{label}</span>
      <div style={{ display: "flex", gap: "0.4rem" }}>
        <input
          type="text"
          readOnly
          value={value}
          onFocus={(e) => e.currentTarget.select()}
          style={{
            flex: 1,
            fontFamily: "ui-monospace, Menlo, monospace",
            fontSize: "0.82rem",
          }}
        />
        <button type="button" className="ghost" onClick={onCopy}>
          {copied ? t("delegation.copied") : t("delegation.copy")}
        </button>
      </div>
      {warn && (
        <span
          style={{
            fontSize: "0.74rem",
            color: "var(--bv-warning, #c98b2a)",
          }}
        >
          {warn}
        </span>
      )}
    </div>
  );
}

// --- Notification channels dialog (sprint D3) -------------------------

function ContactChannelsDialog({
  patientId,
  contact,
  onClose,
  onChanged,
}: {
  patientId: string;
  contact: PatientContact | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const t = useTranslations("patient");
  const [consentEmail, setConsentEmail] = useState<boolean>(!!contact?.consent_email);
  const [consentTelegram, setConsentTelegram] = useState<boolean>(!!contact?.consent_telegram);
  const [consentWebhook, setConsentWebhook] = useState<boolean>(!!contact?.consent_webhook);
  const [webhookUrl, setWebhookUrl] = useState<string>(contact?.webhook_url ?? "");
  const [whatsappPhone, setWhatsappPhone] = useState<string>(contact?.whatsapp_phone ?? "");
  const [consentWhatsapp, setConsentWhatsapp] = useState<boolean>(!!contact?.consent_whatsapp);
  const [preferredLocale, setPreferredLocale] = useState<string>(contact?.preferred_locale ?? "it");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);
  const [telegramLink, setTelegramLink] = useState<{
    code: string;
    deep_link_url: string;
    expires_at: string;
  } | null>(null);
  const [telegramStatus, setTelegramStatus] = useState<"pending" | "linked" | "expired" | "none">(
    contact?.telegram_chat_id ? "linked" : "none",
  );
  const [webhookSecret, setWebhookSecret] = useState("");

  if (!contact || !contact.id) return null;
  const contactId = contact.id;

  // Poll the Telegram link status every 3 seconds while the modal
  // shows a pending code; stop once linked / expired or modal closes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: telegramLink presence is the explicit trigger.
  useEffect(() => {
    if (!telegramLink) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const status = await patientsApi.pollTelegramLink(patientId, contactId);
        if (cancelled) return;
        setTelegramStatus(status.status);
        if (status.status === "linked") {
          setOkMsg(t("channels.telegramLinkedOk"));
          setTelegramLink(null);
          onChanged();
        } else if (status.status === "expired") {
          setErr(t("channels.telegramExpired"));
          setTelegramLink(null);
        }
      } catch {
        // Network blip: keep polling.
      }
    };
    const id = setInterval(tick, 3000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [telegramLink, patientId, contactId, onChanged, t]);

  const save = async () => {
    setBusy(true);
    setErr(null);
    setOkMsg(null);
    try {
      await patientsApi.configureContactChannel(patientId, contactId, {
        preferred_locale: preferredLocale,
        whatsapp_phone: whatsappPhone || null,
        webhook_url: webhookUrl || null,
        consent_email: consentEmail,
        consent_telegram: consentTelegram,
        consent_whatsapp: consentWhatsapp,
        consent_webhook: consentWebhook,
      });
      setOkMsg(t("channels.saved"));
      onChanged();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("channels.errorGeneric"));
    } finally {
      setBusy(false);
    }
  };

  const startTelegram = async () => {
    setBusy(true);
    setErr(null);
    try {
      const out = await patientsApi.startTelegramLink(patientId, contactId);
      setTelegramLink(out);
      setTelegramStatus("pending");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("channels.errorGeneric"));
    } finally {
      setBusy(false);
    }
  };

  const unlinkTg = async () => {
    setBusy(true);
    setErr(null);
    try {
      await patientsApi.unlinkTelegram(patientId, contactId);
      setTelegramStatus("none");
      setOkMsg(t("channels.telegramUnlinked"));
      onChanged();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("channels.errorGeneric"));
    } finally {
      setBusy(false);
    }
  };

  const setSecret = async () => {
    if (webhookSecret.length < 16) {
      setErr(t("channels.webhookSecretShort"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await patientsApi.setWebhookSecret(patientId, contactId, webhookSecret);
      setOkMsg(t("channels.webhookSecretSet"));
      setWebhookSecret("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("channels.errorGeneric"));
    } finally {
      setBusy(false);
    }
  };

  const sendTest = async (channel: string) => {
    setBusy(true);
    setErr(null);
    try {
      await patientsApi.sendTestNotification(patientId, contactId, channel);
      setOkMsg(t("channels.testQueued"));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("channels.errorGeneric"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <NativeDialog
      open
      onClose={onClose}
      ariaLabel={t("channels.dialogTitle")}
      className="bv-dialog"
    >
      <div
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 480,
          width: "100%",
          maxHeight: "85vh",
          overflowY: "auto",
        }}
      >
        <h3 style={{ marginTop: 0 }}>{t("channels.dialogTitle")}</h3>
        <p className="meta" style={{ fontSize: "0.82rem" }}>
          {t("channels.dialogIntro", { name: contact.label })}
        </p>
        <hr
          style={{
            margin: "0.8rem 0",
            border: "none",
            borderTop: "1px solid var(--bv-card-border)",
          }}
        />

        <h4 style={{ fontSize: "0.92rem", margin: "0.6rem 0 0.3rem" }}>
          {t("channels.localeHeading")}
        </h4>
        <select
          value={preferredLocale}
          onChange={(e) => setPreferredLocale(e.target.value)}
          style={{ fontSize: "0.85rem", padding: "0.2rem 0.4rem" }}
        >
          <option value="it">Italiano</option>
          <option value="en">English</option>
        </select>

        <h4 style={{ fontSize: "0.92rem", margin: "0.8rem 0 0.3rem" }}>
          {t("channels.emailHeading")}
        </h4>
        <p style={{ fontSize: "0.82rem", margin: 0 }}>
          {contact.email || <em>{t("channels.emailMissing")}</em>}
          {contact.email_delivery_state === "bounced" && (
            <span style={{ marginLeft: 8, color: "var(--bv-danger, #b91c1c)" }}>
              {t("channels.emailBounced")}
            </span>
          )}
        </p>
        <label
          style={{
            display: "inline-flex",
            gap: "0.4rem",
            alignItems: "center",
            fontSize: "0.85rem",
          }}
        >
          <input
            type="checkbox"
            checked={consentEmail}
            onChange={(e) => setConsentEmail(e.target.checked)}
            disabled={!contact.email}
          />
          {t("channels.consentEmail")}
        </label>
        {contact.email && (
          <button
            type="button"
            className="ghost"
            disabled={busy || !consentEmail}
            onClick={() => sendTest("email")}
            style={{ marginLeft: 8, fontSize: "0.75rem", padding: "2px 8px" }}
          >
            {t("channels.sendTest")}
          </button>
        )}

        <h4 style={{ fontSize: "0.92rem", margin: "0.8rem 0 0.3rem" }}>
          {t("channels.telegramHeading")}
        </h4>
        {telegramStatus === "linked" ? (
          <div style={{ fontSize: "0.85rem" }}>
            <p style={{ margin: "0 0 0.4rem" }}>{t("channels.telegramLinkedHint")}</p>
            <label
              style={{
                display: "inline-flex",
                gap: "0.4rem",
                alignItems: "center",
                fontSize: "0.85rem",
              }}
            >
              <input
                type="checkbox"
                checked={consentTelegram}
                onChange={(e) => setConsentTelegram(e.target.checked)}
              />
              {t("channels.consentTelegram")}
            </label>
            <div style={{ marginTop: "0.4rem", display: "flex", gap: "0.4rem" }}>
              <button
                type="button"
                className="ghost"
                disabled={busy || !consentTelegram}
                onClick={() => sendTest("webhook_telegram")}
                style={{ fontSize: "0.75rem", padding: "2px 8px" }}
              >
                {t("channels.sendTest")}
              </button>
              <button
                type="button"
                className="ghost"
                disabled={busy}
                onClick={unlinkTg}
                style={{ fontSize: "0.75rem", padding: "2px 8px" }}
              >
                {t("channels.telegramUnlink")}
              </button>
            </div>
          </div>
        ) : telegramLink ? (
          <div style={{ fontSize: "0.85rem" }}>
            <p style={{ margin: "0 0 0.4rem" }}>{t("channels.telegramPendingInstructions")}</p>
            <a
              href={telegramLink.deep_link_url}
              target="_blank"
              rel="noopener noreferrer"
              style={{ fontWeight: 600 }}
            >
              {telegramLink.deep_link_url}
            </a>
            <p style={{ fontSize: "0.78rem", marginTop: "0.4rem" }}>
              {t("channels.telegramCode")}: <code>{telegramLink.code}</code>
            </p>
            <p style={{ fontSize: "0.75rem", color: "var(--bv-muted, #64748b)" }}>
              {t("channels.telegramExpiresAt", { time: telegramLink.expires_at })}
            </p>
          </div>
        ) : (
          <button
            type="button"
            onClick={startTelegram}
            disabled={busy}
            style={{ fontSize: "0.85rem" }}
          >
            {t("channels.telegramConnect")}
          </button>
        )}

        <h4 style={{ fontSize: "0.92rem", margin: "0.8rem 0 0.3rem" }}>
          {t("channels.webhookHeading")}
        </h4>
        <input
          type="url"
          placeholder="https://example.org/webhook"
          value={webhookUrl}
          onChange={(e) => setWebhookUrl(e.target.value)}
          style={{ width: "100%", fontSize: "0.85rem", padding: "0.25rem 0.4rem" }}
        />
        <label
          style={{
            display: "inline-flex",
            gap: "0.4rem",
            alignItems: "center",
            fontSize: "0.85rem",
            marginTop: "0.3rem",
          }}
        >
          <input
            type="checkbox"
            checked={consentWebhook}
            onChange={(e) => setConsentWebhook(e.target.checked)}
            disabled={!webhookUrl}
          />
          {t("channels.consentWebhook")}
        </label>
        <div style={{ marginTop: "0.4rem", display: "flex", gap: "0.4rem", alignItems: "center" }}>
          <input
            type="password"
            placeholder={t("channels.webhookSecretPlaceholder")}
            value={webhookSecret}
            onChange={(e) => setWebhookSecret(e.target.value)}
            style={{ flex: 1, fontSize: "0.85rem", padding: "0.25rem 0.4rem" }}
          />
          <button
            type="button"
            className="ghost"
            onClick={setSecret}
            disabled={busy || webhookSecret.length < 16}
            style={{ fontSize: "0.75rem", padding: "2px 8px" }}
          >
            {t("channels.webhookSecretSave")}
          </button>
        </div>

        <h4 style={{ fontSize: "0.92rem", margin: "0.8rem 0 0.3rem" }}>
          {t("channels.whatsappHeading")}
        </h4>
        <input
          type="tel"
          placeholder="+39…"
          value={whatsappPhone}
          onChange={(e) => setWhatsappPhone(e.target.value)}
          style={{ width: "100%", fontSize: "0.85rem", padding: "0.25rem 0.4rem" }}
        />
        <label
          style={{
            display: "inline-flex",
            gap: "0.4rem",
            alignItems: "center",
            fontSize: "0.85rem",
            marginTop: "0.3rem",
          }}
        >
          <input
            type="checkbox"
            checked={consentWhatsapp}
            onChange={(e) => setConsentWhatsapp(e.target.checked)}
            disabled={!whatsappPhone}
          />
          {t("channels.consentWhatsapp")}
        </label>
        <p style={{ fontSize: "0.75rem", color: "var(--bv-muted, #64748b)" }}>
          {t("channels.whatsappFlagOff")}
        </p>

        {err && (
          <p
            style={{ color: "var(--bv-danger, #b91c1c)", fontSize: "0.85rem", marginTop: "0.6rem" }}
          >
            {err}
          </p>
        )}
        {okMsg && (
          <p
            style={{
              color: "var(--bv-success, #047857)",
              fontSize: "0.85rem",
              marginTop: "0.6rem",
            }}
          >
            {okMsg}
          </p>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "1rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            {t("channels.close")}
          </button>
          <button type="button" onClick={save} disabled={busy}>
            {busy ? t("channels.saving") : t("channels.save")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
