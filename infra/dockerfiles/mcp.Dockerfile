# Production image for the MCP server.
# Use from repo root: docker build -f infra/dockerfiles/mcp.Dockerfile -t bvphoenix-mcp .
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/mcp
COPY mcp/pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev || uv sync --no-install-project

COPY mcp/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/mcp/.venv/bin:$PATH"

WORKDIR /app/mcp
COPY --from=builder /app/mcp /app/mcp

# Drop root before runtime.
RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp && \
    chown -R bvp:bvp /app
USER 1000:1000

CMD ["python", "-m", "bvmcp.server"]
