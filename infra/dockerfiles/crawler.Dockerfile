# Production image for the crawler CLI.
# Use from repo root: docker build -f infra/dockerfiles/crawler.Dockerfile -t bvphoenix-crawler .
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/crawler
COPY crawler/pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev || uv sync --no-install-project

COPY crawler/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/crawler/.venv/bin:$PATH"

# Create the runtime user before the COPY so ``--chown`` sets ownership in
# one layer; a post-copy ``chown -R /app`` duplicates the whole tree.
RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp

WORKDIR /app/crawler
COPY --from=builder --chown=bvp:bvp /app/crawler /app/crawler

# Drop root before runtime.
USER 1000:1000

ENTRYPOINT ["bvcrawler"]
