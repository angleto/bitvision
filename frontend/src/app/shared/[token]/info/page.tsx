"use client";

// Public landing page for a share link. Unlike the sibling verify page,
// this route only describes the share — it never issues a JWT, so we
// call the info endpoint directly without the stored bearer token.
//
// Audience: a clinician (often external to the institution) who just
// got an email saying "studio condiviso da X". Three goals:
//   1. Reduce phishing-suspicion. Show the grantor by name, the
//      institution chrome, the audit-trail commitment.
//   2. Set expectations for the download. File count + size + whether
//      PHI is stripped, all visible BEFORE the recipient clicks.
//   3. Close the audit chain. A "I received it" affordance writes a
//      ``share_receipt_confirmed`` row so the grantor knows the
//      message arrived (decouples receipt confirmation from access:
//      the consultant may want to confirm receipt before they have
//      time to actually open the study).
//
// Locale: pulls IT/EN from the shared messages catalogue via next-intl.
// The platform's locale precedence (cookie → Accept-Language → IT
// default) drives copy, so a recipient on an English-speaking browser
// gets EN even though the email was sent by an Italian grantor.

import { useTranslations } from "next-intl";
import { useParams, useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { API_BASE_URL, ApiError, setStoredToken } from "@/lib/api";

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
  recipient_name?: string | null;
  recipient_email?: string | null;
  deidentified?: boolean;
  total_files?: number | null;
  total_bytes?: number | null;
  grantor_display?: string | null;
  /** Pre-export job state. Drives the recipient-side progress bar
   *  and the "Scarica DICOM" button: enabled only when the cached
   *  artifact is ``succeeded`` (and the link doesn't require a
   *  password — password-protected links still go through verify). */
  prepared_status?: string | null;
  prepared_progress_done?: number | null;
  prepared_progress_total?: number | null;
}

const PREP_TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const PREP_POLL_MS = 4000;

