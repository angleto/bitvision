"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";

import { ApiError, authApi } from "@/lib/api";

export default function ResetPasswordPage() {
  return (
    <Suspense
      fallback={
        <div className="form">
          <p className="meta">Loading…</p>
        </div>
      }
    >
      <ResetPasswordForm />
    </Suspense>
  );
}

function ResetPasswordForm() {
  const router = useRouter();
  const search = useSearchParams();
  // Token arrives in the URL query from the email link; we read it once
  // at mount and keep it local so a subsequent rerender doesn't drop it
  // if the router refreshes the params.
  const [token] = useState(() => search.get("token") ?? "");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!token) {
      setErr("missing reset token — request a new link");
      return;
    }
    if (password !== confirm) {
      setErr("passwords don't match");
      return;
    }
    setBusy(true);
    try {
      await authApi.resetPassword(token, password);
      setDone(true);
      // Small delay so the success copy is visible before redirect.
      setTimeout(() => router.push("/login"), 1500);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "reset failed");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="form">
        <h1>Password updated</h1>
        <p className="meta">Redirecting you to login…</p>
      </div>
    );
  }

  return (
    <form className="form" onSubmit={onSubmit}>
      <h1>Reset password</h1>
      {!token && (
        <div className="error">
          No reset token in URL. <Link href="/forgot-password">Request a new link</Link>.
        </div>
      )}
      {err && <div className="error">{err}</div>}
      <label>
        New password (min 8 characters)
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <label>
        Confirm password
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={8}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </label>
      <div className="actions">
        <Link href="/login" className="meta">
          Back to login
        </Link>
        <button type="submit" disabled={busy || !token}>
          {busy ? "…" : "Set new password"}
        </button>
      </div>
    </form>
  );
}
