"use client";

// Shared auth gate for /settings/* pages. Every page under this
// segment needs a logged-in user; the previous pattern repeated the
// same useEffect-redirect block in five files, which drifted and hid
// the rule. Centralising here means each sub-page can assume
// ``user`` is set by the time its body renders.
//
// Kept as a client component so we can read ``useAuth`` and
// ``usePathname``. The server has no session context in this app, so
// there is no gain to server-rendering the guard.

import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect } from "react";

import { useAuth } from "@/lib/auth-context";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, status } = useAuth();

  useEffect(() => {
    if (status === "ready" && !user) {
      router.replace(`/login?next=${encodeURIComponent(pathname || "/settings")}`);
    }
  }, [status, user, router, pathname]);

  if (status !== "ready" || !user) {
    return (
      <main>
        <p className="meta">Loading…</p>
      </main>
    );
  }

  return <>{children}</>;
}
