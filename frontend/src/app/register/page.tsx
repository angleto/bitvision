"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { ApiError, authApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function RegisterPage() {
  const router = useRouter();
  const { register } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [verificationSent, setVerificationSent] = useState(false);
  const [resendMsg, setResendMsg] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const { verificationRequired } = await register(email, password, displayName);
      if (verificationRequired) {
        setVerificationSent(true);
      } else {
        router.push("/studies");
      }
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "register failed");
    } finally {
      setBusy(false);
    }
  }

  async function onResend() {
    setResendMsg(null);
    try {
      await authApi.resendVerification(email);
      setResendMsg("Verification email re-sent. Please check your inbox.");
    } catch (e) {
      setResendMsg(e instanceof ApiError ? e.message : "failed to resend");
    }
  }

  if (verificationSent) {
    return (
      <div className="form">
        <h1>Check your email</h1>
        <p>
          We sent a verification link to <strong>{email}</strong>. Click the link to finish creating
          your account. The link expires in 24 hours.
        </p>
        {resendMsg && <div className="meta">{resendMsg}</div>}
        <div className="actions">
          <Link href="/login" className="meta">
            Back to login
          </Link>
          <button type="button" onClick={onResend}>
            Resend email
          </button>
        </div>
      </div>
    );
  }

  return (
    <form className="form" onSubmit={onSubmit}>
      <h1>Create account</h1>
      {err && <div className="error">{err}</div>}
      <label>
        Display name
        <input required value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
      </label>
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
      <label>
        Password (min 8 characters)
        <input
          type="password"
          autoComplete="new-password"
          required
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <div className="actions">
        <Link href="/login" className="meta">
          Already have an account?
        </Link>
        <button type="submit" disabled={busy}>
          {busy ? "…" : "Sign up"}
        </button>
      </div>
    </form>
  );
}
