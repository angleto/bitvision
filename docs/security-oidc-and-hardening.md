# Security hardening — OIDC, PHI redaction, concurrency, DoS caps

This document collects the security-relevant subsystems added in Unit
S10 of the audit-follow-up work:

1. **OIDC login flow** — external-identity path that converges on the
   same local JWT as the local-password route.
2. **PHI-redacting logger** — root-level filter that scrubs emails,
   Italian codici fiscali, JWTs, and bcrypt hashes from every
   `LogRecord` before a formatter ever sees it.
3. **Atomic share-link usage counter** — race-free `max_uses`
   enforcement via a single `UPDATE ... RETURNING` statement.
4. **DoS caps on search / list endpoints** — tight `q` length limit,
   per-transaction `statement_timeout`, capped pagination.

None of the four are user-visible on their own, but together they
close the medium/low findings from the most recent audit pass.

---

## 1. OIDC integration

### Module

`backend/src/bvphoenix/auth/oidc.py` — exposes two endpoints under
the `/api/auth/oidc` prefix:

- `GET /api/auth/oidc/login` — builds the issuer's authorize URL,
  drops a signed `bvp_oidc_state` cookie (HttpOnly, SameSite=lax, 10
  min TTL), and 302-redirects the browser.
- `GET /api/auth/oidc/callback` — reads `code` + `state`, verifies
  state against the cookie, exchanges the code at the issuer's
  `token_endpoint`, validates the `id_token` signature against the
  live JWKS, then issues a local JWT via `issue_access_token`.

### Configuration

Set in `backend/src/bvphoenix/config.py`:

| Setting (env)              | Purpose                                                |
|----------------------------|--------------------------------------------------------|
| `BVP_OIDC_ISSUER`          | Base URL of any OIDC issuer (Keycloak, Auth0, …)       |
| `BVP_OIDC_CLIENT_ID`       | Public client identifier registered with the provider  |
| `BVP_OIDC_CLIENT_SECRET`   | Confidential client secret                             |
| `BVP_OIDC_REDIRECT_URI`    | Callback URL registered at the provider                |

If any of the four are empty, the endpoints return `503 Service
Unavailable`. This guarantees we never ship half-wired auth in a
deployment where the operator forgot to configure it.

### JWKS / discovery caching

The discovery document and JWKS are cached per process in two
dictionaries. They refresh automatically if the issuer URL changes
(cache key) but not on time — operators rotating signing keys should
bounce the worker. This matches our other "boot-time configuration"
idioms and avoids adding a background refresh task for something
that changes every few months at most.

### User linking rules

1. Match existing `users` row by `oidc_subject = claims.sub` first.
2. If no match, try `email = claims.email.lower()` and link
   (`users.oidc_subject = claims.sub`) on hit.
3. If still no match, create a new `Subject` + `User` with
   `password_hash = NULL` (OIDC-only account).

The local-password path in `api/auth.py` is untouched — rows created
via `POST /api/auth/register` continue to authenticate with bcrypt
and remain independent of OIDC.

### Token handoff to the SPA

The callback returns a 302 to `/auth/callback` with the local JWT in
the URL fragment (`#access_token=...`). Fragments never cross the
network in `Referer` headers and never land in server access logs,
so the token doesn't leak through ordinary log shipping.

### CSRF protection

`state` is a `secrets.token_urlsafe(32)` nonce stored in an HttpOnly
cookie scoped to `/api/auth/oidc`. The callback rejects any request
whose `state` query parameter doesn't match the cookie via
`secrets.compare_digest`. An attacker who can land the victim on a
forged callback URL cannot satisfy both constraints simultaneously.

### What this does *not* cover

- No refresh-token handling — the local JWT lifetime
  (`BVP_JWT_EXPIRES_SECONDS`, default 24h) is authoritative. Users
  re-login via OIDC after expiry; the browser round-trip is fast if
  the IdP session is still valid.
- No logout propagation (RP-initiated logout). Clearing the local
  JWT on the client is sufficient for the app's session; revoking
  the upstream session is left to the IdP.
- No group / role claims mapping — `is_admin` still derives from the
  local `users.is_admin` column. Mapping IdP groups onto roles is a
  follow-up.

---

## 2. PHI redaction filter

### Module

`backend/src/bvphoenix/logging.py` installs a
`PHIRedactionFilter(logging.Filter)` on the root logger and every
attached handler. Installation happens once, in `main.py`, before
any other app code runs.

### Patterns scrubbed

