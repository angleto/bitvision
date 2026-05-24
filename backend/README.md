# backend — REST API

FastAPI backend for bitvision phoenix. Exposes the REST API consumed by
the frontend, the crawler, and LLM agents (via MCP).

## Stack

- FastAPI + Uvicorn
- SQLAlchemy 2.0 async + asyncpg + Alembic
- Pydantic 2 + pydantic-settings
- `boto3` for S3-compatible object storage
- `PyJWT` 2.10+ for JWT (post-3.7.9 H1 migration from `python-jose`;
  every token carries `iss`/`aud`/`iat`/`nbf`/`exp`/`jti`)
- `authlib` for OIDC
- `arq` client for dispatching async jobs to workers

## Run locally

```sh
# from repo root
make up               # infra (postgres, redis, minio)
make backend.install  # uv sync
make db.migrate       # alembic upgrade head
make backend.dev      # uvicorn on :8000, autoreload
```

OpenAPI docs: http://localhost:8000/docs
Health check: http://localhost:8000/health

## Layout

```
src/bvphoenix/
  main.py          FastAPI app, root + health endpoints
  config.py        Settings from env (prefix BVP_)
  api/             HTTP route handlers (routers added phase by phase)
  db/              SQLAlchemy engine, session, declarative Base
  services/        Business logic
  schemas/         Pydantic request/response models
alembic/           Database migrations (`0001_initial_schema.py` is the
                   post-OSS-release baseline; see `../docs/data-model.md §9`)
tests/             Pytest tests
```

## Testing

```sh
make backend.test
```

The suite ships with four adversarial files that codify the
non-negotiable security invariants. See
`../docs/development-guide.md §"Severe-test suites"` for the catalogue.
Quick pointers:

- `tests/test_versioning_security.py` — F12 + GDPR + publish isolation.
- `tests/test_versioning_authz_concurrency.py` — endpoint authz, ref-lock race, agent tokens.
- `tests/test_versioning_extras.py` — share-link auth, branch isolation, **parallel fast_forward_merge** (one wins, one diverges).
- `tests/test_deid_text_italian.py` — Italian PHI regex coverage matrix.

A failure in any of these signals a real regression on a security
contract — read the test's docstring before relaxing the assertion.

## Style

- `ruff check . && ruff format .` — run on every change
- Type hints on all public functions; `mypy --strict` where practical

## Implemented surfaces

The backend has grown well past the scaffold. Currently in:

- Authentication: bearer JWT, OIDC (authlib), agent tokens with
  per-patient scope, share-link synthetic public users, MFA / TOTP,
  password reset, email verification.
- Resources: studies, series, instances, reports, annotations,
  patients, patient documents, consultations, folders, tags,
  measurements, segmentations.
- Upload flow: presigned PUT to S3 + content-hash dedup +
  classifier + DICOM + non-DICOM ingestion paths.
- Authorization: `services/permissions` predicates + RLS policies on
  every resource table and on the F12 versioning tables (production
  hardening note: see
  [`../docs/security-rls.md`](../docs/security-rls.md) §"Production
  hardening required" — RLS is decorative when the app role coincides
  with the table owner).
- Search: metadata + pgvector embeddings + full-text + similar-cases.
- Versioning (F12): git-like patient fascicolo with commit DAG,
  consultation branches, three-way merge, fast-forward,
  pack-on-GC delta encoding, time-travel API. See
  `../docs/versioning.md`.
- Publish: clone-and-scrub a private fascicolo to OpenData with
  regex baseline + optional LLM scrub. See
  `../docs/versioning.md §"Publish"`.
- GDPR: erasure with entity_objects tombstoning + cross-patient
  dedup safety. See `../docs/security-gdpr.md`.
- A2A: Agent-to-Agent JSON-RPC with task store.
- LLM: provider abstraction, BYOK, cost ledger, transparency log.
- Sharing: share links with passwords, expiry, max-uses, per-grant
  de-identification.

See `../docs/*.md` for the per-feature design docs.
