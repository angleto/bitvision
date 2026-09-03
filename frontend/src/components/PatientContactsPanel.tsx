"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type ContactDelegateResponse,
  type NotifyShareLinkResult,
  type Patient,
  type PatientContact,
  patientsApi,
} from "@/lib/api";

/** What the promote flow produced: the delegation, plus the outcome of
 *  the invitation email when one was sent. ``delivery === null`` means
 *  either no email was attempted or the relay did not take it — the
 *  banner then falls back to handing the operator the link. */
type DelegationOutcome = ContactDelegateResponse & {
  delivery?: NotifyShareLinkResult | null;
};

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
  const [confirmDeleteFor, setConfirmDeleteFor] = useState<string | null>(null);
  const [pendingResult, setPendingResult] = useState<DelegationOutcome | null>(null);
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

  // Deleting a contact used to be impossible from any screen: the row
  // could only be removed by re-sending the whole patient record, and
  // that path duplicated the delegated contacts instead of removing
  // anything. The per-contact endpoint has always existed; this is the
  // affordance that reaches it.
  const handleDelete = async (contactId: string, revokeDelegation: boolean) => {
    setActionErr(null);
    try {
      await patientsApi.removeContact(patient.id, contactId, revokeDelegation);
      setConfirmDeleteFor(null);
      onChanged();
    } catch (e) {
      setActionErr(e instanceof ApiError ? e.message : t("contactDelete.errorGeneric"));
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
            onDelete={() => c.id && setConfirmDeleteFor(c.id)}
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
      {confirmDeleteFor && (
        <DeleteContactDialog
          contact={patient.contacts.find((c) => c.id === confirmDeleteFor) ?? null}
          onCancel={() => setConfirmDeleteFor(null)}
          onConfirm={(revokeDelegation) => handleDelete(confirmDeleteFor, revokeDelegation)}
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
  onDelete,
}: {
  contact: PatientContact;
  isOwner: boolean;
  onPromote: () => void;
  onRevoke: () => void;
  onOpenChannels: () => void;
  onDelete: () => void;
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
          <button
            type="button"
            className="ghost"
            onClick={onDelete}
            style={{
              fontSize: "0.74rem",
              padding: "2px 8px",
              // --bv-danger is a foreground red; used as a background it
              // renders dark-on-dark in the dark theme.
              color: "var(--bv-danger)",
            }}
            title={t("contactDelete.title", { name: contact.label })}
          >
            {t("contactDelete.action")}
          </button>
        </>
      )}
    </div>
  );
}

// --- Delete confirmation ----------------------------------------------

/**
 * Confirms removing a contact, and says plainly what else goes with it.
 *
 * A contact holding a live delegation is two things at once: a name in
 * a list, and somebody with a working grant on the health record. The
 * server refuses to remove the first while the second stands, unless
 * the caller says explicitly that the access goes too — because a
 * contact list that quietly stops mentioning a person who can still
 * read the record is worse than no list. So the dialog asks, once, in
 * the words that describe the consequence.
 */