async function fetchInfo(token: string): Promise<ShareInfo> {
  const resp = await fetch(`${API_BASE_URL}/api/shared/${token}/info`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!resp.ok) {
    let detail: unknown;
    try {
      detail = await resp.json();
    } catch {
      detail = await resp.text();
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as ShareInfo;
}

async function confirmReceipt(token: string): Promise<{ received_at: string }> {
  const resp = await fetch(`${API_BASE_URL}/api/shared/${token}/confirm-receipt`, {
    credentials: "include",
    method: "POST",
    cache: "no-store",
  });
  if (!resp.ok) {
    let detail: unknown;
    try {
      detail = await resp.json();
    } catch {
      detail = await resp.text();
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as { received_at: string };
}

/**
 * Map a backend ``{reason: "..."}`` detail to a doctor-friendly
 * localised string. The recipient lands here from a chat / email /
 * SMS without context — "404 not found" alone is useless. Distinct
 * codes let us tell them what happened (revoked / expired / used up
 * already) so they can decide whether to retry the URL or ask the
 * grantor for a fresh link.
 *
 * Falls back to the generic "linkNotFoundFallback" string when the
 * detail shape is unexpected (legacy backend response, network
 * error, JSON parse failure).
 */
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

export default function SharedInfoPage() {
  const t = useTranslations("sharedLanding");
  const params = useParams<{ token: string }>();
  const router = useRouter();
  const [info, setInfo] = useState<ShareInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [receiptBusy, setReceiptBusy] = useState(false);
  const [receiptDone, setReceiptDone] = useState<string | null>(null);
  const [receiptErr, setReceiptErr] = useState<string | null>(null);
  // Inline verify state. Folding the verify call into this page
  // collapses the previous two-hop UX (land on /info → click "Open
  // Study" → land on legacy verify form → click again) into a
  // single click for passwordless shares, and into a single
  // password-input + submit for password-protected shares. The
  // legacy /shared/{token} route still exists for back-compat with
  // already-emailed links (it now redirects here on mount).
  const [openBusy, setOpenBusy] = useState(false);
  const [password, setPassword] = useState("");
  const [verifyErr, setVerifyErr] = useState<string | null>(null);
  const passwordInputRef = useRef<HTMLInputElement | null>(null);
  // Auto-focus the password field once /info has loaded and the
  // share is password-protected. We use a ref + effect rather than
  // ``autoFocus`` so the lint rule stays clean and the focus only
  // fires after the form is actually rendered (not during the
  // ``Loading…`` placeholder pass).
  useEffect(() => {
    if (info?.requires_password && passwordInputRef.current) {
      passwordInputRef.current.focus();
    }
  }, [info?.requires_password]);

  useEffect(() => {
    fetchInfo(params.token)
      .then(setInfo)
      .catch((e) => setErr(extractErrReason(e, t)));
  }, [params.token, t]);

  // Adaptive polling: while the cached export is still queued or
  // running, refresh /info every PREP_POLL_MS so the progress bar
  // moves under the recipient's eye and the download button flips
  // to "ready" without a manual reload. Stops as soon as the prep
  // hits any terminal state (succeeded / failed / cancelled).
  useEffect(() => {
    if (!info) return;
    const status = info.prepared_status;
    if (!status || PREP_TERMINAL.has(status)) return;
    const handle = window.setInterval(() => {
      fetchInfo(params.token)
        .then(setInfo)
        .catch(() => {
          // Transient errors stay silent — the next tick retries.
          // A persistent failure is surfaced by the next manual
          // navigation or by /verify.
        });
    }, PREP_POLL_MS);
    return () => window.clearInterval(handle);
  }, [info, params.token]);

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
      if (!resp.ok) {
        let detail: unknown;
        try {
          detail = await resp.json();
        } catch {
          detail = await resp.text();
        }
        throw new ApiError(resp.status, detail);
      }
      const out = (await resp.json()) as { access_token: string; expires_in: number };
      setStoredToken(out.access_token);
      // Route the recipient to the destination they came for. The
      // info-page metadata already told us resource_kind/id so we
      // can deep-link without a follow-up fetch.
      if (info?.resource_kind === "patient") {
        router.push(`/patients/${info.resource_id}`);
      } else if (info?.resource_kind === "study") {
        router.push(`/studies/${info.resource_id}`);
      } else if (info?.resource_kind === "folder") {
        // Folder shares haven't got a dedicated public viewer yet
        // (the cached ZIP is the deliverable). Bounce to the
        // patient page if we know it; otherwise fall back to the
        // generic studies list.
        router.push("/studies");
      } else {
        router.push("/studies");
      }
    } catch (e) {
      // Reuse the same granular extractErrReason mapping as the
      // info-fetch error path so password-protected wrong-password
      // shows "Password errata" (not "HTTP 401").
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

  async function handleConfirm(): Promise<void> {
    setReceiptBusy(true);
    setReceiptErr(null);
    try {
      const out = await confirmReceipt(params.token);
      setReceiptDone(out.received_at);
    } catch (e) {
      setReceiptErr(e instanceof ApiError ? e.message : t("confirmReceiptError"));
    } finally {
      setReceiptBusy(false);
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
  const studyTitle = info.study_title || t("fallbackTitle");
  const grantor = info.grantor_display?.trim() || t("fallbackGrantor");
  const sizeSuffix = info.total_bytes != null ? ` · ${formatBytes(info.total_bytes)}` : "";

  // Pre-export state-machine: the recipient gets a direct download
  // button only when (a) no password gates the link AND (b) the
  // cached export is ready. Password-protected links still route to
  // /verify, after which the bytes are reachable via the standard
  // job-result path inside the study viewer.
  const prepStatus = info.prepared_status ?? null;
  const prepReady = prepStatus === "succeeded";
  const prepFailed = prepStatus === "failed" || prepStatus === "cancelled";
  const prepActive = prepStatus === "queued" || prepStatus === "running";
  const prepTotal = info.prepared_progress_total ?? 0;
  const prepDone = info.prepared_progress_done ?? 0;
  const prepPct = prepTotal > 0 ? Math.min(100, Math.floor((prepDone / prepTotal) * 100)) : null;
  const canDirectDownload = prepReady && !info.requires_password && !isExpired && !noUsesLeft;
  const directDownloadUrl = `${API_BASE_URL}/api/shared/${params.token}/download`;

  return (
    <main>
      <BrandedShell brandTag={t("brandTag")}>
        <header style={{ marginBottom: "1rem" }}>
          <h1 style={{ marginBottom: "0.25rem" }}>{studyTitle}</h1>
          <p className="meta">
            {t("sharedBy", { grantor })}
            {info.recipient_name ? (
              <> {t("sharedWith", { recipient: info.recipient_name })}</>
            ) : null}
          </p>
        </header>

        {/* Privacy banner. Pseudonymization is a strong claim worth
            calling out — a consultant who skims the page should know
            within 1 second whether to expect the patient's real name
            on the DICOM headers. */}
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

          {/* Pre-flight payload: if the recipient is on a phone tether,
              "12 file, 350 MB" prevents an unwanted multi-GB pull. */}
          {(info.total_files != null || info.total_bytes != null) && (
            <div className="meta" style={{ marginTop: "0.35rem" }}>
              {t("contentSummary", {
                files: info.total_files ?? 0,
                size: sizeSuffix,
              })}
            </div>
          )}

          <div className="meta" style={{ marginTop: "0.35rem" }}>
            {t("permissions")}:{" "}
            {info.permissions.length > 0
              ? info.permissions.map(permissionLabel).join(", ")
              : t("permissionsNone")}
          </div>

          {expiresAt && (
            <div className="meta" style={{ marginTop: "0.35rem" }}>
              {isExpired ? t("expiredOn") : t("expiresOn")}: {expiresAt.toLocaleString()}
            </div>
          )}

          {info.max_uses !== null && (
            <div className="meta" style={{ marginTop: "0.35rem" }}>
              {t("usesRemaining", {
                remaining: info.uses_remaining ?? 0,
                max: info.max_uses,
              })}
            </div>
          )}

          <div className="meta" style={{ marginTop: "0.35rem" }}>
            {info.requires_password ? t("passwordRequired") : t("passwordNotRequired")}
          </div>

          {/* Pre-export progress strip. Hidden when no prep state
              is reported (e.g. patient/folder shares) or when prep
              has reached terminal-success — at that point the
              "Scarica DICOM" button below carries the meaning. */}
          {prepActive && (
            <div className="meta" style={{ marginTop: "0.5rem" }}>
              <div style={{ marginBottom: "0.25rem" }}>
                {t("prepRunning", {
                  done: prepDone,
                  total: prepTotal || 0,
                })}
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
          {prepReady && (
            <div
              className="meta"
              style={{ marginTop: "0.5rem", color: "var(--bv-success, #047857)" }}
            >
              ✓ {t("prepReady")}
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
        ) : (
          <div
            className="actions"
            style={{
              marginTop: "0.75rem",
              display: "flex",
              gap: "0.5rem",
              flexWrap: "wrap",
              justifyContent: "flex-end",
            }}
          >
            {/* "I received it" button is independent of /verify — the
                consultant might confirm before they actually have time
                to view the study (e.g. between rounds). Closes the
                audit chain for the grantor either way. */}
            {receiptDone ? (
              <span
                style={{
                  alignSelf: "center",
                  fontSize: "0.85rem",
                  color: "var(--bv-success, #047857)",
                }}
              >
                {t("confirmReceiptDone", { when: new Date(receiptDone).toLocaleString() })}
              </span>
            ) : (
              <button
                type="button"
                onClick={handleConfirm}
                disabled={receiptBusy}
                className="ghost"
              >
                {receiptBusy ? t("confirmReceiptBusy") : t("confirmReceiptButton")}
              </button>
            )}
            {/* Direct download via the cached artifact (no-password
                shares only). Anchor click streams the ZIP straight
                to disk through the proxy — same Range/resume support
                as document downloads. Disabled visually when prep is
                still running so the recipient understands why the
                button isn't actionable yet. */}
            {!info.requires_password && (
              <a
                href={canDirectDownload ? directDownloadUrl : undefined}
                aria-disabled={!canDirectDownload}
                onClick={(e) => {
                  if (!canDirectDownload) e.preventDefault();
                }}
                style={{
                  background: canDirectDownload
                    ? "var(--bv-success, #047857)"
                    : "var(--bv-card-bg, #fff)",
                  color: canDirectDownload ? "#fff" : "var(--bv-fg-soft, #475569)",
                  border: canDirectDownload ? "none" : "1px solid var(--bv-card-border, #e5e7eb)",
                  padding: "0.5rem 0.9rem",
                  borderRadius: 6,
                  textDecoration: "none",
                  cursor: canDirectDownload ? "pointer" : "not-allowed",
                  opacity: canDirectDownload ? 1 : 0.6,
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
            {/* Inline open: one click for passwordless, password +
                submit for protected. The legacy /shared/{token}
                verify form is no longer in the user's path —
                already-emailed links to that path 307 here. */}
            {!info.requires_password && (
              <button
                type="button"
                onClick={() => void runVerify(null)}
                disabled={openBusy}
                style={{
                  background: "#111",
                  color: "#fff",
                  padding: "0.5rem 0.9rem",
                  borderRadius: 6,
                  border: "none",
                  cursor: openBusy ? "wait" : "pointer",
                  fontWeight: 500,
                }}
              >
                {openBusy ? t("opening") : t("openButton")}
              </button>
            )}
          </div>
        )}
        {info.requires_password && !isExpired && !noUsesLeft && (
          <form
            onSubmit={(e) => void handleSubmitPassword(e)}
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "0.5rem",
              alignItems: "center",
              marginTop: "0.5rem",
              background: "var(--bv-card-bg-soft, #f9fafb)",
              border: "1px solid var(--border, #e5e7eb)",
              borderRadius: 6,
              padding: "0.5rem 0.75rem",
            }}
          >
            <label
              style={{
                flex: "1 1 240px",
                display: "flex",
                flexDirection: "column",
                gap: "0.25rem",
                fontSize: "0.85rem",
              }}
            >
              <span>{t("passwordRequired")}</span>
              <input
                ref={passwordInputRef}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={openBusy}
                placeholder={t("passwordPlaceholder")}
                style={{ padding: "0.4rem 0.5rem", font: "inherit" }}
              />
            </label>
            <button
              type="submit"
              disabled={openBusy || !password}
              style={{
                background: "#111",
                color: "#fff",
                padding: "0.5rem 0.9rem",
                borderRadius: 6,
                border: "none",
                cursor: openBusy || !password ? "not-allowed" : "pointer",
                fontWeight: 500,
                opacity: !password ? 0.6 : 1,
              }}
            >
              {openBusy ? t("opening") : t("openButton")}
            </button>
          </form>
        )}
        {verifyErr && (
          <p
            className="error"
            style={{
              fontSize: "0.85rem",
              marginTop: "0.4rem",
              color: "var(--bv-danger, #b91c1c)",
            }}
          >
            {verifyErr}
          </p>
        )}
        {receiptErr && (
          <p className="error" style={{ fontSize: "0.85rem" }}>
            {receiptErr}
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
