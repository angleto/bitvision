# Development guide

What you need to run bitvision phoenix locally, how the pieces fit
together, and the conventions we follow. For architecture see
[`architecture.md`](./architecture.md). For the full API surface see
[`api-reference.md`](./api-reference.md).

---

## 1. Prerequisites

- **Docker** + **docker compose** (for Postgres/pgvector, Redis, MinIO).
- **Python 3.12** and [**uv**](https://docs.astral.sh/uv/) (used by backend / workers / crawler / mcp).
- **Node.js 20+** (22+ recommended) and [**pnpm**](https://pnpm.io/) (used by frontend).
- **GNU Make** (the repository's task runner is a plain `Makefile`).
- macOS, Linux, or WSL2. Native Windows is not tested.

Optional:

- `dcm2niix` is **not** required for the current code paths (volume
  packing is done in-process by NumPy stacking), but will be needed if
  you extend the worker to emit `.nii.gz` derivatives.

---

## 2. First-time setup

Run from the repo root.

```sh
cp .env.example .env                # fill in any secrets you need
make up.infra                       # start Postgres, Redis, MinIO, minio-init
make backend.install                # uv sync backend/
make workers.install                # uv sync workers/
make mcp.install                    # uv sync mcp/
make crawler.install                # uv sync crawler/
make frontend.install               # pnpm install frontend/
make db.migrate                     # alembic upgrade head
```

`make up` brings up the **full** stack in Docker (infra + backend +
worker + a one-shot migrator). Use `make up.infra` instead if you
want to run the app processes on the host for faster iteration.

### Seeding data

For local dev, use the bulk import CLI to populate Postgres + MinIO
with a DICOM folder:

```sh
make import DIR=/path/to/dicoms OWNER=admin@example.com TIER=t1 PUBLIC=1
```

This walks the folder, groups by StudyInstanceUID/SeriesInstanceUID,
uploads each instance to `bvphoenix-raw`, and inserts rows. The user
with email `OWNER` is created if missing. See
`backend/src/bvphoenix/cli/import_dicom.py`.

You also need to register a user for the frontend:

```sh
curl -sXPOST http://localhost:8000/api/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"secret123","display_name":"You"}'
```

---

## 3. Running services in dev

Each command runs in a separate terminal. All services autoreload on
source changes where supported.

| Service | Command | URL / channel |
|---------|---------|---------------|
| Backend | `make backend.dev` | http://localhost:8000 (Swagger at `/docs`) |
| Worker | `make workers.dev` | Arq consumer on Redis |
| Frontend | `make frontend.dev` | http://localhost:3000 |
| MCP | `make mcp.dev` | stdio (launched by MCP clients) |
| Crawler CLI | `make crawler.dev` | CLI (`bvcrawler --help`) |

Container variants (`backend` and `workers` baked into images) run
under `make up` and read `infra/docker-compose.yml` env.

### MCP client setup

For Claude Desktop or another stdio MCP client, point it at
`mcp/` and pass a user token:

```json
{
  "mcpServers": {
    "bitvision": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/bitvision_phoenix/mcp", "python", "-m", "bvmcp.server"],
      "env": {
        "BVP_MCP_BACKEND_BASE_URL": "http://localhost:8000",
        "BVP_MCP_USER_TOKEN": "<jwt from /api/auth/login>"
      }
    }
  }
}
```

---

## 4. Running tests

```sh
make backend.test       # pytest for backend/
make workers.test       # pytest for workers/
make mcp.test           # pytest for mcp/
make crawler.test       # pytest for crawler/
make frontend.test      # pnpm test for frontend/ (vitest)
```

Backend tests use an async SQLAlchemy test fixture against Postgres
(the dev `postgres` container is reused). MinIO and Redis must be
running for tests that touch storage or the queue.

### Severe-test suites (security & invariants)

Four files under `backend/tests/` are intentionally adversarial. They
codify the invariants that a regression *cannot* be allowed to break,
and each one has a clear "if this fails, here is the bug" framing.
Run them with the rest of the suite; they need a Postgres with F12
migrations applied (the dev `postgres` container is fine).

| File | Domain | What it pins |
|------|--------|--------------|
| `tests/test_versioning_security.py` | F12 + GDPR + publish | Cross-patient leak on `/at`/`/history`/`/diff`/`/ref-log`; tombstone semantics through delta chains; `execute_erasure` strips `entity_objects` of erased self-patients but preserves cross-dedup; publish never mutates source. |
| `tests/test_versioning_authz_concurrency.py` | Endpoint authz | Non-owner cannot merge / resolve / withdraw; `commit_change` serialises concurrent writes to the same ref; expired/revoked agent tokens fail auth; agent tokens scoped to other patients are rejected. |
| `tests/test_versioning_extras.py` | Sharing + branch isolation | Share-link auth refuses revoked / expired / not-yet-valid grants; consultation branches are disjoint; owner-as-proposer policy; **two parallel `fast_forward_merge` calls — exactly one wins**. |
| `tests/test_deid_text_italian.py` | OpenData de-id | Italian PHI matrix (CF, email, phone with multi-separator, dates, addresses); audit invariants (no plaintext retained, kind alphabet stable). |

When you hit a failure in one of these:

1. The test docstring tells you which invariant fired and where to
   look in production code.
2. Don't relax the assertion. Either (a) fix the production code to
   restore the invariant, or (b) move the case to an explicit
   `MISSED` / `xfail` list with a written justification — the
   regression will be visible to the next reader.
3. New test contributions to these files should follow the same
   structure: name the invariant in the docstring, hard-fail when
   it breaks.

The adversarial coverage was added together in F12.7 and surfaced
four bug fixes (delta-read crash, erasure leak, FF-merge race, phone
regex blind spot). See `docs/versioning.md §10` for the full
post-mortem.

---

## 5. Code style

### Python (backend / workers / mcp / crawler)

- `uv run ruff check .` and `uv run ruff format .` on every change.
  `make backend.lint` runs both in check mode.
- Type hints on every public function. `mypy --strict` where
  practical on new modules.
- 4-space indent, `from __future__ import annotations` at the top of
  modules that use forward references.
- Pydantic 2 + SQLAlchemy 2.0 async idioms.

### TypeScript (frontend)

- `biome check` is the single source of truth (replaces ESLint +
  Prettier). Run `make frontend.lint`.
- Strict TypeScript (`tsconfig.json`).
- Server components by default; mark client components with `"use
  client";` at the top (see `frontend/src/app/viewer/series/[id]/page.tsx`).

---

## 6. Commit convention

**Conventional Commits.** Prefixes in use:

- `feat:` new feature
- `fix:` bug fix
- `chore:` tooling, config, non-source changes
- `docs:` documentation only
- `refactor:` no behavior change
- `test:` tests only
- `perf:` perf improvement

Scope is optional: `feat(viewer): add cine mode`.

The git log in this repo follows this convention — `git log --oneline`
for examples. Add a DCO sign-off (`git commit -s`) to certify
authorship; see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## 7. Adding a migration

Alembic lives under `backend/alembic/`. All models under
`backend/src/bvphoenix/db/models/` are imported by
`db/models/__init__.py`, which Alembic's `env.py` consumes.

### Autogenerate

```sh
make db.revision m="add patient blood_type column"
```

Review the generated file under `backend/alembic/versions/`. Hand-edit
as needed (autogen misses some PostgreSQL-specific constructs, enums,
partial indexes, RLS policies).

### Apply

```sh
make db.migrate
```

### Reset (dev only)

```sh
make db.reset     # downgrade to base, then upgrade to head
```

Never run `db.reset` against a non-local database.

Naming: `0005_<short_description>.py` — the four-digit prefix keeps
the version order obvious in a directory listing.

---

## 8. Where to look for what

### Backend (`backend/src/bvphoenix/`)

| Subfolder | Role |
|-----------|------|
| `main.py` | FastAPI app, CORS, root + health endpoints, agent card route |
| `config.py` | `Settings` pydantic-settings, prefix `BVP_` |
| `api/` | HTTP routers (one module per resource) |
| `auth/` | JWT issuance, password hashing, FastAPI dependencies |
| `db/` | SQLAlchemy `Base`, async session, all models under `db/models/` |
| `services/` | Business logic — permissions, access levels, LLM provider, thumbnails, volume packing, A2A intent parsing + task store |
| `schemas/` | Cross-module Pydantic schemas (currently only `api/_schemas.py`) |
| `storage/` | `S3Storage` wrapper around boto3 |
| `cli/` | Click CLI entry points: `bvphoenix-import`, `bvphoenix-admin`, `bvphoenix-export` |

### Frontend (`frontend/src/`)

| Subfolder | Role |
|-----------|------|
| `app/` | Next.js App Router pages (`login`, `register`, `studies`, `patients`, `search/visual`, `shared/[token]`, `viewer/series/[id]`) |
| `components/` | React components (viewer pieces, share dialogs, annotation panels, similar-cases panel) |
| `lib/api.ts` | Typed REST client wrapper |
| `lib/auth-context.tsx` | React context for auth state + token storage |

### Workers (`workers/src/bvworkers/`)

| Subfolder | Role |
|-----------|------|
| `main.py` | Arq `WorkerSettings` (entry point) |
| `tasks/` | One module per task: `ping`, `pack_volume`, `embed_series`. `registry.py` registers them |
| `pipeline/` | Placeholder for higher-level pipelines |

### MCP (`mcp/src/bvmcp/`)

| Subfolder | Role |
|-----------|------|
| `server.py` | stdio MCP server, tool list + dispatch |
| `config.py` | `BVP_MCP_BACKEND_BASE_URL`, `BVP_MCP_USER_TOKEN` |
| `tools/` | One module per tool family: `studies`, `search`, `annotations`, `patients`; `client.py` is a shared httpx client |

### Crawler (`crawler/src/bvcrawler/`)

| Subfolder | Role |
|-----------|------|
| `cli.py` | Click entry point (`bvcrawler`) |
| `connectors/` | Plugin per upstream archive |

### Infra (`infra/`)

| Path | Role |
|------|------|
| `docker-compose.yml` | Full stack definition |
| `dockerfiles/` | Per-service Dockerfile |
| `postgres/01-init.sql` | Bootstrap SQL (pgvector extension, etc.) |

---

## 9. Troubleshooting

- **`database_url` not reachable** — confirm `make up.infra` ran and
  `docker ps` shows `postgres` + `redis` + `minio` healthy.
- **Empty viewer, `volume.raw` 500** — the series has no instances
  yet or pixel data is missing (structured reports, key objects).
  Check server logs for `NoPixelDataError`.
- **`ModuleNotFoundError: bvphoenix`** — run `make backend.install`
  (uv sync); the import fails when you haven't synced the venv after
  a pull.
- **`alembic.runtime.migration` errors** — make sure the sync DSN in
  `BVP_DATABASE_URL_SYNC` matches the running Postgres. Default is
  `postgresql+psycopg://...`, not `asyncpg`.
- **Browser can't fetch MinIO** — the compose override sets
  `BVP_S3_PUBLIC_ENDPOINT_URL=http://localhost:9000` so presigned
  URLs resolve from the browser. Override if you deploy elsewhere.
