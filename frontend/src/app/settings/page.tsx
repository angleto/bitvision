"use client";

// /settings — user settings hub. The auth guard lives in the shared
// ``settings/layout.tsx``; by the time this component renders the
// user is guaranteed to be signed in.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";

import { type BuildInfo, versionApi } from "@/lib/api";

const REPO_URL = "https://github.com/angleto/bitvision";

// One card for AI (assorbe ai-models + wallet + api-keys + ai-assistants).
// Le pagine specialistiche restano accessibili via deep-link, ma l'entry
// principale del hub è /settings/ai con progressive disclosure.
const SETTINGS_LINKS: { href: string; labelKey: string; descriptionKey: string }[] = [
  {
    href: "/settings/ai",
    labelKey: "aiHubLabel",
    descriptionKey: "aiHubDescription",
  },
  {
    href: "/settings/wallet",
    labelKey: "walletLabel",
    descriptionKey: "walletDescription",
  },
  {
    href: "/settings/wallet/sponsorships",
    labelKey: "sponsorshipsLabel",
    descriptionKey: "sponsorshipsDescription",
  },
  {
    href: "/settings/mfa",
    labelKey: "mfaLabel",
    descriptionKey: "mfaDescription",
  },
  {
    href: "/settings/privacy",
    labelKey: "privacyLabel",
    descriptionKey: "privacyDescription",
  },
  {
    href: "/settings/shares",
    labelKey: "sharesLabel",
    descriptionKey: "sharesDescription",
  },
  {
    href: "/contributions/review",
    labelKey: "contributionsLabel",
    descriptionKey: "contributionsDescription",
  },
  {
    href: "/settings/calendar",
    labelKey: "calendarLabel",
    descriptionKey: "calendarDescription",
  },
];

function BuildInfoCard(): React.JSX.Element {
  const t = useTranslations("buildInfo");
  const [info, setInfo] = useState<BuildInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    versionApi
      .get()
      .then((b) => {
        if (!cancelled) setInfo(b);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function copy(value: string, what: string): void {
    if (typeof window === "undefined" || !value) return;
    navigator.clipboard?.writeText(value).then(() => {
      setCopied(what);
      window.setTimeout(() => setCopied(null), 1800);
    });
  }

  const fmtDate = (iso: string) => (iso ? new Date(iso).toLocaleString() : "—");

  return (
    <section
      className="card"
      style={{ marginTop: "2rem", padding: "1rem 1.2rem" }}
      aria-labelledby="build-info-title"
    >
      <h2 id="build-info-title" style={{ marginTop: 0, fontSize: "1rem" }}>
        {t("title")}
      </h2>
      {error && (
        <p role="alert" style={{ color: "var(--bv-danger, #c00)", fontSize: "0.85rem" }}>
          {t("loadError", { detail: error })}
        </p>
      )}
      {!info && !error && (
        <p className="meta" aria-live="polite">
          …
        </p>
      )}
      {info && (
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(7rem, max-content) 1fr",
            gap: "0.35rem 1rem",
            margin: 0,
            fontSize: "0.86rem",
          }}
        >
          <dt className="meta">{t("version")}</dt>
          <dd style={{ margin: 0, display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <code style={{ fontFamily: "var(--bv-mono, ui-monospace, monospace)" }}>
              {info.version || "dev"}
            </code>
            <button
              type="button"
              onClick={() => copy(info.version, "version")}
              aria-label={t("copyVersion")}
              style={{ fontSize: "0.72rem", padding: "0.1rem 0.45rem" }}
            >
              {copied === "version" ? `✓ ${t("copied")}` : t("copy")}
            </button>
          </dd>

          <dt className="meta">{t("commit")}</dt>
          <dd style={{ margin: 0, display: "flex", alignItems: "center", gap: "0.5rem" }}>
            {info.git_sha ? (
              <a
                href={`${REPO_URL}/commit/${info.git_sha}`}
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontFamily: "var(--bv-mono, ui-monospace, monospace)" }}
              >
                {info.git_sha_short}
              </a>
            ) : (
              <span className="meta">—</span>
            )}
            {info.git_sha && (
              <button
                type="button"
                onClick={() => copy(info.git_sha, "sha")}
                aria-label={t("copySha")}
                style={{ fontSize: "0.72rem", padding: "0.1rem 0.45rem" }}
              >
                {copied === "sha" ? `✓ ${t("copied")}` : t("copy")}
              </button>
            )}
          </dd>

          <dt className="meta">{t("built")}</dt>
          <dd style={{ margin: 0 }}>{fmtDate(info.build_date)}</dd>

          <dt className="meta">{t("runtime")}</dt>
          <dd style={{ margin: 0 }}>Python {info.python_version}</dd>
        </dl>
      )}
    </section>
  );
}

export default function SettingsIndex(): React.JSX.Element {
  const t = useTranslations("settingsHub");
  return (
    <main>
      <h1>{t("title")}</h1>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
          gap: "1rem",
          marginTop: "1rem",
        }}
      >
        {SETTINGS_LINKS.map((l) => (
          <Link
            key={l.href}
            href={l.href}
            className="card"
            style={{ display: "block", color: "inherit" }}
          >
            <h3 style={{ marginTop: 0 }}>{t(l.labelKey)}</h3>
            <p className="meta" style={{ margin: 0 }}>
              {t(l.descriptionKey)}
            </p>
          </Link>
        ))}
      </div>
      <BuildInfoCard />
    </main>
  );
}
