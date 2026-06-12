# Production image for the inbound-email MTA adapter (task fbbf5270 §5).
#
# A dumb SMTP→HTTP bridge: terminates port 25 (in-cluster 2525, see the
# Service), validates RCPTs against the backend and forwards raw
# messages. Carries NO S3/DB credentials by construction — the only
# secret in its environment is BVP_INBOUND_INTERNAL_SECRET.
#
# Build from repo root:
#   docker build -f infra/dockerfiles/mta.Dockerfile -t bvphoenix-mta .
#
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/mta
COPY mta/pyproject.toml mta/README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev || uv sync --no-install-project

COPY mta/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/mta/.venv/bin:$PATH"

ARG VERSION=""
ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV BVP_APP_VERSION=$VERSION \
    BVP_APP_GIT_SHA=$GIT_SHA \
    BVP_APP_BUILD_DATE=$BUILD_DATE

RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp

WORKDIR /app/mta
COPY --from=builder --chown=bvp:bvp /app/mta /app/mta

# Unprivileged: the Service maps LB port 25 -> container 2525, so no
# CAP_NET_BIND_SERVICE is needed.
USER 1000:1000

EXPOSE 2525
CMD ["python", "-m", "bvmta.server"]
