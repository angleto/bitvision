import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

// ---------------------------------------------------------------------
// Security headers
// ---------------------------------------------------------------------
//
// Applied to every response served by the Next.js process.
//
// Content-Security-Policy is intentionally NOT emitted from here —
// it is generated per-request by ``src/middleware.ts`` so each
// response carries a fresh nonce that Next.js can attach to the
// hydration inline-script tags. Setting a static CSP here would
// shadow the dynamic one and re-introduce the "page loads but never
// hydrates" failure mode we hit on 2026-05-21.
//
// The headers below are static (no per-request state) and stay here
// for clarity: HSTS, frame-deny, MIME-sniffing, referrer-policy,
// permissions-policy, COOP.
const SECURITY_HEADERS = [
  {
    // HSTS: 2-year max-age, includeSubDomains, preload-ready. Sent
    // unconditionally; the Traefik edge already redirects http→https
    // so a downgrade attack on a fresh client only has one window
    // before the header pins TLS for two years.
    key: "Strict-Transport-Security",
    value: "max-age=63072000; includeSubDomains; preload",
  },
  {
    // Legacy clickjacking defence retained alongside the CSP
    // ``frame-ancestors`` directive: older browsers (Safari 13.x,
    // some embedded WebViews) honour XFO but not the CSP equivalent.
    key: "X-Frame-Options",
    value: "DENY",
  },
  {
    key: "X-Content-Type-Options",
    value: "nosniff",
  },
  {
    // Strip patient/study IDs from the Referer when the user clicks
    // an external link. Internal navigations within the app keep
    // full referrer (same-origin) so analytics still work.
    key: "Referrer-Policy",
    value: "strict-origin-when-cross-origin",
  },
  {
    // Disable browser features the app never asks for. Each new
    // capability must be explicitly opted-in here before the
    // feature can be used at runtime.
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  },
  {
    // Defence-in-depth: if a future route accidentally embeds an
    // attacker-controlled origin via an iframe, the cross-origin
    // window isolation prevents document.domain shenanigans.
    key: "Cross-Origin-Opener-Policy",
    value: "same-origin",
  },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Disabled: we build routes from query params dynamically, so the
  // strict TypedRoutes literal check adds ceremony without catching real
  // bugs. Re-enable once all internal navigation is static.
  typedRoutes: false,
  // VTK.js ships ESM with macros that bundlers must process; without
  // transpilation Next leaves them untouched and the runtime trips on
  // the worker / WebGL helpers.
  transpilePackages: ["@kitware/vtk.js"],
  experimental: {
    // Restore the scroll position when the browser back button takes
    // the user back to a previous page. Without this, Next defaults to
    // scrolling-to-top on every navigation, which makes the clinician's
    // "open a document, read it, ⬅, find the next one" loop painful
    // inside a long folder: every return lands at the top of the list.
    // Combined with the ``#item-<id>`` hash that the in-page back-links
    // emit, the user lands exactly where they left.
    scrollRestoration: true,
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: SECURITY_HEADERS,
      },
    ];
  },
};

export default withNextIntl(nextConfig);
