"use client";

import { useEffect } from "react";

/**
 * Registers the service worker, and nothing else.
 *
 * The worker is what makes Chrome offer "Install app", and what serves
 * the offline page when a navigation fails. It caches only the
 * application bundle and one static page — see `public/sw.js` for why
 * clinical content is deliberately excluded.
 *
 * Registration is skipped in development: a worker that outlives a dev
 * server hands you a stale bundle and a confusing afternoon. It is also
 * skipped where the API is not same-origin, because then the page is
 * being served from somewhere the worker's scope assumptions do not
 * hold.
 */
export default function ServiceWorkerRegistrar() {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") return;
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;

    let cancelled = false;
    // After load, so registration never competes with the first paint
    // for bandwidth on a phone.
    const register = () => {
      if (cancelled) return;
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
        // A failed registration costs the install prompt and the offline
        // page. Everything else works, so there is nothing to tell the
        // user and nothing to retry.
      });
    };
    if (document.readyState === "complete") register();
    else window.addEventListener("load", register, { once: true });

    return () => {
      cancelled = true;
      window.removeEventListener("load", register);
    };
  }, []);

  return null;
}

/**
 * Drop every cache the worker holds.
 *
 * Called on sign-out. What the worker stores is the application shell
 * rather than any record, but on a shared device even the shell is a
 * hint that somebody used this app here, and a signed-out session
 * should leave nothing of its own behind.
 *
 * Safe to call when there is no worker, no controller, or no Cache API.
 */
export async function clearServiceWorkerCaches(): Promise<void> {
  try {
    navigator.serviceWorker?.controller?.postMessage({ type: "bvp-clear-caches" });
    if (typeof caches !== "undefined") {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    }
  } catch {
    // Private windows and locked-down browsers throw on the accessor
    // itself. Losing the cleanup is not worth failing a sign-out over.
  }
}
