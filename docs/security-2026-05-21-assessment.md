# Security assessment & hardening — 2026-05-21

This document captures the multi-domain security review that landed in
release **3.7.9** and the remediation pass that closes every critical
and high-severity finding. It is the canonical reference for what was
fixed, why, and where the regression tests live.

## Scope of the review

Seven parallel agents covered:

  1. AuthN / AuthZ surface + scope / cross-tenant isolation;
  2. Public-facing surfaces (share links, calendar feed, OpenData)
     + PHI handling + S3 / DB isolation;
  3. Input validation: injection, SSRF, path traversal, file upload,
     XXE, deserialisation;
  4. MCP server write paths: scope, agent provenance, audit trail,
     idempotency / ETag / dry_run;
  5. Secrets management, k8s deploy, Dockerfile hardening, supply
     chain;
  6. Frontend security model: XSS, CSP, CSRF, cookie security, open
     redirect;
  7. Operational robustness: timeouts, retry/backoff, idempotency,
     race conditions, recovery, observability.

Hallucinated findings (e.g. CVE-2026-39892 — does not exist in NVD)
were filtered before remediation. Every claim below was reverified
against the source tree before action.

## Findings + remediation

### Critical (production data at risk, sfruttabili)

| ID | Finding | Code paths | Remediation |
|----|---------|-----------|-------------|
| C1 | Open redirect on `/login` (``router.push(search.get("next"))``) | `frontend/src/app/login/page.tsx:58` | New `frontend/src/lib/safe-redirect.ts` validates the candidate (rejects `https://`, `//`, `javascript:`, NUL, backslash escapes). Tests in `__tests__/safeRedirect.test.ts`. |
| C2 | JWT in `localStorage` (XSS → account takeover) | `frontend/src/lib/api.ts`, `auth-context.tsx`, 24 consumer files | Backend sets `bvp_session` cookie (HttpOnly+Secure+SameSite=Lax) on every token-mint endpoint (`/login`, `/login-mfa`, `/verify-email`). SPA uses `credentials: "include"` everywhere; `getStoredToken` / `setStoredToken` retained as no-op shims. `/api/auth/logout` clears the cookie server-side. |
| C3 | `recipient_email` / `recipient_name` exposed on public `/shared/{token}/info` | `backend/src/bvphoenix/api/sharing.py:311` (`ShareInfoOut`), `:1872` (builder) | Removed both fields from the response model + builder. `tests/test_share_recipient_pii_redaction.py` pins the invariant. |
| C4 | Pods ran as root, no `securityContext` | `deploy/bvphoenix-production-k8s-deploy/*-deployment.yaml`, `*-job.yaml`, `infra/dockerfiles/*` | Every Dockerfile creates user `bvp` (uid 1000) — frontend retains its existing `nextjs` user (uid 1001). Pod spec enforces `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities: drop ALL`, `seccompProfile: RuntimeDefault`. Frontend + mcp-http additionally turn on `readOnlyRootFilesystem`. |
| C5 | No CSP / HSTS / X-Frame-Options / Referrer-Policy | `frontend/next.config.mjs`, `deploy/.../ingress/` | Next.js `headers()` emits the full bundle (CSP `script-src 'self' 'wasm-unsafe-eval'`, `frame-ancestors 'none'`, HSTS `max-age=63072000`, XFO `DENY`, Referrer-Policy, Permissions-Policy). Re-asserted at the Traefik edge via `middleware-security-headers.yaml`. |
| C6 | ZIP slip in bulk upload | `backend/src/bvphoenix/api/bulk_upload.py:210` | New `_is_safe_archive_member_name(name)` helper rejects `..`, absolute paths, NUL bytes, Windows escapes. Applied in `_unpack_zip` + the ISO walker. Crafted-archive regression test in `test_bulk_upload_zip_slip.py`. |

### High

