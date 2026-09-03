/* eslint-disable no-restricted-globals */
/**
 * Service worker — the piece Chrome requires before it will offer to
 * install the app, and the piece that decides what of a health record
 * is allowed to persist on the device.
 *
 * What it caches, and what it refuses to
 * --------------------------------------
 *
 * A cache is a store of whatever passes through it. This app renders
 * patient names, reports and images, so the default is: cache nothing.
 * The exceptions are narrow and deliberate:
 *
 *  - `/_next/static/**` — the application bundle. Content-hashed and
 *    immutable, so a cached copy can never be stale, and it holds code,
 *    never data.
 *  - `/icons/**` — the installed-app icons.
 *  - `/offline` — one static page shown when a navigation fails.
 *
 * Everything else is passed straight to the network and the response is
 * never stored. That includes every `/api/**` call (the data), every
 * `/shared/**` page (a recipient's view of somebody's record), and every
 * navigation: an SSR'd HTML document for `/patients/{id}` contains the
 * record itself, so it is fetched fresh every time and the offline page
 * is served only when the network fails outright.
 *
 * This is why there is no "offline mode" for clinical content. Making
 * the record readable on a plane means writing it to the device, and
 * that is a data-protection decision with a retention bound attached,
 * not a caching tweak. It is deliberately not taken here.
 *
 * Signing out clears everything: the app posts `bvp-clear-caches` on
 * logout, because leaving even the shell behind on a shared device is a
 * hint about who used it.
 *
 * Versioning
 * ----------
 * `CACHE_VERSION` names the cache; `activate` deletes every cache that
 * is not the current one. The browser byte-compares this file on
 * navigation, so editing it is what ships a new worker — bump the
 * version in the same edit. Bundle assets are content-hashed, so a
 * deploy cannot leave a stale chunk behind regardless.
 */

const CACHE_VERSION = "bvp-shell-v1";

// Fetched at install time. Kept to the minimum that makes a failed
// navigation legible; nothing here is user-specific.
const PRECACHE = ["/offline", "/icons/icon-192.png", "/icons/icon-512.png"];

/** Paths whose responses must never be written to a cache. */
function isNeverCached(url) {
  return (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/shared/") ||
    url.pathname.startsWith("/viewer/") ||
    url.pathname.startsWith("/_next/image")
  );
}

/** Immutable, data-free assets that are safe to serve from cache. */
function isImmutableAsset(url) {
  return url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/icons/");
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_VERSION);
      // Individually, so one 404 during a rollout does not abort the
      // whole install and leave the app without a worker.
      await Promise.all(
        PRECACHE.map((path) => cache.add(new Request(path, { cache: "reload" })).catch(() => {})),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((n) => n !== CACHE_VERSION).map((n) => caches.delete(n)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // A fetch handler has to exist for Chrome to consider the app
  // installable, but it must not change the semantics of anything it
  // does not understand. Non-GET, cross-origin and never-cached paths
  // fall through untouched.
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (isNeverCached(url)) return;

  if (isImmutableAsset(url)) {
    event.respondWith(
      (async () => {
        const cached = await caches.match(request);
        if (cached) return cached;
        const response = await fetch(request);
        // Opaque and error responses are not worth persisting, and a
        // partial (206) response would poison the cache.
        if (response.ok && response.type === "basic") {
          const cache = await caches.open(CACHE_VERSION);
          cache.put(request, response.clone());
        }
        return response;
      })(),
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          // Always the network: the document may carry the record.
          // Nothing about the response is stored.
          return await fetch(request);
        } catch {
          const offline = await caches.match("/offline");
          return (
            offline ??
            new Response("Offline", {
              status: 503,
              headers: { "content-type": "text/plain; charset=utf-8" },
            })
          );
        }
      })(),
    );
  }
});

self.addEventListener("message", (event) => {
  if (event.data?.type !== "bvp-clear-caches") return;
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    })(),
  );
});
