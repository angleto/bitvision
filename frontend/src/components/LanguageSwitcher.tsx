"use client";

// Cookie-based locale toggle. Writes the BVP_LOCALE cookie and reloads
// the page so next-intl picks up the new catalogue server-side.
//
// Kept deliberately minimal: a <select> in the header. The cookie is
// readable + writable from the browser (no Secure flag locally so dev
// over plain http works); the request-side helper in
// src/i18n/request.ts reads the same cookie name.

import { useLocale, useTranslations } from "next-intl";
import { useTransition } from "react";

const COOKIE = "BVP_LOCALE";

export default function LanguageSwitcher() {
  const locale = useLocale();
  const t = useTranslations("common");
  const [pending, startTransition] = useTransition();

  function setLocale(next: string) {
    if (next === locale) return;
    // 1 year, root path. Same-site lax so the cookie travels on
    // top-level navigations from external links.
    document.cookie = `${COOKIE}=${next}; path=/; max-age=${60 * 60 * 24 * 365}; samesite=lax`;
    startTransition(() => {
      // Server components read the cookie at request-time; a hard
      // reload is the simplest way to refresh every translated string.
      window.location.reload();
    });
  }

  return (
    <label className="meta" style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}>
      <span style={{ fontSize: "0.75rem" }}>{t("language")}</span>
      <select
        value={locale}
        onChange={(e) => setLocale(e.target.value)}
        disabled={pending}
        aria-label={t("language")}
        style={{
          fontSize: "0.78rem",
          padding: "0.15rem 0.3rem",
          border: "1px solid var(--bv-card-border, #e5e7eb)",
          borderRadius: 3,
          background: "transparent",
          color: "inherit",
          cursor: pending ? "wait" : "pointer",
        }}
      >
        <option value="en">{t("languageEnglish")}</option>
        <option value="it">{t("languageItalian")}</option>
      </select>
    </label>
  );
}
