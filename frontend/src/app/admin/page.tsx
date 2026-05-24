"use client";

// /admin — single landing hub for every admin surface. The point of
// having this page is twofold: it gives operators a glanceable map of
// "what can I configure" (rather than the dropdown-only navigation
// that came before) and it groups the surfaces by intent so that a
// new entry can land in the right section without rethinking the
// header chrome each time.

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth-context";

type Card = {
  href: string;
  titleKey: string;
  descKey: string;
};

type Section = {
  titleKey: string;
  subtitleKey: string;
  cards: Card[];
};

const SECTIONS: Section[] = [
  {
    titleKey: "sectionAccessTitle",
    subtitleKey: "sectionAccessSubtitle",
    cards: [{ href: "/admin/users", titleKey: "cardUsersTitle", descKey: "cardUsersDesc" }],
  },
  {
    titleKey: "sectionAITitle",
    subtitleKey: "sectionAISubtitle",
    cards: [
      { href: "/admin/llm-rates", titleKey: "cardLlmRatesTitle", descKey: "cardLlmRatesDesc" },
      {
        href: "/admin/llm-prompts",
        titleKey: "cardLlmPromptsTitle",
        descKey: "cardLlmPromptsDesc",
      },
      {
        href: "/admin/embeddings",
        titleKey: "cardEmbeddingsTitle",
        descKey: "cardEmbeddingsDesc",
      },
    ],
  },
  {
    titleKey: "sectionSystemTitle",
    subtitleKey: "sectionSystemSubtitle",
    cards: [
      { href: "/admin/settings", titleKey: "cardSettingsTitle", descKey: "cardSettingsDesc" },
    ],
  },
];

export default function AdminHubPage() {
  const t = useTranslations("adminHub");
  const { user, status } = useAuth();
  const router = useRouter();

  // Auth gate. The page lives outside ``settings/layout.tsx`` so we
  // recheck here; non-admin → home, non-authenticated → login.
  useEffect(() => {
    if (status !== "ready") return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!user.is_admin) {
      router.replace("/");
    }
  }, [status, user, router]);

  if (status !== "ready") {
    return (
      <main style={{ padding: "1.25rem" }}>
        <p className="meta">…</p>
      </main>
    );
  }
  if (!user || !user.is_admin) {
    return (
      <main style={{ padding: "1.25rem" }}>
        <p className="error">{t("forbidden")}</p>
      </main>
    );
  }

  return (
    <main style={{ padding: "1.25rem", maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ marginBottom: "0.25rem" }}>{t("title")}</h1>
      <p className="meta" style={{ marginTop: 0, marginBottom: "1.5rem" }}>
        {t("subtitle")}
      </p>

      {SECTIONS.map((sec) => (
        <section key={sec.titleKey} style={{ marginBottom: "2rem" }}>
          <h2 style={{ marginBottom: "0.1rem", fontSize: "1.05rem" }}>{t(sec.titleKey)}</h2>
          <p className="meta" style={{ marginTop: 0, marginBottom: "0.75rem" }}>
            {t(sec.subtitleKey)}
          </p>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
              gap: "0.8rem",
            }}
          >
            {sec.cards.map((c) => (
              <Link
                key={c.href}
                href={c.href}
                className="card"
                style={{
                  display: "block",
                  color: "inherit",
                  padding: "0.85rem 1rem",
                  textDecoration: "none",
                  border: "1px solid var(--bv-card-border)",
                  borderRadius: "var(--bv-r-md, 6px)",
                  background: "var(--bv-card-bg)",
                }}
              >
                <h3 style={{ marginTop: 0, marginBottom: "0.3rem", fontSize: "0.98rem" }}>
                  {t(c.titleKey)}
                </h3>
                <p className="meta" style={{ margin: 0, fontSize: "0.82rem" }}>
                  {t(c.descKey)}
                </p>
              </Link>
            ))}
          </div>
        </section>
      ))}
    </main>
  );
}
