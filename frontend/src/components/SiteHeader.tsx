"use client";

import { useTranslations } from "next-intl";
import Image from "next/image";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import ActiveJobsPanel from "@/components/ActiveJobsPanel";
import LanguageSwitcher from "@/components/LanguageSwitcher";
import ThemeToggle from "@/components/ThemeToggle";
import { useAuth } from "@/lib/auth-context";

export default function SiteHeader() {
  const { user, status, logout } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [q, setQ] = useState(searchParams.get("q") ?? "");
  const t = useTranslations("site");

  // Defer auth-conditional chrome until after first client commit.
  // The server renders the unauthenticated tree (no token in
  // ``localStorage`` from inside Node); when localStorage carries a
  // session the client would otherwise render the authenticated tree
  // on the very first paint and mismatch the SSR HTML. Flipping the
  // gate inside ``useEffect`` guarantees the first render is identical
  // on both sides and the auth-dependent branches mount on the next
  // commit.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);
  const isAuthed = hydrated && status === "ready" && !!user;

  return (
    <header className="site-header">
      <div className="site-header__row">
        <Link href="/" className="site-header__logo">
          <Image
            src="/brand/wordmark.png"
            alt="bit.vision"
            width={160}
            height={32}
            priority
            style={{ height: 28, width: "auto" }}
          />
        </Link>
        {isAuthed && (
          <form
            className="site-header__search"
            onSubmit={(e) => {
              e.preventDefault();
              const query = q.trim();
              // The "Studies" tab is gone — searches now route to the
              // unified Patients listing, which surfaces studies via
              // their owning patient. Free-text queries match patient
              // name / CF / external id server-side.
              router.push(query ? `/patients?q=${encodeURIComponent(query)}` : "/patients");
            }}
          >
            <input
              type="search"
              placeholder={t("searchPlaceholder")}
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </form>
        )}
        <nav className="site-header__nav">
          {isAuthed && (
            <>
              <Link href="/patients">{t("patients")}</Link>
              <Link href="/tags">{t("tags")}</Link>
              <Link href="/search">{t("search")}</Link>
              <Link href="/search/visual">{t("visualSearch")}</Link>
              <Link href="/upload">{t("upload")}</Link>
              {user.is_admin && <AdminMenu />}
              <ActiveJobsPanel />
              <Link href="/settings" title={user.email}>
                {user.display_name}
                {user.is_admin ? ` ·${t("adminBadge")}` : ""}
              </Link>
              <button type="button" onClick={logout}>
                {t("logout")}
              </button>
            </>
          )}
          {!isAuthed && (
            <>
              <Link href="/login">{t("login")}</Link>
              <Link href="/register">{t("register")}</Link>
            </>
          )}
          <ThemeToggle className="ghost" />
          <LanguageSwitcher />
        </nav>
      </div>
    </header>
  );
}

function AdminMenu() {
  const t = useTranslations("site");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const linkStyle = {
    display: "block",
    padding: "0.42rem 0.9rem",
    fontSize: "0.88rem",
  } as const;
  const headerStyle = {
    padding: "0.4rem 0.9rem 0.15rem",
    fontSize: "0.7rem",
    fontWeight: 600,
    textTransform: "uppercase" as const,
    letterSpacing: "0.04em",
    opacity: 0.65,
  };
  const sepStyle = {
    height: 1,
    background: "var(--bv-card-border)",
    margin: "0.25rem 0",
  };

  return (
    <div ref={ref} style={{ position: "relative" }} className="site-header__admin-menu">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={t("adminUsersTitle")}
        style={{ cursor: "pointer", padding: "0.4rem 0.6rem", background: "transparent" }}
      >
        {t("adminUsersLink")} ▾
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: "0.25rem",
            background: "var(--bv-card-bg)",
            color: "var(--bv-fg)",
            border: "1px solid var(--bv-card-border)",
            borderRadius: "var(--bv-r-md, 6px)",
            padding: "0.3rem 0",
            minWidth: 260,
            zIndex: 100,
            boxShadow: "0 8px 24px rgba(0,0,0,0.22)",
          }}
        >
          <Link
            role="menuitem"
            href="/admin"
            style={{ ...linkStyle, fontWeight: 600 }}
            onClick={() => setOpen(false)}
          >
            {t("adminMenuHubLink")}
          </Link>
          <div style={sepStyle} />

          <div style={headerStyle}>{t("adminMenuSectionAccess")}</div>
          <Link
            role="menuitem"
            href="/admin/users"
            style={linkStyle}
            onClick={() => setOpen(false)}
          >
            {t("adminUsersMenuUsers")}
          </Link>

          <div style={headerStyle}>{t("adminMenuSectionAI")}</div>
          <Link
            role="menuitem"
            href="/admin/llm-rates"
            style={linkStyle}
            onClick={() => setOpen(false)}
          >
            {t("adminUsersMenuLlmRates")}
          </Link>
          <Link
            role="menuitem"
            href="/admin/llm-prompts"
            style={linkStyle}
            onClick={() => setOpen(false)}
          >
            {t("adminUsersMenuLlmPrompts")}
          </Link>
          <Link
            role="menuitem"
            href="/admin/embeddings"
            style={linkStyle}
            onClick={() => setOpen(false)}
          >
            {t("adminUsersMenuEmbeddings")}
          </Link>

          <div style={headerStyle}>{t("adminMenuSectionSystem")}</div>
          <Link
            role="menuitem"
            href="/admin/settings"
            style={linkStyle}
            onClick={() => setOpen(false)}
          >
            {t("adminUsersMenuSettings")}
          </Link>
        </div>
      )}
    </div>
  );
}
