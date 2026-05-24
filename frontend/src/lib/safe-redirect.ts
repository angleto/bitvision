/**
 * Validate a redirect target produced from untrusted input (typically a
 * ``?next=`` query parameter on the login page). Only same-origin
 * relative paths are accepted; everything else falls back to the
 * caller-supplied default.
 *
 * Threat model
 * ------------
 * Without validation, ``router.push(search.get("next") ?? "/studies")``
 * is an open-redirect (CWE-601): an attacker crafts
 * ``https://app.example/login?next=https://attacker.example`` and we
 * happily forward the freshly-authenticated user off-origin. On a
 * medical / health-record app this is a phishing accelerator (the
 * attacker page renders a "session expired" copy and steals the next
 * credential entry).
 *
 * What we reject
 * --------------
 *  * absolute URLs (``http:``, ``https:``, ``javascript:`` …)
 *  * protocol-relative URLs (``//evil.example/path``) which browsers
 *    treat as absolute under the page's own scheme
 *  * data URIs and similar exotic schemes
 *  * NUL bytes
 *  * any value that does not start with a single ``/``
 *
 * What we accept
 * --------------
 *  * relative paths that start with one ``/`` and optionally carry a
 *    query string and / or fragment (``/patients/abc?tab=labs#row-3``)
 */
export function safeInternalPath(candidate: string | null | undefined, fallback: string): string {
  if (!candidate) return fallback;
  if (candidate.includes("\x00")) return fallback;
  // Protocol-relative: //evil.example/foo
  if (candidate.startsWith("//")) return fallback;
  // Must be a single leading slash followed by a path char.
  if (!candidate.startsWith("/")) return fallback;
  // Defence-in-depth: reject any embedded scheme separator. A path
  // like ``/a/b:c`` is legitimate, but the cheap check ``includes(":")``
  // would be too aggressive. We instead reject only the canonical
  // ``scheme:`` shape at the start of the string (already covered by
  // the slash check) and reject backslash-escapes that some browsers
  // normalise back to forward slashes.
  if (candidate.startsWith("/\\")) return fallback;
  return candidate;
}
