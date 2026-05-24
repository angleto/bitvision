"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { API_BASE_URL, ApiError, getStoredToken } from "@/lib/api";

type ShareMode = "claim" | "anonymous";

interface ShareLink {
  id: string;
  token: string;
  url: string;
  label: string | null;
  permissions: string[];
  expires_at: string | null;
  revoked: boolean;
  use_count: number;
  max_uses: number | null;
  requires_password: boolean;
  created_at: string;
  mode?: ShareMode;
  recipient_name?: string | null;
  recipient_email?: string | null;
  recipient_phone?: string | null;
  /** Returned ONCE on POST when ``autogen_password`` is requested. */
  generated_password?: string | null;
}

interface Props {
  studyId: string;
  isOwner: boolean;
}

const PRESETS: Record<string, string[]> = {
  "View only": ["read:metadata", "read:pixels", "read:annotations"],
  Consultation: [
    "read:metadata",
    "read:pixels",
    "read:annotations",
    "write:annotations",
    "write:report",
  ],
  Download: [
    "read:metadata",
    "read:pixels",
    "read:annotations",
    "download:dicom",
    "download:derivative",
  ],
  "Full access": [
    "read:metadata",
    "read:pixels",
    "read:annotations",
    "write:annotations",
    "write:report",
    "run:llm",
    "download:dicom",
    "download:derivative",
    "share",
  ],
};

const EXPIRY_OPTIONS: Record<string, number | null> = {
  "24 hours": 24,
  "7 days": 168,
  "30 days": 720,
  "90 days": 2160,
  Never: null,
};