function DeleteContactDialog({
  contact,
  onCancel,
  onConfirm,
}: {
  contact: PatientContact | null;
  onCancel: () => void;
  onConfirm: (revokeDelegation: boolean) => void;
}) {
  const t = useTranslations("patient");
  const [busy, setBusy] = useState(false);
  if (!contact) return null;
  const isDelegated = !!contact.delegation_share_link_id;

  return (
    <NativeDialog open onClose={onCancel} className="bv-dialog">
      <div
        style={{
          background: "var(--bv-card-bg)",
          color: "var(--bv-fg)",
          border: "1px solid var(--bv-card-border)",
          borderRadius: "var(--bv-r-md)",
          padding: "1.25rem 1.4rem",
          maxWidth: 460,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          gap: "0.75rem",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1rem" }}>
          {t("contactDelete.dialogTitle", { name: contact.label })}
        </h3>
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-fg-soft)" }}>
          {isDelegated
            ? t("contactDelete.delegatedWarning", { name: contact.label })
            : t("contactDelete.plainIntro")}
        </p>
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginTop: "0.4rem",
          }}
        >
          <button type="button" className="ghost" onClick={onCancel} disabled={busy}>
            {t("contactDelete.cancel")}
          </button>
          <button
            type="button"
            onClick={() => {
              setBusy(true);
              onConfirm(isDelegated);
            }}
            disabled={busy}
            style={{ color: "var(--bv-danger)" }}
          >
            {busy
              ? t("contactDelete.submitting")
              : isDelegated
                ? t("contactDelete.confirmWithRevoke")
                : t("contactDelete.confirm")}
          </button>
        </div>
      </div>
    </NativeDialog>
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
  onSuccess: (result: DelegationOutcome) => void;
}) {
  const t = useTranslations("patient");
  const [accessLevel, setAccessLevel] = useState<"viewer" | "editor" | "manager">("editor");
  const [expiresInDays, setExpiresInDays] = useState<string>(""); // empty = permanent
  // Emailing the invitation is the default when the contact has an
  // address. The alternative — mint a link password and read it out to
  // the recipient — is what made operators believe they had set an
  // account password for that person. They had not: it unlocks the
  // link and nothing else.
  const [sendEmail, setSendEmail] = useState(true);
  const [withLinkPassword, setWithLinkPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!contact) return null;
  const hasEmail = !!contact.email?.trim();

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
        // Without an address there is nobody to email, so the link has
        // to be handed over by other means and a password on it is the
        // only thing standing between a forwarded URL and the record.
        autogen_password: withLinkPassword || !hasEmail,
      });
      let delivery: NotifyShareLinkResult | null = null;
      if (sendEmail && hasEmail) {
        try {
          delivery = await patientsApi.notifyShareLink(result.delegation_share_link_id);
        } catch {
          // The delegation itself succeeded and is the thing that
          // matters; a relay problem must not read as "it did not
          // work". The banner says the mail did not go out and offers
          // the link to hand over instead.
          delivery = null;
        }
      }
      onSuccess({ ...result, delivery });
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

        {hasEmail ? (
          <>
            <label style={{ display: "flex", alignItems: "flex-start", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={sendEmail}
                onChange={(e) => setSendEmail(e.target.checked)}
                disabled={submitting}
                style={{ marginTop: "0.2rem" }}
              />
              <span style={{ fontSize: "0.82rem" }}>
                {t("delegation.sendEmailLabel", { email: contact.email ?? "" })}
                <span
                  style={{
                    display: "block",
                    fontSize: "0.72rem",
                    color: "var(--bv-fg-soft)",
                  }}
                >
                  {t("delegation.sendEmailHint")}
                </span>
              </span>
            </label>
            <label style={{ display: "flex", alignItems: "flex-start", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={withLinkPassword}
                onChange={(e) => setWithLinkPassword(e.target.checked)}
                disabled={submitting}
                style={{ marginTop: "0.2rem" }}
              />
              <span style={{ fontSize: "0.82rem" }}>
                {t("delegation.linkPasswordLabel")}
                <span
                  style={{
                    display: "block",
                    fontSize: "0.72rem",
                    color: "var(--bv-fg-soft)",
                  }}
                >
                  {t("delegation.linkPasswordHint")}
                </span>
              </span>
            </label>
          </>
        ) : (
          <p
            style={{
              margin: 0,
              fontSize: "0.8rem",
              color: "var(--bv-fg-soft)",
              padding: "0.5rem 0.65rem",
              background: "var(--bv-info-soft)",
              borderRadius: "var(--bv-r-sm)",
            }}
          >
            {t("delegation.noEmailHint")}
          </p>
        )}

        {err && <p style={{ color: "var(--bv-danger)", fontSize: "0.82rem", margin: 0 }}>{err}</p>}

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
  result: DelegationOutcome;
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
        {/* The two outcomes are genuinely different and the screen used
            to describe only one of them. When the address already
            belongs to an account, the grant went onto it and the person
            just signs in — telling them to keep the link would be
            wrong. When it does not, the link is how they create the
            account, and it is the only way in until they do. */}
        <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-fg-soft)" }}>
          {result.recipient_has_account
            ? t("delegation.successExistingAccount", { email: result.recipient_email ?? "" })
            : t("delegation.successNewAccount", { email: result.recipient_email ?? "" })}
        </p>

        {result.delivery?.status === "sent" && (
          <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-success)" }}>
            {t("delegation.emailSent", { email: result.delivery.to })}
          </p>
        )}
        {result.delivery?.status === "queued" && (
          <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-fg-soft)" }}>
            {t("delegation.emailQueued", { email: result.delivery.to })}
          </p>
        )}
        {(result.delivery === null || result.delivery?.status === "failed") &&
          result.recipient_email && (
            <p style={{ margin: 0, fontSize: "0.85rem", color: "var(--bv-danger)" }}>
              {t("delegation.emailFailed", { email: result.recipient_email })}
            </p>
          )}

        <CopyRow
          label={t("delegation.shareUrlLabel")}
          value={result.share_url}
          onCopy={() => copy(result.share_url, "url")}
          copied={copiedField === "url"}
        />
        {result.generated_password && (
          <CopyRow
            label={t("delegation.linkPasswordResultLabel")}
            value={result.generated_password}
            onCopy={() => copy(result.generated_password ?? "", "password")}
            copied={copiedField === "password"}
            warn={t("delegation.linkPasswordResultWarning")}
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
