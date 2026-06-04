import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// CSP nonce middleware.
// ======================
//
// Why this exists
// ---------------
// 2026-05-21: the initial 3.7.9 security pass landed a CSP via
// ``next.config.mjs::headers()`` with ``script-src 'self'
// 'wasm-unsafe-eval'``. That works for our own bundled JS but blocks
// the inline ``<script>self.__next_f.push(...)</script>`` tags Next.js
// emits to feed React's streaming hydration. Result: a 200 OK page
// that never hydrates client-side, looking like "the site doesn't
// load".
//
// The architectural answer (Next.js 15 docs, Configuring/Content
// Security Policy) is a middleware that:
//
//   * generates a fresh nonce per request,
//   * sets ``Content-Security-Policy`` on the REQUEST headers with
//     that nonce — Next.js then automatically attaches the nonce to
//     every inline script it emits during streaming,
//   * exposes the nonce via ``x-nonce`` for any server-component
//     that needs to attach it manually,
//   * sets ``Content-Security-Policy`` on the RESPONSE headers so
//     the browser enforces it.
//
// The next.config.mjs CSP header is removed in lock-step (it would
// shadow this one).
//
// ``strict-dynamic`` is included on the script-src so any script
// loaded by a nonce'd script inherits the trust, which is required
// by Next.js's chunk-loader pattern.

function generateNonce(): string {
  const arr = new Uint8Array(16);
  crypto.getRandomValues(arr);
  let bin = "";
  for (let i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
  return btoa(bin);
}

const STATIC_DIRECTIVES = [
  "default-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "worker-src 'self' blob:",
  "media-src 'self' blob:",
  // PDF / document preview renders the bearer-fetched bytes through a
  // ``blob:`` URL inside an <iframe> (see DocumentPreview + PDFViewer).
  // Without ``frame-src``, framing falls back to ``default-src 'self'``,
  // which excludes ``blob:`` and makes Chrome show "This content is
  // blocked. Contact the site owner to fix the issue." ``child-src`` is
  // the deprecated alias older Safari still consults for frames.
  "frame-src 'self' blob:",
  "child-src 'self' blob:",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
];

function buildCsp(nonce: string): string {
  const isProd = process.env.NODE_ENV === "production";
  // ``strict-dynamic`` (CSP3) means "anything loaded by a trusted
  // script is also trusted" — Next.js's chunked module loader needs
  // it to bring in /_next/static/chunks/*.js after the nonce'd
  // bootstrap. ``'self'`` is kept alongside as a fallback for older
  // browsers that ignore ``strict-dynamic``.
  // ``'wasm-unsafe-eval'`` permits the WASM compilation step used by
  // Cornerstone / VTK decoders without re-enabling general ``eval``.
  const scriptSrc = isProd
    ? `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' 'wasm-unsafe-eval'`
    : `script-src 'self' 'nonce-${nonce}' 'unsafe-eval' 'wasm-unsafe-eval'`;

  const directives = [scriptSrc, ...STATIC_DIRECTIVES];
  if (isProd) directives.push("upgrade-insecure-requests");
  return directives.join("; ");
}

export function middleware(request: NextRequest) {
  const nonce = generateNonce();
  const csp = buildCsp(nonce);

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  // Next.js 15 reads CSP from the request headers to figure out
  // which nonce to weave into its own inline scripts. Without this
  // line the hydration tags ship un-nonce'd and the browser blocks
  // them.
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({
    request: { headers: requestHeaders },
  });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    // Skip API routes, Next static assets, image optimisation, and
    // favicon — none of those need a per-request nonce, and including
    // them taxes the LB pointlessly. ``missing: next-router-prefetch``
    // skips client-side prefetch revalidations.
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
