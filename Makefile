.PHONY: help up up.infra down logs ps build \
        backend.install backend.dev backend.test backend.lint \
        workers.install workers.dev workers.test \
        crawler.install crawler.dev crawler.test \
        mcp.install mcp.dev mcp.test \
        frontend.install frontend.dev frontend.build frontend.test frontend.lint \
        db.migrate db.revision db.reset \
        openapi.dump openapi.check \
        import \
        deploy.secrets deploy.verify deploy.apply deploy.migrate deploy.status \
        clean

help:
	@echo "bitvision phoenix — common tasks"
	@echo ""
	@echo "Full stack (compose):"
	@echo "  make up              Start full stack (infra + backend + workers + migrate)"
	@echo "  make up.infra        Start only infra services (for host-side dev)"
	@echo "  make build           Build backend/worker images"
	@echo "  make down            Stop stack"
	@echo "  make logs            Tail stack logs"
	@echo "  make ps              List services"
	@echo ""
	@echo "DICOM import tool:"
	@echo "  make import DIR=./slices OWNER=user@example.com [TIER=t1] [PUBLIC=1]"
	@echo ""
	@echo "Backend (FastAPI):"
	@echo "  make backend.install"
	@echo "  make backend.dev     Run API on :8000 with autoreload"
	@echo "  make backend.test"
	@echo "  make backend.lint"
	@echo ""
	@echo "Workers (Arq):"
	@echo "  make workers.install"
	@echo "  make workers.dev     Run Arq worker"
	@echo "  make workers.test"
	@echo ""
	@echo "Crawler (admin CLI):"
	@echo "  make crawler.install"
	@echo "  make crawler.dev"
	@echo ""
	@echo "MCP server:"
	@echo "  make mcp.install"
	@echo "  make mcp.dev         Run MCP server"
	@echo ""
	@echo "Frontend (Next.js):"
	@echo "  make frontend.install"
	@echo "  make frontend.dev    Run Next on :3000"
	@echo "  make frontend.build"
	@echo ""
	@echo "Database (Alembic):"
	@echo "  make db.migrate      Apply all migrations"
	@echo "  make db.revision m=\"msg\"  Generate a new migration"
	@echo "  make db.reset        Drop and recreate schema (dev only)"
	@echo ""
	@echo "Production deploy (wrappers around your private deploy repo; see docs/deployment.md):"
	@echo "  make deploy.secrets         Create/replace all k8s Secrets from secrets.env"
	@echo "  make deploy.verify          Print size of every key in every Secret"
	@echo "  make deploy.apply [svc=...]  Apply manifests (optionally one service: backend|workers|frontend|authentik|migrate)"
	@echo "  make deploy.migrate         Run Alembic upgrade head as a one-shot Job"
	@echo "  make deploy.status          Show rollout + pods + events"

# ---------- compose ----------
up:
	docker compose -f infra/docker-compose.yml up -d --build

up.infra:
	docker compose -f infra/docker-compose.yml up -d postgres redis minio minio-init

build:
	docker compose -f infra/docker-compose.yml build backend workers migrate

down:
	docker compose -f infra/docker-compose.yml down

logs:
	docker compose -f infra/docker-compose.yml logs -f --tail=100

ps:
	docker compose -f infra/docker-compose.yml ps

# ---------- backend ----------
backend.install:
	cd backend && uv sync

backend.dev:
	cd backend && uv run uvicorn bvphoenix.main:app --reload --port 8000

backend.test:
	cd backend && uv run pytest

backend.lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run python ../scripts/lint_phi_safe.py

openapi.dump:
	cd backend && uv run python ../scripts/dump_openapi.py

openapi.check:
	cd backend && uv run python ../scripts/check_openapi_diff.py

# ---------- workers ----------
workers.install:
	cd workers && uv sync

workers.dev:
	cd workers && uv run arq bvworkers.main.WorkerSettings

workers.test:
	cd workers && uv run pytest

# ---------- crawler ----------
crawler.install:
	cd crawler && uv sync

crawler.dev:
	cd crawler && uv run bvcrawler --help

crawler.test:
	cd crawler && uv run pytest

# ---------- mcp ----------
mcp.install:
	cd mcp && uv sync

mcp.dev:
	cd mcp && uv run python -m bvmcp.server

mcp.test:
	cd mcp && uv run pytest

# ---------- frontend ----------
frontend.install:
	cd frontend && pnpm install

frontend.dev:
	cd frontend && pnpm dev

frontend.build:
	cd frontend && pnpm build

frontend.test:
	cd frontend && pnpm test

frontend.lint:
	cd frontend && pnpm lint

# ---------- database ----------
db.migrate:
	cd backend && uv run alembic upgrade head

db.revision:
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

db.reset:
	cd backend && uv run alembic downgrade base && uv run alembic upgrade head

# ---------- DICOM import ----------
# Usage: make import DIR=/path/to/dicoms OWNER=admin@example.com [TIER=t1] [PUBLIC=1]
import:
	@test -n "$(DIR)" || (echo "DIR=... required" && exit 1)
	@test -n "$(OWNER)" || (echo "OWNER=<email> required" && exit 1)
	cd backend && uv run bvphoenix-import \
		--input "$(DIR)" \
		--owner-email "$(OWNER)" \
		--tier "$(or $(TIER),t1)" \
		$(if $(PUBLIC),--public,--private)

# ---------- Production deploy ----------
# Thin wrappers around the operator's PRIVATE deploy repo (not part of
# this open-source tree; kept out via .gitignore) so `make deploy.*`
# can run from the repo root. See docs/deployment.md for the generic,
# provider-agnostic procedure.
DEPLOY_DIR := deploy/bvphoenix-production-k8s-deploy
DEPLOY_SECRETS_DIR := ../bvphoenix-production-k8s-secrets

deploy.secrets:
	# Apply the declarative secret manifests from the operator's
	# private deploy tree. The specifics of how secret values are
	# sourced are an operator/deployment concern; this target is
	# intentionally provider-agnostic.
	cd $(DEPLOY_DIR) && kubectl apply -n bvphoenix-production -f secrets/

deploy.secrets-legacy:
	# Rollback path: re-create k8s Secrets from a local env file
	# instead of the declarative manifests. Operator-specific; see
	# the private deploy tree for the runbook.
	cd $(DEPLOY_DIR) && SECRETS_DIR=$(DEPLOY_SECRETS_DIR) ./legacy/provision-secrets.sh

deploy.verify:
	cd $(DEPLOY_DIR) && kubectl get secrets -n bvphoenix-production

# `make deploy.apply`            applies everything
# `make deploy.apply svc=backend` applies only one slice
deploy.apply:
	cd $(DEPLOY_DIR) && ./redeploy.sh $(or $(svc),all)

deploy.migrate:
	cd $(DEPLOY_DIR) && ./redeploy.sh migrate

deploy.status:
	cd $(DEPLOY_DIR) && ./check-status.sh

# ---------- housekeeping ----------
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
