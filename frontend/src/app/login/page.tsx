"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";

import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { safeInternalPath } from "@/lib/safe-redirect";

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="form">
          <FallbackMeta />
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}

function FallbackMeta() {
  // Hooks must be called inside a component, so the loading fallback
  // is its own tiny component.
  const t = useTranslations("common");
  return <p className="meta">{t("loading")}</p>;
}

function LoginForm() {
  const router = useRouter();
  const search = useSearchParams();
  const { login, loginMfa } = useAuth();
  const t = useTranslations("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  // When the backend signals ``mfa_required`` we pivot to a second step
  // that asks for the TOTP code in addition to the credentials already
  // captured. Keeping the email/password in state means the user does
  // not have to retype them.
  const [mfaRequired, setMfaRequired] = useState(false);
  const [totpCode, setTotpCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      if (mfaRequired) {
        await loginMfa(email, password, totpCode.trim());
      } else {
        await login(email, password);
      }
      // Validate ``?next=`` before redirecting to defend against the
      // open-redirect / phishing vector: a crafted
      // ``/login?next=https://evil.example`` would otherwise punt the
      // freshly-authenticated user off-origin.
      const dest = safeInternalPath(search.get("next"), "/studies");
      // ``/api/*`` targets are served by the backend (e.g. the
      // auth-gated Swagger docs at /api/docs), not by the SPA router.
      // ``router.push`` would try to resolve them as app routes and
      // fail; hard-navigate instead so the browser issues a real
      // request (carrying the freshly-set session cookie) to the
      // ingress, which routes /api/* to the backend.
      if (dest.startsWith("/api/")) {
        window.location.assign(dest);
      } else {
        router.push(dest);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        const detail = (e.detail as { detail?: string })?.detail;
        if (detail === "mfa_required") {
          setMfaRequired(true);
          setErr(null);
          setBusy(false);
          return;
        }
        if (detail === "invalid_totp") {
          setErr(t("errorInvalidTotp"));
          setBusy(false);
          return;
        }
      }
      setErr(e instanceof ApiError ? e.message : t("errorGeneric"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={onSubmit}>
      <h1>{t("title")}</h1>
      {err && <div className="error">{err}</div>}
      {!mfaRequired && (
        <>
          <label>
            {t("emailLabel")}
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label>
            {t("passwordLabel")}
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        </>
      )}
      {mfaRequired && (
        <>
          <p className="meta">{t("mfaTitle")}</p>
          <label>
            {t("mfaCodeLabel")}
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              value={totpCode}
              onChange={(e) => setTotpCode(e.target.value)}
            />
          </label>
        </>
      )}
      <div className="actions">
        {!mfaRequired ? (
          <>
            <Link href="/register" className="meta">
              {t("noAccount")}
            </Link>
            <Link href="/forgot-password" className="meta">
              {t("forgotPassword")}
            </Link>
          </>
        ) : (
          <button
            type="button"
            className="meta"
            onClick={() => {
              setMfaRequired(false);
              setTotpCode("");
              setErr(null);
            }}
          >
            {t("mfaUseDifferentAccount")}
          </button>
        )}
        <button type="submit" disabled={busy}>
          {busy ? t("submitBusy") : mfaRequired ? t("mfaVerify") : t("submit")}
        </button>
      </div>
    </form>
  );
}
