# Email verification

Registration previously accepted any email address without proof of
ownership, letting an attacker register under an address they do not
control (MEDIUM severity). This document describes the end-to-end flow
now in place and the knobs available to operators.

## Flow

1. `POST /api/auth/register` creates the `subjects` + `users` rows and
   mints a one-shot verification token.
   * Raw token: `secrets.token_urlsafe(32)`.
   * Persisted value: `sha256(raw)` — stored in
     `email_verification_tokens.token_hash`. The raw value is only ever
     in transit (mail body) and in the user's inbox.
   * TTL: `BVP_EMAIL_VERIFICATION_TTL_SECONDS` (default 24 hours).
2. The backend dispatches `send_verification_email` on a FastAPI
   `BackgroundTasks` worker. The mail body contains
   `<BVP_FRONTEND_BASE_URL>/verify-email?token=<raw>`.
3. The user follows the link; the Next.js page `POST`s the token back
   to `/api/auth/verify-email`. On success the row's `used_at` is set,
   `users.email_verified_at` is populated, and a JWT is returned so the
   user lands logged in.
4. If the link is lost or expires, `POST /api/auth/resend-verification`
   mints a fresh token. This endpoint always responds `202 Accepted`
   regardless of whether the address exists — account-enumeration
   resistance.

## Login gating

`BVP_REQUIRE_EMAIL_VERIFICATION` (default `false`) controls whether
login refuses unverified accounts.

* **Off** (default, dev convenience): verification still happens — the
  email is mailed, the endpoint works — but unverified users can log
  in. This preserves the existing dev bootstrap UX.
* **On** (recommended for production): `POST /api/auth/login` returns
  `403 email not verified` until the user clicks the link. The 403 is
  distinct from the 401 used for bad passwords so the client can
  surface an actionable error.

The JWT path is unchanged: once verified, the user receives exactly the
same token issuance as before (`issue_access_token`). Users who sign
in via an external OIDC issuer bypass this flow entirely because the
external IdP owns their email verification.

## Email sender

`bvphoenix.services.email` provides a tiny `EmailSender` protocol with
two built-ins:

* `DevEmailSender` — the default when `BVP_SMTP_HOST` is empty.
  Appends each message to `logs/dev_emails.eml` and echoes a one-line
  summary to stdout. This is what CI and `docker compose up` see.
* `SmtpEmailSender` — stdlib `smtplib` with STARTTLS. Configured via:
  * `BVP_SMTP_HOST`
  * `BVP_SMTP_PORT` (default 587)
  * `BVP_SMTP_USERNAME` / `BVP_SMTP_PASSWORD`
  * `BVP_SMTP_USE_TLS` (default true)
  * `BVP_SMTP_FROM_ADDRESS` / `BVP_SMTP_FROM_NAME`

Mail dispatch errors are logged and swallowed — the token remains
valid so the user can hit "resend".

## Threat model notes

* **Token enumeration** — 32-byte `token_urlsafe` gives ~256 bits of
  entropy, infeasible to brute force.
* **Stored-hash compromise** — a DB dump does not leak usable tokens;
  the raw value is SHA-256'd before insertion.
* **Account enumeration** — both `/register` and `/resend-verification`
  avoid leaking existence of an address (the latter always returns
  202). `/register` still returns 409 on duplicate addresses because
  the UX would otherwise silently fail; that is acceptable risk given
  the account is visible to anyone who tries to sign up anyway.
* **Token reuse** — `used_at` is set atomically in the same commit as
  `email_verified_at`. A replay returns `400 invalid or expired
  token`.
* **Link interception** — STARTTLS and a long random token limit the
  attack surface. Operators handling highly sensitive deployments
  should front the mail relay with DKIM/SPF and consider dropping the
  `access_token` out of the verify response (forcing a login step).
