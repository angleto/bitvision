"use client";

// Persistent, app-wide banner shown while the session is an anonymous
// share-link guest (``Me.is_anonymous_share``). It is the "register once
// inside — anytime" affordance the user asked for: even after the
// recipient has navigated deep into the shared study/fascicolo, a single
// always-visible bar lets them turn the guest session into a real account
// (reconciling the grant). The actual form lives on /keep-access so the
// bar stays slim and the same UX is reused from the post-open interstitial.

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { type ShareSession, authApi } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

export default function ShareGuestBanner() {
  const { user } = useAuth();
  const t = useTranslations("shareGuest");
  const router = useRouter();
  const [session, setSession] = useState<ShareSession | null>(null);

  const isGuest = user?.is_anonymous_share === true;

  useEffect(() => {
    if (!isGuest) {
      setSession(null);
      return;
    }
    let alive = true;
    authApi
      .shareSessionCurrent()
      .then((s) => {
        if (alive) setSession(s);
      })
      .catch(() => {
        // Non-fatal: if we can't resolve the reconcile state we simply
        // don't show the bar rather than breaking the page.
      });
    return () => {
      alive = false;
    };
  }, [isGuest]);

  if (!isGuest || !session || (!session.claimable && !session.bindable)) return null;

  return (
    <section
      aria-label={t("title")}
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        justifyContent: "center",
        gap: "0.75rem",
        padding: "0.5rem 1rem",
        background: "var(--bv-accent-soft, #eff6ff)",
        borderBottom: "1px solid var(--bv-accent, #2563eb)",
        fontSize: "0.9rem",
      }}
    >
      <span>{t("bannerText")}</span>
      <button
        type="button"
        onClick={() => router.push("/keep-access")}
        style={{
          background: "var(--bv-accent, #2563eb)",
          color: "#fff",
          border: "none",
          borderRadius: 6,
          padding: "0.35rem 0.8rem",
          fontWeight: 500,
          cursor: "pointer",
        }}
      >
        {t("bannerCta")}
      </button>
    </section>
  );
}
