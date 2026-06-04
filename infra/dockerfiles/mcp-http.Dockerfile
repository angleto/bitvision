# Production image for the remote MCP HTTP server (ADR 0019).
#
# Built and tagged via the same CI matrix as the other services (see
# .github/workflows/ci.yml `docker-images` job). Reuses the bvmcp
# Python package: only the entry point differs (server_http vs.
# server). Kept in its own Dockerfile so the stdio image stays
# minimal — production users hitting Claude.ai never want stdio
# pulled in, and neither do CD pipelines for the stdio variant.
#
# Build from repo root:
#   docker build -f infra/dockerfiles/mcp-http.Dockerfile -t bvphoenix-mcp-http .
#
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/mcp
COPY mcp/pyproject.toml mcp/README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev || uv sync --no-install-project

COPY mcp/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/mcp/.venv/bin:$PATH"

# Build identity baked into the image; matched across all bvphoenix
# images for at-a-glance release confirmation.
ARG VERSION=""
ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV BVP_APP_VERSION=$VERSION \
    BVP_APP_GIT_SHA=$GIT_SHA \
    BVP_APP_BUILD_DATE=$BUILD_DATE

# Create the runtime user before the COPY so ``--chown`` sets ownership in
# one layer; a post-copy ``chown -R /app`` duplicates the whole tree.
RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp

WORKDIR /app/mcp
COPY --from=builder --chown=bvp:bvp /app/mcp /app/mcp

# Drop root before runtime.
USER 1000:1000

EXPOSE 8080
# Default to 0.0.0.0:8080. Override BVP_MCP_HTTP_HOST/PORT in the K8s
# deployment if a sidecar / mesh expects a different binding.
CMD ["python", "-m", "bvmcp.server_http"]
