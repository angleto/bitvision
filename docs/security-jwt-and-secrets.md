# JWT signing and secret hygiene

This document describes how bitvision phoenix handles JWT signing keys
and other secrets, and what operators must do before deploying to
production.

## Threat model, briefly

The local-password auth path mints JWTs that authorize callers against
the REST API. If an attacker recovers the signing secret they can
impersonate any user, including admins. A weak or leaked default
therefore constitutes full compromise — critical severity.

## Startup guard rails

Two layers refuse to run with an insecure configuration when
`BVP_ENV=production`:

1. **Model validator** (`backend/src/bvphoenix/config.py`). Raised at
   `Settings()` construction, so even offline tools (migrations,
   import CLIs) bail out.
2. **Startup checks** (`backend/src/bvphoenix/startup_checks.py`).
   Invoked from `backend/src/bvphoenix/main.py` at module load time,
   before the first request is served.

Both layers consult a shared placeholder list and the length floor
(`>= 32` chars for HS256, per RFC 7518 §3.2).

In non-production environments the checks are skipped and an empty
`BVP_JWT_SECRET` is quietly replaced with the obvious marker
`dev-only-insecure-default`.

## Token shape

Post the 3.7.9 hardening (H1 in
[`security-2026-05-21-assessment.md`](./security-2026-05-21-assessment.md))
the backend uses PyJWT 2.10+ and every minted token carries:

| Claim | Purpose |
|---|---|
| `iss` | Issuer; mismatch is a hard reject. Env: `BVP_JWT_ISSUER`. |
| `aud` | Audience; mismatch is a hard reject. Env: `BVP_JWT_AUDIENCE`. |
| `iat` | Issued-at. |
| `nbf` | Not-before; future-dated tokens fail. |
| `exp` | Expiry; `BVP_JWT_EXPIRES_SECONDS`. |
| `jti` | Unique id; tracked via `revoked_tokens` so a revoke is enforceable mid-session. |

Clock-skew tolerance: `BVP_JWT_LEEWAY_SECONDS` (default modest). The
decode path uses `options.require=[iss, aud, iat, nbf, exp, jti]`,
so missing or malformed claims are rejected at the auth boundary.

## HS256 (default)

1. Generate a secret:

   ```sh
   python -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

2. Set it in the deployment environment:

   ```sh
   export BVP_JWT_SECRET=<output-from-step-1>
   ```

3. Rotate at least yearly. Rotation invalidates all outstanding tokens
   — acceptable because `BVP_JWT_EXPIRES_SECONDS` is 24 h by default.

## RS256 (opt-in)

Use RS256 when downstream services need to verify tokens without
holding the signing key.

1. Generate a 4096-bit RSA keypair:

   ```sh
   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 \
       -out jwt-private.pem
   openssl pkey -in jwt-private.pem -pubout -out jwt-public.pem
   ```

2. Configure the backend:

   ```sh
   export BVP_JWT_ALGORITHM=RS256
   export BVP_JWT_PRIVATE_KEY_PATH=/run/secrets/jwt-private.pem
   export BVP_JWT_PUBLIC_KEY_PATH=/run/secrets/jwt-public.pem
   ```

   `BVP_JWT_PUBLIC_KEY_PATH` is optional — if omitted we derive the
   public key from the private PEM.

3. Distribute `jwt-public.pem` (only) to verifying services.

## Other secrets

`startup_checks.run_startup_checks` also rejects production deploys
that:

- reuse the bundled MinIO access key or secret
  (`bvphoenix` / `bvphoenix-dev-secret` / `minioadmin`);
- leave `BVP_CORS_ORIGINS` empty or `"*"`.

`.env.example` ships only placeholder markers (`<...>`) — never real
defaults. `.gitignore` excludes `.env`, `.env.local`, and
`.env.*.local` while keeping `.env.example` tracked.

## Operational checklist

- [ ] Generate a fresh `BVP_JWT_SECRET` per environment.
- [ ] Rotate `BVP_S3_ACCESS_KEY` / `BVP_S3_SECRET_KEY` before first
      production deploy.
- [ ] Set `BVP_CORS_ORIGINS` to an explicit allowlist (no wildcard).
- [ ] Store all secrets in your platform's secret manager (Docker
      secrets, Kubernetes `Secret`, HashiCorp Vault, SOPS, …) — never
      in a tracked file.
- [ ] On rotation, restart all backend instances simultaneously; stale
      tokens will 401 and clients will re-auth.
