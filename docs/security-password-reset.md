# Password Reset Flow — Security Notes

Applies to the local-password auth path. Users authenticated via an
external OIDC issuer reset their credentials through that IdP and
never exercise this flow.

## Endpoints

- `POST /api/auth/forgot-password`  body `{email}`  → always 204
- `POST /api/auth/reset-password`  body `{token, new_password}` → 204 / 400

Both are rate-limited to 10 requests / minute / client IP (sliding window,
per-process — see §Rate limiting).

## Token model

- Table: `password_reset_tokens` (migration `0007_password_reset`).
- Plaintext: `secrets.token_urlsafe(32)` — 32 raw bytes, 43 URL-safe chars.
- Storage: **only** the SHA-256 hex digest is persisted in `token_hash`.
- Lifetime: 15 minutes (config `BVP_PASSWORD_RESET_TTL_MINUTES`).
- Single use: `used_at` is stamped on success; any subsequent redeem fails.
- On a successful reset, **all** other outstanding tokens for the same
  user are also marked used (defence in depth against parallel theft).

## Enumeration resistance

- `forgot-password` returns 204 whether or not the address has an
  account. No timing-correlated branch creates visible latency skew
  (both branches stop after an index lookup; email dispatch for the
  existing-user branch happens inside a suppressed `try` so a slow SMTP
  relay doesn't become an oracle).
- `reset-password` returns an identical "invalid or expired reset token"
  error for: unknown token, expired token, already-used token, and the
  (practically unreachable) FK-cascade-race case.
- OIDC-only users (`password_hash IS NULL`) silently receive no email —
  sending them a local-reset link would be misleading and would also
  leak that an OIDC account exists.

## Email delivery

- `services/email.py` exposes a single `send_email` coroutine. The
  default `stub` provider logs the message at INFO — useful for dev and
  tests without a real MTA. Production ops set `BVP_EMAIL_PROVIDER` to
  route through SMTP / SES / SendGrid (not yet implemented).
- The reset URL is built from `BVP_PUBLIC_FRONTEND_URL`. Any deployment
  with a non-default frontend host must set this or users will receive
  `http://localhost:3000/...` links.
- Email body copy does **not** confirm whether the address has an
  account — see the template in `build_password_reset_email`.

## Rate limiting

- `services/rate_limit.SlidingWindowRateLimiter` is an in-process token
  window. Good enough to blunt reset-token harvesting against a single
  replica. A multi-worker / multi-replica deployment should migrate to
  a Redis-backed limiter; the call site (`_forgot_limiter.check(ip)`)
  does not need to change.
- Client IP is extracted via `client_ip()`, preferring the first
  `X-Forwarded-For` hop. Operators running behind an untrusted proxy
  should strip spoofed XFF headers upstream.

## What a successful reset does

1. Verifies the submitted token's SHA-256 hash exists, is not used, and
   is not expired.
2. Rehashes `new_password` with bcrypt (`auth/passwords.hash_password`)
   and overwrites `users.password_hash`.
3. Stamps `used_at = now()` on the consumed token **and** on every
   other outstanding reset token for that user.

## What it does *not* do (yet)

- JWT revocation. Existing access tokens issued before the reset remain
  valid until their natural expiry (`BVP_JWT_EXPIRES_SECONDS`, default
  24 h). A future `users.session_version` column — bumped on reset and
  compared inside `decode_token` — would close this gap. Until then,
  operators should shorten the JWT TTL in production and/or ship a
  server-side session table.
- Account lockout after repeated failed resets. Rate limiting by IP is
  the only throttle.
- Email signing / DKIM enforcement. Those are transport-layer concerns
  handled by the downstream email provider.

## Audit trail

- `password_reset_tokens.requested_ip` captures the IP that initiated
  the request. Reviewing "I never asked for a reset" reports starts
  there. Consider attaching `AuditLog` events in a follow-up change —
  the current migration keeps the table self-contained.