| ID | Finding | Remediation |
|----|---------|-------------|
| H1 | `python-jose` unmaintained (CVE-2022-29217); no `aud`/`iss`/`nbf` claim or validation on local JWT | Migrated to PyJWT 2.10+. Tokens now carry `iss`/`aud`/`iat`/`nbf`/`exp`/`jti`; decode enforces all of them with `options.require`. New settings `BVP_JWT_ISSUER`, `BVP_JWT_AUDIENCE`, `BVP_JWT_LEEWAY_SECONDS`. Tests: `test_jwt_claims_hardening.py`. |
| H2 | Per-assistant secret revocation gap | New `revoked_at` column on `agent_assistants` (migration `0006_agent_assistants_revoked_at.py`). New endpoint `POST /api/ai-assistants/{id}/revoke` zeros the hash, sets `is_active=false`, stamps the timestamp. Auth resolver enforces both flags. Tests: `test_agent_assistant_revoke.py`. |
| H3 | `Idempotency-Key` not enforced on MCP sharing mint tools | `create_study_share_link` and `create_folder_share_link` now accept and propagate `idempotency_key`. Tests: `test_sharing_tools.py::test_create_study_share_link_propagates_idempotency_key_header`. |
| H4 | Multi-write endpoints lacked explicit transactions | Verified: SQLAlchemy AsyncSession with `autobegin=True` (default) already guarantees implicit atomicity between the first `execute()` and `commit()`. Finding withdrawn after source review. |
| H5 | DB pool not dimensioned | `create_async_engine` now passes `pool_size=10`, `max_overflow=10`, `pool_recycle=3600`, `pool_timeout=30`, `connect_args.statement_cache_size=250`. |
| H6 | OpenData tier isolation enforced only in Python | New CHECK constraint `ck_imaging_studies_public_tier_t4`: `is_public = FALSE OR contribution_tier = 't4'`. Migration `0007_opendata_pathology_constraints.py`. |
| H7 | Pathology PHI label policy not DB-enforced | New CHECK `ck_pathology_slides_label_redacted_when_present`: `s3_label_key IS NULL OR label_redacted = TRUE`. Same migration. |
| H8 | No `NetworkPolicy` in the namespace | New `network-policies.yaml`: workers carry a hard `deny-all-ingress`; backend, frontend, mcp-http each declare a port-scoped ingress policy. Egress left default-allow until the managed-service CIDRs stabilise. |
| H9 | Race on `add_tag_to_study` | `PATCH /api/studies/{id}/tags` now executes `SELECT ... FOR UPDATE` on the study row before reading the existing tag set. Lost-update bug closed. |
| H10 | WSI file-size cap missing | New setting `BVP_WSI_MAX_BYTES` (default 30 GiB). `import_pathology_slide` rejects oversized files with `ValueError` before OpenSlide memory-maps them. Tests: `test_pathology_wsi_size_cap.py`. |
| H11 | `patients.py` cross-patient guard coverage | Audited every write route under `/patients/{patient_id}/...`. Two outstanding holes (`create_patient_share` :5096, `publish_patient` :5755) now call `enforce_agent_patient_scope`. CI regression test in `tests/security/test_patients_cross_patient_guard.py` walks the AST. |

### Medium

| ID | Finding | Remediation |
|----|---------|-------------|
| M1 | OIDC httpx timeouts collapsed to a single number | `httpx.Timeout(connect=5, read=15, write=5, pool=5)` shared across the three OIDC fetches. |
| M5 | ESO `refreshInterval: 1h` | Lowered to `5m` across all eight ExternalSecret resources. |
| M8 | bcrypt cost factor implicit | `bcrypt.gensalt(rounds=14)` pinned explicitly. |

### Lower-severity items deliberately left for a follow-up

* M2 (per-task arq job timeouts) — needs a per-task registry; tracked
  separately.
* M3 (idempotent alembic migrations) — historical migrations are
  frozen baseline; new ones already use `IF NOT EXISTS`.
* M4 (structlog `request_id` middleware) — requires a tracing decision
  (OTel vs custom); deferred.
* M7 (Docker base-image digest pinning) — needs a Renovate config
  before it's worth doing manually.
* The legacy `secrets.env` file on the local deploy host still
  contains historically-exposed credentials. The operator action
  ``rotate exposed deploy creds`` from memory ``oss_release_phi_blocker``
  is still pending and outside the scope of this code change.

## Test surface added in this pass

* `frontend/__tests__/safeRedirect.test.ts` (7 cases — C1)
* `backend/tests/test_share_recipient_pii_redaction.py` (2 cases — C3)
* `backend/tests/test_bulk_upload_zip_slip.py` (3 cases — C6)
* `backend/tests/test_jwt_claims_hardening.py` (10 cases — H1)
* `backend/tests/test_agent_assistant_revoke.py` (2 invariant cases — H2)
* `backend/tests/test_pathology_wsi_size_cap.py` (2 cases — H10)
* `backend/tests/security/test_patients_cross_patient_guard.py`
  (AST-based audit — H11)
* `mcp/tests/test_sharing_tools.py` (3 new cases — H3)

## Cookie-based auth migration notes

* Endpoint inventory that emits the cookie:
  ``POST /api/auth/login``, ``POST /api/auth/login-mfa``,
  ``POST /api/auth/verify-email``. Each one keeps the legacy
  ``{ access_token, token_type }`` body for non-browser callers
  (curl smoke tests, ``bvphoenix-import`` CLI, MCP integration tests
  that don't go through the cookie path).
* ``POST /api/auth/logout`` clears the cookie via
  ``Response.delete_cookie``.
* ``backend/src/bvphoenix/auth/deps.py::_creds_from_request`` resolves
  the bearer token from the Authorization header *or* the
  ``bvp_session`` cookie. Header wins when both are present (explicit
  client overrides implicit browser session).
* No CSRF token is shipped: ``SameSite=Lax`` blocks cross-site POST
  navigation, and every mutating endpoint only accepts ``application/json``
  bodies. Top-level form-data POST therefore cannot exfiltrate or
  mutate.

## Operational follow-up

The Kubernetes hardening changes (C4, C5, H8) require a smoke test
on staging before the next prod rollout because:

* a misconfigured ``readOnlyRootFilesystem`` can crash the frontend
  pod at boot (Next.js writes ``.next/cache`` lazily — kept off for
  backend / workers for the same reason),
* a `NetworkPolicy` typo blocks all ingress instantly. The current
  bundle (``network-policies.yaml``) declares ingress port allow-lists
  without source restrictions to keep kubelet probes alive, so the
  blast radius is contained, but apply it during a low-traffic window
  anyway.

The Traefik `Middleware` (``middleware-security-headers.yaml``) is
referenced by the Ingress via the
``traefik.ingress.kubernetes.io/router.middlewares`` annotation; apply
the middleware **before** re-applying the Ingress.
