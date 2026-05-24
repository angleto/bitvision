"use client";

import Link from "next/link";
import { type FormEvent, useState } from "react";

import { ApiError, authApi } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Deliberately optimistic: the backend always returns 204 (existence
  // is not leaked), so on any non-network success we show the same
  // "check your inbox" copy regardless of whether the address exists.
  const [sent, setSent] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await authApi.forgotPassword(email);
      setSent(true);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <div className="form">
        <h1>Check your email</h1>
        <p className="meta">
          If an account exists for {email}, we&apos;ve sent a password reset link. The link expires
          in 15 minutes.
        </p>
        <div className="actions">
          <Link href="/login" className="meta">
            Back to login
          </Link>
        </div>
      </div>
    );
  }

  return (
    <form className="form" onSubmit={onSubmit}>
      <h1>Forgot password</h1>
      <p className="meta">Enter your account email and we&apos;ll send you a reset link.</p>
      {err && <div className="error">{err}</div>}
      <label>
        Email
        <input
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <div className="actions">
        <Link href="/login" className="meta">
          Back to login
        </Link>
        <button type="submit" disabled={busy}>
          {busy ? "…" : "Send reset link"}
        </button>
      </div>
    </form>
  );
}
