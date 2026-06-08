"use client";

import { useTranslations } from "next-intl";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { API_BASE_URL, ApiError, getStoredToken, setStoredToken } from "@/lib/api";

interface ShareInfo {
  study_title: string | null;
  modalities: string[];
  study_date: string | null;
  requires_password: boolean;
  expires_at: string | null;
  permissions: string[];
  resource_kind: string;
  resource_id: string;
  mode?: "claim" | "anonymous";
  /** True only when the link is anonymous, has a recipient_email,
   *  the grant is alive and no claim has been done yet. */
  claimable?: boolean;
  /** True when an account already exists for the link's recipient: they
   *  sign in and attach the grant via /bind instead of creating one. */
  bindable?: boolean;
  recipient_name?: string | null;
  recipient_email?: string | null;
}

interface SharedReport {
  id: string;
  study_id: string;
  version: number;
  title: string;
  file_content_type: string | null;
  created_at: string;
}

interface SharedDocument {
  id: string;
  document_type: string;
  title: string;
  file_content_type: string | null;
  document_date: string | null;
  created_at: string;
}

interface SharedArtifacts {
  can_download: boolean;
  reports: SharedReport[];
  documents: SharedDocument[];
}

async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(init.json);
  }
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    ...init,
    headers,
    body,
  });
  if (!resp.ok) throw new ApiError(resp.status, await resp.json());
  return (await resp.json()) as T;
}

