"use client";

// "Keep this access" — turn an anonymous share-link guest session into a
// real account (or connect an existing one), reconciling the share grant.
// Reached from the persistent ShareGuestBanner. Token-less: it reads the
// reconcilable state from the session (the HttpOnly JWT carries the
// share_link_id), so the share token never touches JS.

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { ApiError, type ShareSession, authApi } from "@/lib/api";

function resourceHref(s: ShareSession | null): string {
  if (!s) return "/studies";
  if (s.resource_kind === "patient") return `/patients/${s.resource_id}`;
  if (s.resource_kind === "study") return `/studies/${s.resource_id}`;
  return "/studies";
}

export default function KeepAccessPage() {
  const t = useTranslations("shareGuest");
  const router = useRouter();
  // ``undefined`` = loading, ``null`` = not a guest session.
  const [session, setSession] = useState<ShareSession | null | undefined>(undefined);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [emailExists, setEmailExists] = useState(false);

  useEffect(() => {
    authApi
      .shareSessionCurrent()
      .then((s) => setSession(s))
      .catch(() => setSession(null));
  }, []);

  async function onCreate(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (password.length < 8) {
      setErr(t("passwordTooShort"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const body: { password: string; display_name?: string; email?: string } = { password };
      if (name.trim()) body.display_name = name.trim();
      if (session && !session.recipient_email_known) body.email = email.trim();
      await authApi.shareSessionClaim(body);
      // Account created + grant reconciled + logged in via cookie.
      router.push(resourceHref(session ?? null));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setEmailExists(true);
        setErr(null);
      } else {
        setErr(t("error"));
      }
    } finally {
      setBusy(false);
    }
  }

  if (session === undefined) {
    return (
      <main>
        <div className="form">
          <p className="meta">{t("loading")}</p>
        </div>
      </main>
    );
  }

  if (session === null) {
    return (
      <main>
        <div className="form">
          <h1>{t("title")}</h1>
          <p className="meta">{t("notGuest")}</p>
          <a href="/studies">{t("goStudies")}</a>
        </div>
      </main>
    );
  }

  const showBind = session.bindable || emailExists;
  const loginBindHref = `/login?then=bind&sid=${encodeURIComponent(
    session.share_link_id,
  )}&next=${encodeURIComponent(resourceHref(session))}`;

  return (
    <main>
      <div className="form" style={{ maxWidth: 520 }}>
        <h1>{t("title")}</h1>
        <p className="meta">{t("intro")}</p>

        {showBind ? (
          <>
            {emailExists && <p className="meta">{t("emailExistsBody")}</p>}
            <a
              href={loginBindHref}
              style={{
                display: "block",
                textAlign: "center",
                background: "var(--bv-accent, #2563eb)",
                color: "#fff",
                borderRadius: 6,
                padding: "0.6rem 0.9rem",
                textDecoration: "none",
                fontWeight: 500,
                marginTop: "0.5rem",
              }}
            >
              {t("loginConnect")}
            </a>
          </>
        ) : (
          <form onSubmit={(e) => void onCreate(e)} style={{ marginTop: "0.5rem" }}>
            {!session.recipient_email_known && (
              <label style={{ display: "block", marginBottom: "0.6rem" }}>
                {t("emailLabel")}
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder={t("emailPlaceholder")}
                  style={{ width: "100%" }}
                />
              </label>
            )}
            <label style={{ display: "block", marginBottom: "0.6rem" }}>
              {t("nameLabel")}
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                style={{ width: "100%" }}
              />
            </label>
            <label style={{ display: "block", marginBottom: "0.6rem" }}>
              {t("passwordLabel")}
              <input
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                style={{ width: "100%" }}
              />
            </label>
            <button type="submit" disabled={busy} style={{ width: "100%" }}>
              {busy ? t("busy") : t("submit")}
            </button>
            {err && (
              <p className="error" style={{ marginTop: "0.5rem" }}>
                {err}
              </p>
            )}
          </form>
        )}

        <a
          href={resourceHref(session)}
          className="meta"
          style={{ display: "block", marginTop: "1rem" }}
        >
          {t("goResource")}
        </a>
      </div>
    </main>
  );
}
