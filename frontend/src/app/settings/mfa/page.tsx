"use client";

// MFA setup / management page.
//
// Three states:
//   1. status loading         — the GET /api/mfa/status is in flight.
//   2. not enabled / pending  — user can click "Start setup" to get a
//                               secret + QR code, then activate with a
//                               TOTP code. On success we show the
//                               backup codes (one-time display) and
//                               offer a .txt download.
//   3. enabled                — user can disable (requires valid TOTP).

// Auth gate is in ``settings/layout.tsx``; ``user`` is guaranteed set here.
// We still read ``useAuth`` to surface the admin-HIPAA hint.

import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, type MfaSetup, type MfaStatus, mfaApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function MfaSettingsPage() {
  const { user } = useAuth();
  const [status, setStatus] = useState<MfaStatus | null>(null);
  const [setup, setSetup] = useState<MfaSetup | null>(null);
  const [totp, setTotp] = useState("");
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await mfaApi.status());
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed to load MFA status");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onStartSetup() {
    setErr(null);
    setBusy(true);
    try {
      setSetup(await mfaApi.setup());
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "setup failed");
    } finally {
      setBusy(false);
    }
  }

  async function onActivate(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const res = await mfaApi.activate(totp.trim());
      setBackupCodes(res.backup_codes);
      setSetup(null);
      setTotp("");
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "activation failed");
    } finally {
      setBusy(false);
    }
  }

  async function onDisable(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await mfaApi.disable(totp.trim());
      setTotp("");
      setBackupCodes(null);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "disable failed");
    } finally {
      setBusy(false);
    }
  }

  function downloadBackupCodes() {
    if (!backupCodes) return;
    const body = `bitvision phoenix — MFA backup codes\nEach code is single-use. Keep them somewhere safe.\n\n${backupCodes.join("\n")}\n`;
    const blob = new Blob([body], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bvphoenix-backup-codes.txt";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  if (status === null) {
    return (
      <div className="form">
        <p className="meta">Loading…</p>
      </div>
    );
  }

  return (
    <div className="form" style={{ maxWidth: 560 }}>
      <h1>Two-factor authentication</h1>
      <p className="meta">
        <Link href="/studies">← back</Link>
      </p>
      {err && <div className="error">{err}</div>}

      {backupCodes && (
        <div className="card" style={{ padding: "1rem", marginBottom: "1rem" }}>
          <h2>Backup codes</h2>
          <p className="meta">
            Store these one-time codes somewhere safe. You can use one in place of a TOTP code if
            you lose access to your authenticator. They are shown only this once.
          </p>
          <ul style={{ fontFamily: "monospace", columns: 2 }}>
            {backupCodes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
          <button type="button" onClick={downloadBackupCodes}>
            Download .txt
          </button>
        </div>
      )}

      {status.enabled ? (
        <form onSubmit={onDisable}>
          <p>
            MFA is <strong>active</strong>
            {status.enabled_at && <> since {new Date(status.enabled_at).toLocaleDateString()}</>}.
            You have {status.backup_codes_remaining} backup code
            {status.backup_codes_remaining === 1 ? "" : "s"} remaining.
          </p>
          {user?.is_admin ? (
            <p className="meta">
              Admins are required to keep MFA enabled on HIPAA-mode deployments. Contact a super
              admin to disable it.
            </p>
          ) : (
            <>
              <label>
                Confirm with a TOTP or backup code
                <input
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  required
                  value={totp}
                  onChange={(e) => setTotp(e.target.value)}
                />
              </label>
              <div className="actions">
                <button type="submit" disabled={busy}>
                  {busy ? "…" : "Disable MFA"}
                </button>
              </div>
            </>
          )}
        </form>
      ) : setup ? (
        <form onSubmit={onActivate}>
          <p>
            Scan this QR code with your authenticator (1Password, Authy, Google Authenticator, etc.)
            or enter the secret manually.
          </p>
          <div style={{ textAlign: "center", margin: "1rem 0" }}>
            <img
              src={`data:image/png;base64,${setup.qr_png_base64}`}
              alt="MFA setup QR code"
              style={{ maxWidth: 220, width: "100%" }}
            />
          </div>
          <p className="meta" style={{ fontFamily: "monospace", wordBreak: "break-all" }}>
            Secret: {setup.secret}
          </p>
          <label>
            Enter the 6-digit code from your authenticator to activate
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
            />
          </label>
          <div className="actions">
            <button type="submit" disabled={busy}>
              {busy ? "…" : "Activate"}
            </button>
          </div>
        </form>
      ) : (
        <>
          <p>
            {status.pending
              ? "You started MFA setup but did not finish. Restart below."
              : "MFA is currently disabled. Turn it on to protect your account with a one-time code from an authenticator app."}
          </p>
          {user?.is_admin && (
            <p className="meta">
              As an admin you are required to have MFA enabled on HIPAA-mode deployments. Please
              complete setup now.
            </p>
          )}
          <div className="actions">
            <button type="button" disabled={busy} onClick={onStartSetup}>
              {busy ? "…" : status.pending ? "Restart setup" : "Start setup"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
