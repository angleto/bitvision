"use client";

// Lista gestibile dei share-link che il caller ha creato.
// Mounted from two routes — /settings/shares (cross-paziente) e
// /patients/{id}/shares (filtrato sul singolo paziente). Lo stesso
// componente serve entrambi: il filtro patient_id arriva via prop.
//
// Polling: il backend già join-a sui ``jobs`` per le colonne
// ``prepared_status`` e ``prepared_progress_*``, ma uno share appena
// creato passa per queued → running → succeeded in pochi minuti, e
// il grantor vuole vedere la progress bar muoversi senza ricaricare.
// Re-fetch ogni 8 secondi finché esiste almeno una riga in stato
// non-terminal; appena tutto è terminal il polling si ferma.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useState } from "react";

import EditShareDialog from "@/components/EditShareDialog";
import ResendShareDialog from "@/components/ResendShareDialog";
import { ApiError, type ShareLink, patientsApi } from "@/lib/api";

interface Props {
  /** Quando passato, la lista mostra solo i share-link che
   *  toccano questo paziente. Omettere per vista globale. */
  patientId?: string;
  /** Etichetta sopra la tabella. La pagina /settings/shares ne
   *  usa una globale; la sotto-pagina del paziente la rende
   *  contestuale. */
  heading?: string;
}

const POLL_INTERVAL_MS = 8000;
const TERMINAL_PREP_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

