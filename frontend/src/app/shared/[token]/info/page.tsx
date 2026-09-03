"use client";

// Public landing for a share link — designed to be foolproof for a
// non-technical recipient (often an external clinician). Two screens:
//
//   1. LANDING: one dominant action — "Apri" (passwordless) or a single
//      password field + "Apri". Privacy banner + payload summary above.
//      A secondary "Scarica DICOM" for recipients who only want the ZIP.
//
//   2. POST-OPEN INTERSTITIAL (after /verify sets the bvp_session cookie):
//      "✓ Accesso effettuato" + a big "Vai all'esame/fascicolo", plus a
//      "keep this access — create an account" card that reconciles the
//      share grant onto a real account (claim) or routes an existing
//      account through login+bind. This is the "register once inside"
//      moment, gated behind a successful open so a password link has
//      already proven the recipient holds the password.
//
// Receipt confirmation is fired AUTOMATICALLY on a successful open (the
// strongest possible "they actually opened it" signal for the grantor),
// so there is no separate, confusing "I received it" button.

import { useTranslations } from "next-intl";
import { useParams, useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { API_BASE_URL, ApiError } from "@/lib/api";

interface ShareInfo {
  study_title: string | null;
  modalities: string[];
  study_date: string | null;
  requires_password: boolean;
  expires_at: string | null;
  permissions: string[];
  max_uses: number | null;
  uses_remaining: number | null;
  resource_kind: string;
  resource_id: string;
  deidentified?: boolean;
  total_files?: number | null;
  total_bytes?: number | null;
  grantor_display?: string | null;
  prepared_status?: string | null;
  prepared_progress_done?: number | null;
  prepared_progress_total?: number | null;
  // Account reconciliation hints (see backend ShareInfoOut).
  mode?: string;
  claimable?: boolean;
  bindable?: boolean;
  recipient_email_known?: boolean;
}

const PREP_TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const PREP_POLL_MS = 4000;

async function readErr(resp: Response): Promise<unknown> {
  try {
    return await resp.json();
  } catch {
    return await resp.text();
  }
}

async function fetchInfo(token: string): Promise<ShareInfo> {
  const resp = await fetch(`${API_BASE_URL}/api/shared/${token}/info`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!resp.ok) throw new ApiError(resp.status, await readErr(resp));
  return (await resp.json()) as ShareInfo;
}

// Best-effort, fire-and-forget: closing the audit chain must never block
// or fail the open. The grantor sees ``received_at`` either way.
function confirmReceiptSilently(token: string): void {
  void fetch(`${API_BASE_URL}/api/shared/${token}/confirm-receipt`, {
    credentials: "include",
    method: "POST",
    cache: "no-store",
  }).catch(() => {});
}

function extractErrReason(e: unknown, t: ReturnType<typeof useTranslations>): string {
  if (e instanceof ApiError) {
    const detail = e.detail as { detail?: { reason?: string } } | undefined;
    const reason = detail?.detail?.reason;
    if (reason) {
      const key = `errReason.${reason}`;
      if (t.has(key)) return t(key);
    }
    return t("linkNotFoundFallback");
  }
  return t("linkNotFoundFallback");
}

function permissionLabel(p: string): string {
  const tail = p.split(":")[1] ?? p;
  return tail.replace(/_/g, " ");
}

function formatBytes(n: number | null | undefined): string {
  if (n == null || n <= 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function resourceHref(info: ShareInfo | null): string {
  if (!info) return "/studies";
  if (info.resource_kind === "patient") return `/patients/${info.resource_id}`;
  if (info.resource_kind === "study") return `/studies/${info.resource_id}`;
  return "/studies";
}

export default function SharedInfoPage() {
  const t = useTranslations("sharedLanding");
  const params = useParams<{ token: string }>();
  const router = useRouter();
  const [info, setInfo] = useState<ShareInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Open (verify) state.
  const [openBusy, setOpenBusy] = useState(false);
  const [password, setPassword] = useState("");
  const [verifyErr, setVerifyErr] = useState<string | null>(null);
  const [verified, setVerified] = useState(false);
  const passwordInputRef = useRef<HTMLInputElement | null>(null);

  // "Create an account to keep access" (post-open) state.
  const [regOpen, setRegOpen] = useState(false);
  const [regPassword, setRegPassword] = useState("");
  const [regName, setRegName] = useState("");
  const [regEmail, setRegEmail] = useState("");
  const [regBusy, setRegBusy] = useState(false);
  const [regErr, setRegErr] = useState<string | null>(null);
  const [emailExists, setEmailExists] = useState(false);

  useEffect(() => {
    if (info?.requires_password && !verified && passwordInputRef.current) {
      passwordInputRef.current.focus();
    }
  }, [info?.requires_password, verified]);

  useEffect(() => {
    fetchInfo(params.token)
      .then(setInfo)
      .catch((e) => setErr(extractErrReason(e, t)));
  }, [params.token, t]);

  // Adaptive polling of the pre-export progress while it runs.
  useEffect(() => {
    if (!info || verified) return;
    const status = info.prepared_status;
    if (!status || PREP_TERMINAL.has(status)) return;
    const handle = window.setInterval(() => {
      fetchInfo(params.token)
        .then(setInfo)
        .catch(() => {});
    }, PREP_POLL_MS);
    return () => window.clearInterval(handle);
  }, [info, verified, params.token]);

  async function runVerify(passwordValue: string | null): Promise<void> {
    setOpenBusy(true);
    setVerifyErr(null);
    try {
      const resp = await fetch(`${API_BASE_URL}/api/shared/${params.token}/verify`, {
        credentials: "include",
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ password: passwordValue }),
        cache: "no-store",
      });
      if (!resp.ok) throw new ApiError(resp.status, await readErr(resp));
      // The backend set the bvp_session cookie; the recipient is now in.
      confirmReceiptSilently(params.token);
      setVerified(true);
    } catch (e) {
      setVerifyErr(extractErrReason(e, t));
    } finally {
      setOpenBusy(false);
    }
  }

  async function handleSubmitPassword(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (!password) return;
    await runVerify(password);
  }

  async function handleRegister(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (regPassword.length < 8) {
      setRegErr(t("regErrorPasswordTooShort"));
      return;
    }
    setRegBusy(true);
    setRegErr(null);
    try {
      const body: { password: string; display_name?: string; email?: string } = {
        password: regPassword,
      };
      if (regName.trim()) body.display_name = regName.trim();
      if (!info?.recipient_email_known) body.email = regEmail.trim();
      // Token-based claim: creates the account, reconciles the grant, and
      // logs in via the bvp_session cookie the response sets.
      const resp = await fetch(`${API_BASE_URL}/api/share-links/${params.token}/claim`, {
        credentials: "include",
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      });
      if (!resp.ok) {
        if (resp.status === 409) {
          setEmailExists(true);
          setRegErr(null);
          return;
        }
        throw new ApiError(resp.status, await readErr(resp));
      }
      // Account created + grant reconciled + logged in via cookie.
      router.push(resourceHref(info));
    } catch (e) {
      setRegErr(e instanceof ApiError ? t("regError") : t("regError"));
    } finally {
      setRegBusy(false);
    }
  }

  if (err && !info) {
    return (
      <main>
        <BrandedShell brandTag={t("brandTag")}>
          <h1>{t("fallbackTitle")}</h1>
          <p className="error">{err}</p>
          <p className="meta">{t("linkInvalid")}</p>
        </BrandedShell>
      </main>
    );
  }

  if (!info) {
    return (
      <main>
        <BrandedShell brandTag={t("brandTag")}>
          <p className="meta">{t("loading")}</p>
        </BrandedShell>
      </main>
    );
  }

  const expiresAt = info.expires_at ? new Date(info.expires_at) : null;
  const isExpired = expiresAt !== null && expiresAt.getTime() < Date.now();
  const noUsesLeft = info.uses_remaining !== null && info.uses_remaining <= 0;
  // Patient-scoped shares carry no title: the backend withholds the
  // patient name from this pre-auth endpoint on purpose, so fall back
  // to a label that says what the link is without saying whose it is.
  const studyTitle =
    info.study_title ||
    t(info.resource_kind === "patient" ? "fallbackTitleRecord" : "fallbackTitle");
  const grantor = info.grantor_display?.trim() || t("fallbackGrantor");
  const sizeSuffix = info.total_bytes != null ? ` · ${formatBytes(info.total_bytes)}` : "";

  const prepStatus = info.prepared_status ?? null;
  const prepReady = prepStatus === "succeeded";
  const prepFailed = prepStatus === "failed" || prepStatus === "cancelled";
  const prepActive = prepStatus === "queued" || prepStatus === "running";
  const prepTotal = info.prepared_progress_total ?? 0;
  const prepDone = info.prepared_progress_done ?? 0;
  const prepPct = prepTotal > 0 ? Math.min(100, Math.floor((prepDone / prepTotal) * 100)) : null;
  const canDirectDownload = prepReady && !info.requires_password && !isExpired && !noUsesLeft;
  const directDownloadUrl = `${API_BASE_URL}/api/shared/${params.token}/download`;
  const goLabel =
    info.resource_kind === "patient" ? t("goToResourcePatient") : t("goToResourceStudy");
  const loginBindHref = `/login?then=bind&token=${encodeURIComponent(params.token)}&next=${encodeURIComponent(resourceHref(info))}`;

  // ---- POST-OPEN INTERSTITIAL ------------------------------------------
  if (verified) {
    return (
      <main>
        <BrandedShell brandTag={t("brandTag")}>
          <div
            aria-live="polite"
            style={{
              border: "1px solid var(--bv-success, #047857)",
              background: "var(--bv-success-soft, #ecfdf5)",
              borderRadius: 8,
              padding: "0.9rem 1rem",
              marginBottom: "1rem",
            }}
          >
            <strong style={{ fontSize: "1.05rem" }}>✓ {t("accessGranted")}</strong>
            <div className="meta" style={{ marginTop: "0.25rem" }}>
              {t("receiptAuto")}
            </div>
          </div>

          <button
            type="button"
            onClick={() => router.push(resourceHref(info))}
            style={{
              width: "100%",
              background: "#111",
              color: "#fff",
              border: "none",
              borderRadius: 8,
              padding: "0.8rem 1rem",
              fontSize: "1.05rem",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            {goLabel} →
          </button>

          {/* Keep-access card. Hidden once the link is already on an
              account (neither claimable nor bindable). */}
          {(info.claimable || info.bindable || emailExists) && (
            <div
              className="card"
              style={{
                marginTop: "1rem",
                borderColor: "var(--bv-accent, #2563eb)",
                background: "var(--bv-accent-soft, #eff6ff)",
              }}
            >
              <h3 style={{ marginTop: 0, marginBottom: "0.25rem" }}>{t("keepAccessTitle")}</h3>
              <p className="meta" style={{ fontSize: "0.9rem", marginTop: 0 }}>
                {t("keepAccessBody")}
              </p>

              {/* Existing account → login + bind. */}
              {(info.bindable || emailExists) && (
                <>
                  {emailExists && (
                    <p className="meta" style={{ fontSize: "0.85rem" }}>
                      {t("emailExistsBody")}
                    </p>
                  )}
                  <a
                    href={loginBindHref}
                    style={{
                      display: "block",
                      textAlign: "center",
                      background: "var(--bv-accent, #2563eb)",
                      color: "#fff",
                      borderRadius: 6,
                      padding: "0.55rem 0.9rem",
                      textDecoration: "none",
                      fontWeight: 500,
                      marginTop: "0.5rem",
                    }}
                  >
                    {t("loginAndConnect")}
                  </a>
                </>
              )}

              {/* No account yet → inline create-account form. */}
              {info.claimable &&
                !emailExists &&
                (!regOpen ? (
                  <button
                    type="button"
                    onClick={() => setRegOpen(true)}
                    style={{
                      width: "100%",
                      marginTop: "0.5rem",
                      background: "var(--bv-accent, #2563eb)",
                      color: "#fff",
                      border: "none",
                      borderRadius: 6,
                      padding: "0.55rem 0.9rem",
                      fontWeight: 500,
                      cursor: "pointer",
                    }}
                  >
                    {t("createAccountCta")}
                  </button>
                ) : (
                  <form onSubmit={(e) => void handleRegister(e)} style={{ marginTop: "0.5rem" }}>
                    {!info.recipient_email_known && (
                      <label style={{ display: "block", marginBottom: "0.5rem" }}>
                        <span className="meta">{t("regEmailLabel")}</span>
                        <input
                          type="email"
                          required
                          value={regEmail}
                          onChange={(e) => setRegEmail(e.target.value)}
                          placeholder={t("regEmailPlaceholder")}
                          style={{ width: "100%" }}
                        />
                      </label>
                    )}
                    <label style={{ display: "block", marginBottom: "0.5rem" }}>
                      <span className="meta">{t("regNameLabel")}</span>
                      <input
                        value={regName}
                        onChange={(e) => setRegName(e.target.value)}
                        style={{ width: "100%" }}
                      />
                    </label>
                    <label style={{ display: "block", marginBottom: "0.5rem" }}>
                      <span className="meta">{t("regPasswordLabel")}</span>
                      <input
                        type="password"
                        required
                        minLength={8}
                        value={regPassword}
                        onChange={(e) => setRegPassword(e.target.value)}
                        style={{ width: "100%" }}
                      />
                    </label>
                    <button
                      type="submit"
                      disabled={regBusy}
                      style={{
                        width: "100%",
                        background: "var(--bv-accent, #2563eb)",
                        color: "#fff",
                        border: "none",
                        borderRadius: 6,
                        padding: "0.55rem 0.9rem",
                        fontWeight: 500,
                        cursor: regBusy ? "wait" : "pointer",
                      }}
                    >
                      {regBusy ? t("regBusy") : t("regSubmit")}
                    </button>
                    {regErr && (
                      <p className="error" style={{ fontSize: "0.85rem", marginTop: "0.4rem" }}>
                        {regErr}
                      </p>
                    )}
                  </form>
                ))}
            </div>
          )}

          <p className="meta" style={{ marginTop: "0.75rem", fontSize: "0.8rem" }}>
            {t("skipForNow")}
          </p>
          <footer
            style={{
              marginTop: "1rem",
              paddingTop: "0.75rem",
              borderTop: "1px solid var(--border, #e5e7eb)",
              fontSize: "0.78rem",
              opacity: 0.7,
            }}
          >
            {t("footerAuditNote")}
          </footer>
        </BrandedShell>
      </main>
    );
  }

  // ---- LANDING ----------------------------------------------------------
  return (
    <main>
      <BrandedShell brandTag={t("brandTag")}>
        <header style={{ marginBottom: "1rem" }}>
          <h1 style={{ marginBottom: "0.25rem" }}>{studyTitle}</h1>
          <p className="meta">{t("sharedBy", { grantor })}</p>
        </header>

        {info.deidentified ? (
          <div
            role="note"
            style={{
              border: "1px solid var(--bv-success, #047857)",
              background: "var(--bv-success-soft, #ecfdf5)",
              borderRadius: 6,
              padding: "0.5rem 0.75rem",
              marginBottom: "0.75rem",
              fontSize: "0.85rem",
            }}
          >
            <strong>{t("deidentifiedBannerTitle")}</strong> {t("deidentifiedBannerBody")}
          </div>
        ) : (
          <div
            role="note"
            style={{
              border: "1px solid var(--bv-warning, #b45309)",
              background: "var(--bv-warning-soft, #fef3c7)",
              borderRadius: 6,
              padding: "0.5rem 0.75rem",
              marginBottom: "0.75rem",
              fontSize: "0.85rem",
            }}
          >
            <strong>{t("originalBannerTitle")}</strong> {t("originalBannerBody")}
          </div>
        )}

        <div className="card">
          <div className="badges" style={{ marginBottom: "0.5rem" }}>
            {info.modalities.map((m) => (
              <span key={m} className="badge">
                {m}
              </span>
            ))}
          </div>
          {info.study_date && (
            <div className="meta">
              {t("studyDate")}: {info.study_date}
            </div>
          )}
          {(info.total_files != null || info.total_bytes != null) && (
            <div className="meta" style={{ marginTop: "0.35rem" }}>
              {t("contentSummary", { files: info.total_files ?? 0, size: sizeSuffix })}
            </div>
          )}
          {expiresAt && (
            <div className="meta" style={{ marginTop: "0.35rem" }}>
              {isExpired ? t("expiredOn") : t("expiresOn")}: {expiresAt.toLocaleString()}
            </div>
          )}
          {info.max_uses !== null && (
            <div className="meta" style={{ marginTop: "0.35rem" }}>
              {t("usesRemaining", { remaining: info.uses_remaining ?? 0, max: info.max_uses })}
            </div>
          )}
          {prepActive && (
            <div className="meta" style={{ marginTop: "0.5rem" }}>
              <div style={{ marginBottom: "0.25rem" }}>
                {t("prepRunning", { done: prepDone, total: prepTotal || 0 })}
              </div>
              <div
                aria-hidden
                style={{
                  width: "100%",
                  height: 6,
                  borderRadius: 3,
                  background: "var(--bv-card-border, #e5e7eb)",
                  overflow: "hidden",
                  position: "relative",
                }}
              >
                <div
                  style={{
                    position: "absolute",
                    inset: 0,
                    width: prepPct != null ? `${prepPct}%` : "30%",
                    background: "var(--bv-accent, #2563eb)",
                    transition: "width 0.4s ease",
                  }}
                />
              </div>
            </div>
          )}
          {prepFailed && (
            <div
              className="meta"
              style={{ marginTop: "0.5rem", color: "var(--bv-danger, #b91c1c)" }}
            >
              ⚠ {t("prepFailed")}
            </div>
          )}
        </div>

        {isExpired || noUsesLeft ? (
          <p className="error" style={{ marginTop: "0.75rem" }}>
            {t("linkNoLongerValid")}
          </p>
        ) : info.requires_password ? (
          <form onSubmit={(e) => void handleSubmitPassword(e)} style={{ marginTop: "0.9rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem" }}>
              <span className="meta">{t("passwordRequired")}</span>
              <input
                ref={passwordInputRef}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={openBusy}
                placeholder={t("passwordPlaceholder")}
                style={{ width: "100%", marginTop: "0.3rem" }}
              />
            </label>
            <button
              type="submit"
              disabled={openBusy || !password}
              style={{
                width: "100%",
                background: "#111",
                color: "#fff",
                border: "none",
                borderRadius: 8,
                padding: "0.8rem 1rem",
                fontSize: "1.05rem",
                fontWeight: 600,
                cursor: openBusy || !password ? "not-allowed" : "pointer",
                opacity: !password ? 0.6 : 1,
              }}
            >
              {openBusy ? t("opening") : t("openButton")}
            </button>
          </form>
        ) : (
          <button
            type="button"
            onClick={() => void runVerify(null)}
            disabled={openBusy}
            style={{
              width: "100%",
              marginTop: "0.9rem",
              background: "#111",
              color: "#fff",
              border: "none",
              borderRadius: 8,
              padding: "0.8rem 1rem",
              fontSize: "1.1rem",
              fontWeight: 600,
              cursor: openBusy ? "wait" : "pointer",
            }}
          >
            {openBusy ? t("opening") : t("openButton")}
          </button>
        )}

        {/* Secondary: direct ZIP download (no-password links only). */}
        {!info.requires_password && !isExpired && !noUsesLeft && (
          <a
            href={canDirectDownload ? directDownloadUrl : undefined}
            aria-disabled={!canDirectDownload}
            onClick={(e) => {
              if (!canDirectDownload) e.preventDefault();
            }}
            style={{
              display: "block",
              textAlign: "center",
              marginTop: "0.5rem",
              padding: "0.5rem 0.9rem",
              borderRadius: 6,
              border: "1px solid var(--bv-card-border, #e5e7eb)",
              background: "var(--bv-card-bg, #fff)",
              color: "var(--bv-fg-soft, #475569)",
              textDecoration: "none",
              cursor: canDirectDownload ? "pointer" : "not-allowed",
              opacity: canDirectDownload ? 1 : 0.6,
              fontSize: "0.9rem",
            }}
          >
            {prepReady
              ? t("downloadDicomButton")
              : prepActive
                ? t("downloadDicomPreparing")
                : prepFailed
                  ? t("downloadDicomUnavailable")
                  : t("downloadDicomButton")}
          </a>
        )}

        {verifyErr && (
          <p className="error" style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>
            {verifyErr}
          </p>
        )}

        <footer
          style={{
            marginTop: "1.25rem",
            paddingTop: "0.75rem",
            borderTop: "1px solid var(--border, #e5e7eb)",
            fontSize: "0.78rem",
            opacity: 0.7,
          }}
        >
          {t("footerAuditNote")}
        </footer>
      </BrandedShell>
    </main>
  );
}

function BrandedShell({
  brandTag,
  children,
}: {
  brandTag: string;
  children: React.ReactNode;
}) {
  return (
    <div className="form" style={{ maxWidth: 640 }}>
      <div
        style={{
          fontSize: "0.78rem",
          opacity: 0.6,
          letterSpacing: "0.05em",
          marginBottom: "0.5rem",
          textTransform: "uppercase",
        }}
      >
        {brandTag}
      </div>
      {children}
    </div>
  );
}