| Pattern         | Regex                                                  | Rationale                                   |
|-----------------|--------------------------------------------------------|---------------------------------------------|
| Email           | `[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`     | RFC-lite; fast; no false positives in prod  |
| Codice fiscale  | `\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b` (case-i)  | Fixed Italian format, 16 chars              |
| JWT             | `\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b` | `eyJ` prefix = base64 of `{"`            |
| bcrypt hash     | `\$2[aby]\$\d{2}\$[A-Za-z0-9./]{53}`                   | Standard bcrypt format                      |

Every match is replaced with `<REDACTED>`.

### Coverage

The filter scrubs both `LogRecord.msg` and `LogRecord.args` (tuple
*and* mapping variants), and recurses into dicts / lists / tuples in
the args. This catches both of these patterns:

```python
log.info("login for %s", email)                  # args path
log.info(f"login for {email}")                   # msg path
log.info("ctx %s", {"email": email, "ip": "..."}) # nested dict path
```

### Limitations / known gaps

- Structured-logging extras passed through `extra=` are **not**
  scrubbed. Keep the structured side opt-in and deliberate.
- Base64-encoded payloads that happen to start with `eyJ` but
  aren't JWTs *will* be redacted. This is a conservative choice:
  false positives are harmless, false negatives leak secrets.
- The filter does not cover records emitted *before* `main.py`
  runs (import-time logging in sibling modules). None of the
  current import paths log at import time; if a new one does,
  move the install call into `bvphoenix/__init__.py`.

---

## 3. Atomic share-link usage counter

### Problem

The previous `verify_share_link` implementation did:

```python
if link.max_uses and link.use_count >= link.max_uses:
    raise HTTPException(status_code=404)
...
link.use_count += 1
await db.commit()
```

Two concurrent requests both read `use_count = 9` when `max_uses =
10`, both pass the check, both commit `use_count = 10`, and both
mint tokens. The counter is now one ahead of reality *and* the cap
has been breached.

### Fix

A single SQL statement replaces the read-modify-write:

```sql
UPDATE share_links
   SET use_count = use_count + 1
 WHERE id = :id
   AND (max_uses IS NULL OR use_count < max_uses)
 RETURNING use_count
```

Postgres takes a row lock on the `WHERE` predicate, and concurrent
updaters serialize. If the row matches, the caller gets the new
`use_count`. If the row exists but the predicate fails
(`use_count >= max_uses`), zero rows return and we map that to
**HTTP 429** (quota exhausted) — a closer semantic match than 404.

### Other status-code changes

- **410 Gone** for expired grants — the link existed once, it no
  longer does.
- Password validation still runs *before* the atomic bump so a
  brute-force attacker can't burn legitimate uses with failed
  guesses.

---

## 4. DoS caps on search / list endpoints

### `q` length

`GET /api/search` previously accepted `q` up to 512 chars. That's
more than enough rope to build a `plainto_tsquery` expression that
burns CPU time even with a GIN index. New cap: **128**. Covers every
real radiology query we've seen; rejects obvious abuse at the
FastAPI validator layer (422 before a DB connection is acquired).

### `statement_timeout`

Both `GET /api/studies` and `GET /api/search` issue a
`SET LOCAL statement_timeout = '3s'` on the current transaction.
Key properties:

- Scoped to the transaction — no global leakage, no knob touching
  `postgresql.conf`.
- 3 seconds is far above the 95th-percentile latency for either
  endpoint but well below any pathological full-text / ILIKE scan.
- A hit yields a Postgres `QueryCanceled` error, surfaced as a
  generic 500. We don't differentiate: the user gets the signal
  that the query failed, an attacker gets nothing actionable.

### `limit` caps

`limit` is already bounded at 200 (`Query(50, ge=1, le=200)`) in
both endpoints. Left as-is — no further tightening needed for now.

### `body_part` ILIKE

The `ilike(f"%{body_part}%")` pattern is a known non-sargable scan.
`body_part` is itself capped at 64 chars, which combined with the
3s `statement_timeout` is an acceptable trade-off for keeping the
simple UX. A trigram index on `series.body_part_examined` is the
real fix when volume grows; tracked separately.

---

## Files created / modified

**Created**

- `backend/src/bvphoenix/auth/oidc.py`
- `backend/src/bvphoenix/logging.py`
- `docs/security-oidc-and-hardening.md`

**Modified**

- `backend/src/bvphoenix/api/__init__.py` — wire OIDC router
- `backend/src/bvphoenix/api/sharing.py` — atomic counter +
  refined status codes
- `backend/src/bvphoenix/api/studies.py` — `statement_timeout`
- `backend/src/bvphoenix/api/search.py` — `q` cap +
  `statement_timeout`
- `backend/src/bvphoenix/config.py` — `oidc_redirect_uri`
- `backend/src/bvphoenix/main.py` — install redaction filter on
  boot