async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getStoredToken();
  if (token) headers.set("authorization", `Bearer ${token}`);
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
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export default function ShareDialog({ studyId, isOwner }: Props) {
  const tShare = useTranslations("share");
  const [links, setLinks] = useState<ShareLink[]>([]);
  const [preset, setPreset] = useState("View only");
  const [expiry, setExpiry] = useState("7 days");
  const [password, setPassword] = useState("");
  const [label, setLabel] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  // A vs B mode + recipient capture for the link-as-API-key flow.
  // ``claim`` is the default. Switching to ``anonymous`` reveals a
  // blocking warning and the recipient form; the backend rejects
  // anonymous payloads missing recipient_name + (email or phone).
  const [mode, setMode] = useState<ShareMode>("claim");
  const [anonAck, setAnonAck] = useState(false);
  const [recipientName, setRecipientName] = useState("");
  const [recipientEmail, setRecipientEmail] = useState("");
  const [recipientPhone, setRecipientPhone] = useState("");
  const [autogenPassword, setAutogenPassword] = useState(false);
  // Plaintext password the server generated when ``autogen_password``
  // was requested. Shown once via a sticky banner; the user must
  // capture it because it can never be retrieved again.
  const [generatedPassword, setGeneratedPassword] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!isOwner) return;
    try {
      setLinks(await api<ShareLink[]>(`/api/studies/${studyId}/shares`));
    } catch {
      /* ignore for non-owners */
    }
  }, [studyId, isOwner]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function createLink(e: FormEvent) {
    e.preventDefault();
    if (mode === "anonymous" && !anonAck) {
      setErr(tShare("errAnonAckMissing"));
      return;
    }
    setBusy(true);
    setErr(null);
    setGeneratedPassword(null);
    try {
      const created = await api<ShareLink>(`/api/studies/${studyId}/share`, {
        method: "POST",
        json: {
          permissions: PRESETS[preset],
          expires_in_hours: EXPIRY_OPTIONS[expiry],
          password: autogenPassword ? null : password || null,
          autogen_password: autogenPassword,
          label: label || null,
          mode,
          recipient_name: recipientName.trim() || null,
          recipient_email: recipientEmail.trim() || null,
          recipient_phone: recipientPhone.trim() || null,
        },
      });
      setPassword("");
      setLabel("");
      if (created.generated_password) {
        setGeneratedPassword(created.generated_password);
      }
      // Recipient fields cleared lazily so the operator can read the
      // banner with the new password while the form data is still
      // fresh for "send another to the same person" flow.
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(linkId: string) {
    try {
      await api<void>(`/api/share-links/${linkId}`, { method: "DELETE" });
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "revoke failed");
    }
  }

  function copyUrl(link: ShareLink) {
    const url = `${window.location.origin}/shared/${link.token}`;
    navigator.clipboard.writeText(url);
    setCopied(link.id);
    setTimeout(() => setCopied(null), 2000);
  }

  if (!isOwner) return null;

  return (
    <div style={{ marginTop: "1.5rem" }}>
      <h2>Share this study</h2>
      {err && <p className="error">{err}</p>}

      {generatedPassword && (
        <div
          role="alert"
          style={{
            background: "var(--bv-warning-soft, #fef3c7)",
            color: "var(--bv-warning, #b45309)",
            border: "1px solid var(--bv-warning, #b45309)",
            borderRadius: 6,
            padding: "10px 12px",
            marginBottom: "0.6rem",
            fontSize: "0.88rem",
          }}
        >
          <strong>{tShare("generatedPasswordTitle")}</strong>
          <p style={{ margin: "0.3rem 0" }}>{tShare("generatedPasswordExplain")}</p>
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <code
              style={{
                background: "var(--bv-card-bg, #fff)",
                padding: "4px 8px",
                borderRadius: 4,
                fontSize: "0.95rem",
                fontFamily: "ui-monospace, monospace",
                userSelect: "all",
                flex: 1,
              }}
            >
              {generatedPassword}
            </code>
            <button
              type="button"
              className="ghost"
              onClick={() => {
                navigator.clipboard.writeText(generatedPassword);
              }}
            >
              {tShare("copyPassword")}
            </button>
            <button type="button" className="ghost" onClick={() => setGeneratedPassword(null)}>
              {tShare("dismiss")}
            </button>
          </div>
        </div>
      )}
      <form className="card" onSubmit={createLink}>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.4rem",
            marginBottom: "0.6rem",
            paddingBottom: "0.6rem",
            borderBottom: "1px solid var(--bv-divider, #eef0f3)",
          }}
        >
          <span className="meta">{tShare("modeLabel")}</span>
          <label style={{ display: "flex", gap: "0.5rem", alignItems: "flex-start" }}>
            <input
              type="radio"
              name="share-mode"
              value="claim"
              checked={mode === "claim"}
              onChange={() => {
                setMode("claim");
                setAnonAck(false);
              }}
              style={{ marginTop: 4 }}
            />
            <span>
              <strong>{tShare("modeClaimTitle")}</strong>
              <div className="meta" style={{ fontSize: "0.78rem" }}>
                {tShare("modeClaimHint")}
              </div>
            </span>
          </label>
          <label style={{ display: "flex", gap: "0.5rem", alignItems: "flex-start" }}>
            <input
              type="radio"
              name="share-mode"
              value="anonymous"
              checked={mode === "anonymous"}
              onChange={() => setMode("anonymous")}
              style={{ marginTop: 4 }}
            />
            <span>
              <strong>{tShare("modeAnonTitle")}</strong>
              <div className="meta" style={{ fontSize: "0.78rem" }}>
                {tShare("modeAnonHint")}
              </div>
            </span>
          </label>
          {mode === "anonymous" && (
            <div
              role="alert"
              style={{
                background: "var(--bv-danger-soft, #fef2f2)",
                color: "var(--bv-danger, #b91c1c)",
                border: "1px solid var(--bv-danger, #b91c1c)",
                borderRadius: 6,
                padding: "10px 12px",
                fontSize: "0.85rem",
              }}
            >
              <strong>{tShare("anonWarningTitle")}</strong>
              <ul style={{ margin: "0.4rem 0 0.6rem", paddingLeft: "1.2rem" }}>
                <li>{tShare("anonWarning1")}</li>
                <li>{tShare("anonWarning2")}</li>
                <li>{tShare("anonWarning3")}</li>
              </ul>
              <label
                style={{
                  display: "flex",
                  gap: "0.4rem",
                  alignItems: "center",
                  fontWeight: 500,
                }}
              >
                <input
                  type="checkbox"
                  checked={anonAck}
                  onChange={(e) => setAnonAck(e.target.checked)}
                />
                {tShare("anonAckCheckbox")}
              </label>
            </div>
          )}
        </div>
        {mode === "anonymous" && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr",
              gap: "0.5rem",
              marginBottom: "0.6rem",
            }}
          >
            <label>
              <span className="meta">{tShare("recipientNameLabel")} *</span>
              <input
                value={recipientName}
                onChange={(e) => setRecipientName(e.target.value)}
                required
                style={{ width: "100%" }}
              />
            </label>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "0.5rem",
              }}
            >
              <label>
                <span className="meta">{tShare("recipientEmailLabel")}</span>
                <input
                  type="email"
                  value={recipientEmail}
                  onChange={(e) => setRecipientEmail(e.target.value)}
                  style={{ width: "100%" }}
                />
              </label>
              <label>
                <span className="meta">{tShare("recipientPhoneLabel")}</span>
                <input
                  type="tel"
                  value={recipientPhone}
                  onChange={(e) => setRecipientPhone(e.target.value)}
                  style={{ width: "100%" }}
                />
              </label>
            </div>
            <div className="meta" style={{ fontSize: "0.78rem" }}>
              {tShare("recipientHint")}
            </div>
          </div>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}>
          <label>
            <span className="meta">Preset</span>
            <select
              value={preset}
              onChange={(e) => setPreset(e.target.value)}
              style={{ width: "100%", padding: "0.4rem" }}
            >
              {Object.keys(PRESETS).map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          <label>
            <span className="meta">Expires</span>
            <select
              value={expiry}
              onChange={(e) => setExpiry(e.target.value)}
              style={{ width: "100%", padding: "0.4rem" }}
            >
              {Object.keys(EXPIRY_OPTIONS).map((e) => (
                <option key={e}>{e}</option>
              ))}
            </select>
          </label>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "0.5rem",
            marginTop: "0.5rem",
          }}
        >
          <label>
            <span className="meta">Label (optional)</span>
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Dr. Rossi, 2nd opinion"
              style={{ width: "100%" }}
            />
          </label>
          <label>
            <span className="meta">{tShare("passwordLabel")}</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={tShare("passwordPlaceholder")}
              style={{ width: "100%" }}
              disabled={autogenPassword}
            />
          </label>
        </div>
        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.4rem",
            marginTop: "0.5rem",
            fontSize: "0.85rem",
          }}
        >
          <input
            type="checkbox"
            checked={autogenPassword}
            onChange={(e) => {
              setAutogenPassword(e.target.checked);
              if (e.target.checked) setPassword("");
            }}
          />
          <span>
            {tShare("autogenLabel")}
            <span className="meta" style={{ marginLeft: "0.4rem" }}>
              {tShare("autogenHint")}
            </span>
          </span>
        </label>
        <div style={{ marginTop: "0.75rem", display: "flex", justifyContent: "flex-end" }}>
          <button type="submit" disabled={busy || (mode === "anonymous" && !anonAck)}>
            {busy ? "…" : tShare("createLinkButton")}
          </button>
        </div>
      </form>

      {links.length > 0 && (
        <>
          <h2 style={{ marginTop: "1rem" }}>Active share links</h2>
          {links.map((link) => (
            <div
              key={link.id}
              className="card"
              style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
            >
              <div>
                <div>
                  <strong>{link.label || "(unlabeled)"}</strong>
                  {link.revoked && (
                    <span
                      className="badge"
                      style={{ marginLeft: "0.5rem", background: "#fee2e2", color: "#991b1b" }}
                    >
                      revoked
                    </span>
                  )}
                  {link.requires_password && (
                    <span className="badge" style={{ marginLeft: "0.3rem" }}>
                      password
                    </span>
                  )}
                </div>
                <div className="meta" style={{ fontSize: "0.75rem" }}>
                  {link.permissions.join(", ")} ·
                  {link.expires_at
                    ? ` expires ${new Date(link.expires_at).toLocaleDateString()}`
                    : " no expiry"}{" "}
                  ·{link.use_count} use{link.use_count === 1 ? "" : "s"}
                  {link.max_uses ? ` / ${link.max_uses} max` : ""}
                </div>
              </div>
              <div style={{ display: "flex", gap: "0.4rem" }}>
                {!link.revoked && (
                  <>
                    <button type="button" className="ghost" onClick={() => copyUrl(link)}>
                      {copied === link.id ? "Copied!" : "Copy URL"}
                    </button>
                    <button
                      type="button"
                      className="ghost"
                      style={{ color: "#b42318" }}
                      onClick={() => revoke(link.id)}
                    >
                      Revoke
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