// For password-protected links the endpoint needs the verify-issued JWT
// in the Authorization header, which a plain window.open can't set — so
// we fetch the redirect ourselves and save the resulting blob. For
// passwordless links a new-tab navigation lets the browser follow the
// 307 straight to the presigned S3 URL.
async function triggerDownload(path: string): Promise<void> {
  const token = getStoredToken();
  if (!token) {
    window.open(`${API_BASE_URL}${path}`, "_blank");
    return;
  }
  const resp = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    headers: { authorization: `Bearer ${token}` },
    redirect: "follow",
  });
  if (!resp.ok) throw new ApiError(resp.status, await resp.text());
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function SharedLinkPage() {
  const params = useParams<{ token: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const tStp = useTranslations("sharedTokenPage");
  const [info, setInfo] = useState<ShareInfo | null>(null);
  const [artifacts, setArtifacts] = useState<SharedArtifacts | null>(null);
  const [verified, setVerified] = useState(false);
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The legacy ``/shared/{token}`` URL is what older emails (sent
  // before beta.118) put in front of the recipient — it was the
  // canonical share URL for years. It still ships a working verify
  // form, but the new branded ``/info`` landing is a strictly better
  // first-touch experience: privacy banner, prep status, direct
  // download CTA, doctor-friendly granular error messages. So when a
  // bare token URL hits this route, redirect to ``/info``. The
  // ``/info`` page's "Apri studio" button keeps deep-linking back
  // here with ``?verify=1`` so the verify form remains reachable
  // without a redirect loop.
  useEffect(() => {
    if (searchParams.get("verify") === "1") return;
    router.replace(`/shared/${params.token}/info`);
  }, [params.token, searchParams, router]);
  // Claim flow A→B: when ``info.claimable`` is true the recipient
  // can convert the link into a real account in one step. This page
  // hosts both the claim form (preferred) and the legacy guest-verify
  // form below; the user picks via a small toggle.
  const [claimOpen, setClaimOpen] = useState(false);
  const [claimPassword, setClaimPassword] = useState("");
  const [claimDisplayName, setClaimDisplayName] = useState("");
  const [downloadsOpen, setDownloadsOpen] = useState(false);
  // Ref for the password input so the top "Open study" button can
  // bring it into view and focus it on password-protected links — the
  // user reported having to scroll past downloads + claim card to
  // find the action, and a duplicate top button has no value if it
  // can't actually open the link without the password.
  const passwordInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    api<ShareInfo>(`/api/shared/${params.token}/info`)
      .then(setInfo)
      .catch((e) => setErr(e instanceof ApiError ? e.message : "link not found or expired"));
    api<SharedArtifacts>(`/api/shared/${params.token}/artifacts`)
      .then(setArtifacts)
      .catch(() => {
        // Non-fatal: info errors already cover expired/revoked cases.
      });
  }, [params.token]);

  async function runVerify(): Promise<void> {
    setBusy(true);
    setErr(null);
    try {
      const { access_token } = await api<{ access_token: string; expires_in: number }>(
        `/api/shared/${params.token}/verify`,
        { method: "POST", json: { password: password || null } },
      );
      setStoredToken(access_token);
      setVerified(true);
      // If the link carries downloadable artifacts, stay on this page so
      // the user can grab them with the freshly-minted session JWT.
      // Otherwise redirect to the studies list so the visibility filter
      // surfaces the share.
      // Always redirect after a successful verify. The recipient came
      // for the patient / study, not for this landing page; downloads
      // are reachable from the destination (patient docs are listed in
      // the fascicolo, study reports are inline on the study detail).
      // Previously a `hasDownloads` branch kept users stuck here when
      // the share allowed download — the "Open study" button looked
      // broken because it never navigated anywhere visible.
      if (info?.resource_kind === "patient") {
        router.push(`/patients/${info.resource_id}`);
      } else if (info?.resource_kind === "study") {
        router.push(`/studies/${info.resource_id}`);
      } else {
        router.push("/studies");
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "verification failed");
    } finally {
      setBusy(false);
    }
  }

  async function onVerify(e: FormEvent) {
    e.preventDefault();
    await runVerify();
  }

  // Top "Open study" button: for passwordless links submit directly;
  // for password-protected links bring the password input into view
  // and focus it (the user can't authenticate without the password
  // anyway, but they no longer have to scroll past downloads / claim
  // card to find the action).
  function onOpenFromTop(): void {
    if (info?.requires_password && !verified) {
      const input = passwordInputRef.current;
      if (input) {
        input.scrollIntoView({ behavior: "smooth", block: "center" });
        input.focus();
      }
      return;
    }
    void runVerify();
  }

  async function onClaim(e: FormEvent) {
    e.preventDefault();
    if (claimPassword.length < 8) {
      setErr(tStp("claimErrorPasswordTooShort"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const result = await api<{
        subject_id: string;
        email: string;
        access_token: string;
        expires_in: number;
      }>(`/api/share-links/${params.token}/claim`, {
        method: "POST",
        json: {
          password: claimPassword,
          display_name: claimDisplayName || null,
        },
      });
      setStoredToken(result.access_token);
      // After claim the user is a regular signed-in account holder;
      // route them to the resource they were given access to.
      if (info?.resource_kind === "patient") {
        router.push(`/patients/${info.resource_id}`);
      } else if (info?.resource_kind === "study") {
        router.push(`/studies/${info.resource_id}`);
      } else {
        router.push("/studies");
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "claim failed");
    } finally {
      setBusy(false);
    }
  }

  async function onBind() {
    setBusy(true);
    setErr(null);
    try {
      // Caller is signed in (button is gated on getStoredToken); the api
      // wrapper attaches the bearer. Backend repoints the PUBLIC-held
      // grant onto this account, gated on email == recipient_email.
      await api(`/api/share-links/${params.token}/bind`, { method: "POST" });
      if (info?.resource_kind === "patient") {
        router.push(`/patients/${info.resource_id}`);
      } else if (info?.resource_kind === "study") {
        router.push(`/studies/${info.resource_id}`);
      } else {
        router.push("/studies");
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "bind failed");
    } finally {
      setBusy(false);
    }
  }

  async function onDownloadReport(reportId: string): Promise<void> {
    try {
      await triggerDownload(`/api/shared/${params.token}/reports/${reportId}/download`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "download failed");
    }
  }

  async function onDownloadDocument(docId: string): Promise<void> {
    try {
      await triggerDownload(`/api/shared/${params.token}/documents/${docId}/download`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "download failed");
    }
  }

  if (err && !info) {
    return (
      <main>
        <div className="form">
          <h1>Shared study</h1>
          <p className="error">{err}</p>
          <p className="meta">This link may be expired, revoked, or invalid.</p>
        </div>
      </main>
    );
  }

  if (!info) {
    return (
      <main>
        <div className="form">
          <p className="meta">Loading…</p>
        </div>
      </main>
    );
  }

  return (
    <main>
      <div className="form">
        <h1>{info.study_title || "Shared study"}</h1>
        <div className="card" style={{ overflow: "hidden" }}>
          <div
            className="badges"
            style={{
              marginBottom: "0.5rem",
              display: "flex",
              flexWrap: "wrap",
              gap: "0.3rem",
              maxWidth: "100%",
            }}
          >
            {info.modalities.map((m) => (
              <span key={m} className="badge">
                {m}
              </span>
            ))}
          </div>
          {info.study_date && <div className="meta">Date: {info.study_date}</div>}
          {info.expires_at && (
            <div className="meta">Expires: {new Date(info.expires_at).toLocaleDateString()}</div>
          )}
          <div className="meta" style={{ marginTop: "0.35rem" }}>
            Access: {info.permissions.map((p) => p.split(":")[1] || p).join(", ")}
          </div>
          <button
            type="button"
            onClick={onOpenFromTop}
            disabled={busy}
            style={{ width: "100%", marginTop: "0.75rem" }}
          >
            {busy ? "…" : info.requires_password && !verified ? "Access Record" : "Open Record"}
          </button>
        </div>

        {artifacts?.can_download &&
          (artifacts.reports.length > 0 || artifacts.documents.length > 0) && (
            <div className="card" style={{ marginTop: "0.75rem" }}>
              <button
                type="button"
                onClick={() => setDownloadsOpen((v) => !v)}
                aria-expanded={downloadsOpen}
                aria-controls="shared-downloads-panel"
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  background: "transparent",
                  border: 0,
                  padding: 0,
                  cursor: "pointer",
                  font: "inherit",
                  color: "inherit",
                }}
              >
                <h3 style={{ margin: 0 }}>
                  Downloads ({artifacts.reports.length + artifacts.documents.length})
                </h3>
                <span aria-hidden style={{ fontSize: "0.9rem" }}>
                  {downloadsOpen ? "▾" : "▸"}
                </span>
              </button>
              {downloadsOpen && (
                <div id="shared-downloads-panel" style={{ marginTop: "0.6rem" }}>
                  {artifacts.reports.length > 0 && (
                    <div style={{ marginBottom: "0.5rem" }}>
                      <div className="meta" style={{ marginBottom: "0.3rem" }}>
                        Reports
                      </div>
                      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                        {artifacts.reports.map((r) => (
                          <li
                            key={r.id}
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              padding: "0.3rem 0",
                            }}
                          >
                            <span>
                              v{r.version} — {r.title}
                            </span>
                            <button type="button" onClick={() => onDownloadReport(r.id)}>
                              Download
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {artifacts.documents.length > 0 && (
                    <div>
                      <div className="meta" style={{ marginBottom: "0.3rem" }}>
                        Patient documents
                      </div>
                      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                        {artifacts.documents.map((d) => (
                          <li
                            key={d.id}
                            style={{
                              display: "flex",
                              justifyContent: "space-between",
                              alignItems: "center",
                              padding: "0.3rem 0",
                            }}
                          >
                            <span>
                              [{d.document_type}] {d.title}
                            </span>
                            <button type="button" onClick={() => onDownloadDocument(d.id)}>
                              Download
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {info.requires_password && !verified && (
                    <p className="meta" style={{ marginTop: "0.5rem" }}>
                      Enter the password below to enable downloads.
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

        {err && <p className="error">{err}</p>}

        {info.claimable && (
          <div
            className="card"
            style={{
              marginTop: "0.75rem",
              borderColor: "var(--bv-accent)",
              background: "var(--bv-accent-soft)",
            }}
          >
            <h3 style={{ marginTop: 0 }}>{tStp("claimTitle")}</h3>
            <p className="meta" style={{ fontSize: "0.85rem" }}>
              {tStp("claimIntro", { email: info.recipient_email ?? "" })}
            </p>
            {!claimOpen ? (
              <button type="button" onClick={() => setClaimOpen(true)} style={{ width: "100%" }}>
                {tStp("claimOpen")}
              </button>
            ) : (
              <form onSubmit={onClaim}>
                <label style={{ display: "block", marginBottom: "0.5rem" }}>
                  <span className="meta">{tStp("claimDisplayNameLabel")}</span>
                  <input
                    value={claimDisplayName}
                    onChange={(e) => setClaimDisplayName(e.target.value)}
                    placeholder={info.recipient_name ?? ""}
                    style={{ width: "100%" }}
                  />
                </label>
                <label style={{ display: "block", marginBottom: "0.5rem" }}>
                  <span className="meta">{tStp("claimPasswordLabel")}</span>
                  <input
                    type="password"
                    required
                    minLength={8}
                    value={claimPassword}
                    onChange={(e) => setClaimPassword(e.target.value)}
                    style={{ width: "100%" }}
                  />
                </label>
                <div
                  style={{
                    display: "flex",
                    gap: "0.4rem",
                    justifyContent: "flex-end",
                    marginTop: "0.5rem",
                  }}
                >
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => setClaimOpen(false)}
                    disabled={busy}
                  >
                    {tStp("cancel")}
                  </button>
                  <button type="submit" disabled={busy}>
                    {busy ? tStp("submitBusy") : tStp("createAccount")}
                  </button>
                </div>
              </form>
            )}
          </div>
        )}

        {info.bindable && (
          <div
            className="card"
            style={{
              marginTop: "0.75rem",
              borderColor: "var(--bv-accent)",
              background: "var(--bv-accent-soft)",
            }}
          >
            <h3 style={{ marginTop: 0 }}>{tStp("bindTitle")}</h3>
            <p className="meta" style={{ fontSize: "0.85rem" }}>
              {tStp("bindIntro")}
            </p>
            {getStoredToken() ? (
              <button type="button" onClick={onBind} disabled={busy} style={{ width: "100%" }}>
                {busy ? tStp("submitBusy") : tStp("bindButton")}
              </button>
            ) : (
              <a
                href={`/login?redirect=${encodeURIComponent(`/shared/${params.token}`)}`}
                style={{ display: "block", textAlign: "center" }}
              >
                {tStp("bindLoginPrompt")}
              </a>
            )}
          </div>
        )}

        {info.requires_password ? (
          <form onSubmit={onVerify}>
            <label>
              This link is password-protected
              <input
                ref={passwordInputRef}
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter password"
                style={{ width: "100%", marginTop: "0.3rem" }}
              />
            </label>
            <div className="actions" style={{ marginTop: "0.75rem" }}>
              <span />
              <button type="submit" disabled={busy}>
                {busy ? "…" : "Access Record"}
              </button>
            </div>
          </form>
        ) : (
          <form onSubmit={onVerify}>
            <button type="submit" disabled={busy} style={{ width: "100%", marginTop: "0.75rem" }}>
              {busy ? "…" : "Open Record"}
            </button>
          </form>
        )}
      </div>
    </main>
  );
}