export default function ShareLinksTable({ patientId, heading }: Props) {
  const t = useTranslations("shareLinksTable");
  const [items, setItems] = useState<ShareLink[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [includeExpired, setIncludeExpired] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<ShareLink | null>(null);
  // Resend opens an options dialog (custom message + locale +
  // password-not-included reminder) instead of firing the email
  // immediately. Stray clicks no longer notify the recipient.
  const [resending, setResending] = useState<ShareLink | null>(null);

  const fetchList = useCallback(async () => {
    try {
      const list = await patientsApi.listMyShares({
        patient_id: patientId,
        include_revoked: includeRevoked,
        include_expired: includeExpired,
        limit: 200,
      });
      setItems(list.items);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, [patientId, includeRevoked, includeExpired]);

  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  // Adaptive polling: as long as at least one row is in a
  // non-terminal prep state we re-fetch every POLL_INTERVAL_MS.
  // The interval is short enough to feel live but long enough not
  // to thrash the backend if the user leaves the page open.
  useEffect(() => {
    const anyActive = items.some(
      (it) => it.prepared_status && !TERMINAL_PREP_STATUSES.has(it.prepared_status),
    );
    if (!anyActive) return;
    const handle = window.setInterval(() => {
      void fetchList();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(handle);
  }, [items, fetchList]);

  const handleRevoke = useCallback(
    async (link: ShareLink) => {
      if (!window.confirm(t("revokeConfirm"))) return;
      setBusyId(link.id);
      try {
        await patientsApi.revokeShare(link.id);
        await fetchList();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "revoke failed");
      } finally {
        setBusyId(null);
      }
    },
    [t, fetchList],
  );

  const handleExtend = useCallback(
    async (link: ShareLink, addMonths: 1 | 3 | 6 | 12) => {
      setBusyId(link.id);
      try {
        await patientsApi.extendShare(link.id, addMonths);
        await fetchList();
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "extend failed");
      } finally {
        setBusyId(null);
      }
    },
    [fetchList],
  );

  const handleResend = useCallback((link: ShareLink) => {
    if (!link.recipient_email) return;
    setResending(link);
  }, []);

  const sorted = useMemo(() => {
    // Ordinamento: link senza scadenza prima (legacy / pericolosi),
    // poi attivi per data scadenza ASC, infine terminali per data
    // creazione DESC. Voluto: l'utente vede subito quali link
    // richiedono attenzione.
    return [...items].sort((a, b) => {
      const aTerm = !!(a.revoked_at || (a.expires_at && new Date(a.expires_at) < new Date()));
      const bTerm = !!(b.revoked_at || (b.expires_at && new Date(b.expires_at) < new Date()));
      if (aTerm !== bTerm) return aTerm ? 1 : -1;
      if (!a.expires_at && b.expires_at) return -1;
      if (a.expires_at && !b.expires_at) return 1;
      if (a.expires_at && b.expires_at) {
        return new Date(a.expires_at).getTime() - new Date(b.expires_at).getTime();
      }
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
  }, [items]);

  if (loading) return <p className="meta">{t("loading")}</p>;

  return (
    <section style={{ width: "100%" }}>
      {heading && <h2 style={{ marginBottom: "0.5rem" }}>{heading}</h2>}
      <div
        style={{
          display: "flex",
          gap: "0.75rem",
          alignItems: "center",
          marginBottom: "0.5rem",
          fontSize: "0.85rem",
        }}
      >
        <label style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
          <input
            type="checkbox"
            checked={includeRevoked}
            onChange={(e) => setIncludeRevoked(e.target.checked)}
          />
          {t("filterIncludeRevoked")}
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
          <input
            type="checkbox"
            checked={includeExpired}
            onChange={(e) => setIncludeExpired(e.target.checked)}
          />
          {t("filterIncludeExpired")}
        </label>
        <span style={{ marginLeft: "auto", opacity: 0.7 }}>
          {t("rowCount", { n: items.length })}
        </span>
      </div>

      {error && <p className="error">{error}</p>}

      {items.length === 0 ? (
        <p className="meta">{t("empty")}</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.85rem",
            }}
          >
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid var(--border)" }}>
                <th style={th}>{t("colKind")}</th>
                <th style={th}>{t("colLabel")}</th>
                <th style={th}>{t("colRecipient")}</th>
                <th style={th}>{t("colCreated")}</th>
                <th style={th}>{t("colExpires")}</th>
                <th style={th}>{t("colPrep")}</th>
                <th style={th}>{t("colDownloads")}</th>
                <th style={th}>{t("colReceived")}</th>
                <th style={th}>{t("colDeid")}</th>
                <th style={th}>{t("colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((link) => (
                <ShareLinkRow
                  key={link.id}
                  link={link}
                  busy={busyId === link.id}
                  onRevoke={() => handleRevoke(link)}
                  onExtend={(m) => handleExtend(link, m)}
                  onResend={() => handleResend(link)}
                  onEdit={() => setEditing(link)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <EditShareDialog
          link={editing}
          open
          onClose={() => setEditing(null)}
          onSaved={() => {
            void fetchList();
          }}
        />
      )}
      {resending && (
        <ResendShareDialog
          link={resending}
          open
          onClose={() => setResending(null)}
          onSent={() => {
            void fetchList();
          }}
        />
      )}
    </section>
  );
}

const th: React.CSSProperties = {
  padding: "0.4rem 0.5rem",
  fontWeight: 600,
  whiteSpace: "nowrap",
};
const td: React.CSSProperties = {
  padding: "0.4rem 0.5rem",
  borderBottom: "1px solid var(--border, #f0f0f0)",
  verticalAlign: "top",
};

function ShareLinkRow({
  link,
  busy,
  onRevoke,
  onExtend,
  onResend,
  onEdit,
}: {
  link: ShareLink;
  busy: boolean;
  onRevoke: () => void;
  onExtend: (m: 1 | 3 | 6 | 12) => void;
  onResend: () => void;
  onEdit: () => void;
}) {
  const t = useTranslations("shareLinksTable");
  const expiresAt = link.expires_at ? new Date(link.expires_at) : null;
  const isExpired = expiresAt !== null && expiresAt.getTime() < Date.now();
  const isRevoked = link.revoked || !!link.revoked_at;
  const isTerminal = isExpired || isRevoked;
  const prepStatus = link.prepared_status ?? null;
  const prepPct =
    link.prepared_progress_total && link.prepared_progress_total > 0
      ? Math.floor(((link.prepared_progress_done ?? 0) / link.prepared_progress_total) * 100)
      : null;
  return (
    <tr style={{ opacity: isTerminal ? 0.55 : 1 }}>
      <td style={td}>
        <div style={{ fontWeight: 500 }}>{t(`kind.${link.resource_kind || "study"}`)}</div>
      </td>
      <td style={td}>
        {link.label ? (
          <span title={link.label}>{link.label}</span>
        ) : (
          <span style={{ opacity: 0.45 }}>—</span>
        )}
      </td>
      <td style={td}>
        {link.recipient_name && <div>{link.recipient_name}</div>}
        {link.recipient_email && (
          <div style={{ fontSize: "0.78rem", opacity: 0.75 }}>{link.recipient_email}</div>
        )}
        {!link.recipient_name && !link.recipient_email && <span style={{ opacity: 0.55 }}>—</span>}
      </td>
      <td style={td}>{new Date(link.created_at).toLocaleDateString()}</td>
      <td style={td}>
        {expiresAt ? (
          <span style={{ color: isExpired ? "var(--bv-danger, #b91c1c)" : undefined }}>
            {expiresAt.toLocaleDateString()}
          </span>
        ) : (
          <span style={{ color: "var(--bv-warning, #b45309)" }}>{t("noExpiry")}</span>
        )}
      </td>
      <td style={td}>
        <PrepCell status={prepStatus} pct={prepPct} />
      </td>
      <td style={td}>
        <span title={t("downloadCountTitle")} style={{ fontVariantNumeric: "tabular-nums" }}>
          {link.download_count ?? 0}
        </span>
      </td>
      <td style={td}>
        {link.received_at ? (
          <span
            title={new Date(link.received_at).toLocaleString()}
            style={{ color: "var(--bv-success, #047857)" }}
          >
            ✓
          </span>
        ) : (
          <span style={{ opacity: 0.45 }}>—</span>
        )}
      </td>
      <td style={td}>
        {link.deidentify ? (
          <span title={t("deidOnTitle")}>{t("deidOn")}</span>
        ) : (
          <span style={{ color: "var(--bv-warning, #b45309)" }} title={t("deidOffTitle")}>
            {t("deidOff")}
          </span>
        )}
      </td>
      <td style={td}>
        <div style={{ display: "inline-flex", gap: "0.3rem", flexWrap: "wrap" }}>
          {!isTerminal && (
            <>
              <button
                type="button"
                className="ghost"
                disabled={busy}
                onClick={() => onExtend(3)}
                title={t("extend3Title")}
                style={{ fontSize: "0.78rem" }}
              >
                +3m
              </button>
              <button
                type="button"
                className="ghost"
                disabled={busy}
                onClick={() => onExtend(6)}
                title={t("extend6Title")}
                style={{ fontSize: "0.78rem" }}
              >
                +6m
              </button>
              <button
                type="button"
                className="ghost"
                disabled={busy}
                onClick={() => onExtend(12)}
                title={t("extend12Title")}
                style={{ fontSize: "0.78rem" }}
              >
                +1a
              </button>
            </>
          )}
          {!isTerminal && link.recipient_email && (
            <button
              type="button"
              className="ghost"
              disabled={busy}
              onClick={onResend}
              title={t("resendTitle")}
              style={{ fontSize: "0.78rem" }}
            >
              {t("resend")}
            </button>
          )}
          {!isTerminal && (
            <button
              type="button"
              className="ghost"
              disabled={busy}
              onClick={onEdit}
              title={t("editTitle")}
              style={{ fontSize: "0.78rem" }}
            >
              {t("edit")}
            </button>
          )}
          {!isRevoked && (
            <button
              type="button"
              className="ghost"
              disabled={busy}
              onClick={onRevoke}
              title={t("revokeTitle")}
              style={{ fontSize: "0.78rem", color: "var(--bv-danger, #b91c1c)" }}
            >
              {t("revoke")}
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

function PrepCell({ status, pct }: { status: string | null; pct: number | null }) {
  const t = useTranslations("shareLinksTable");
  if (!status) return <span style={{ opacity: 0.55 }}>—</span>;
  if (status === "succeeded") {
    return (
      <span style={{ color: "var(--bv-success, #047857)", fontWeight: 500 }}>{t("prepReady")}</span>
    );
  }
  if (status === "failed" || status === "cancelled") {
    return <span style={{ color: "var(--bv-danger, #b91c1c)" }}>{t(`prep.${status}`)}</span>;
  }
  // queued / running
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
      <span
        aria-hidden
        style={{
          display: "inline-block",
          width: 60,
          height: 4,
          borderRadius: 2,
          background: "var(--bv-card-border, #e5e7eb)",
          overflow: "hidden",
          position: "relative",
        }}
      >
        <span
          style={{
            position: "absolute",
            inset: 0,
            width: pct != null ? `${pct}%` : "30%",
            background: "var(--bv-accent, #2563eb)",
            transition: "width 0.4s ease",
          }}
        />
      </span>
      <span style={{ fontSize: "0.78rem" }}>{pct != null ? `${pct}%` : t(`prep.${status}`)}</span>
    </span>
  );
}
