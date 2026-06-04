# Production image for the CPU ONNX BiomedCLIP inference service
# (Phase E of the search overhaul).
# ===============================================================
# Two stages on purpose:
#
#   1. `exporter` — installs the heavy `export` extra (torch +
#      open_clip), downloads the ~500 MB BiomedCLIP checkpoint once, and
#      runs scripts/export_onnx.py to materialise the two ONNX graphs +
#      the text tokenizer.json into /export/models. torch/open_clip never
#      reach the runtime layer.
#
#   2. runtime — installs ONLY the lean runtime deps (fastapi, uvicorn,
#      onnxruntime, numpy, pillow, pydantic) and copies the exported
#      /export/models in. The result runs the ViT on onnxruntime CPU; no
#      GPU, no torch.
#
# Targets Scaleway Kapsule ARM64 nodes, CPU-only. onnxruntime ships
# manylinux aarch64 wheels so no source build is needed.
#
# Build from repo root:
#   docker build -f infra/dockerfiles/inference-svc.Dockerfile \
#     -t bvphoenix-inference-svc .
FROM python:3.12-slim AS exporter

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/root/.cache/huggingface

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/inference-svc
COPY inference-svc/pyproject.toml inference-svc/README.md ./
# `--extra export` pulls torch (CPU) + open_clip just for the export step.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra export --no-install-project --no-dev \
      || uv sync --extra export --no-install-project

COPY inference-svc/src ./src
COPY inference-svc/scripts ./scripts

# Export the two ONNX graphs + tokenizer. The HF checkpoint download is
# cached on the BuildKit cache mount so re-builds that don't bump the
# model skip the ~500 MB pull.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/huggingface \
    /app/inference-svc/.venv/bin/python scripts/export_onnx.py --out-dir /export/models

# ---

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/inference-svc
COPY inference-svc/pyproject.toml inference-svc/README.md ./
# Runtime deps only — no `export` extra, so torch/open_clip are absent.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev || uv sync --no-install-project

COPY inference-svc/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/inference-svc/.venv/bin:$PATH" \
    BVP_INFERENCE_MODEL_DIR=/app/models

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

WORKDIR /app/inference-svc
COPY --from=builder --chown=bvp:bvp /app/inference-svc /app/inference-svc
# The exported ONNX graphs + tokenizer.json from the exporter stage.
COPY --from=exporter --chown=bvp:bvp /export/models /app/models

# Drop root before runtime.
USER 1000:1000

EXPOSE 8090
# Bind 0.0.0.0:8090 by default; override BVP_INFERENCE_HOST/PORT in the
# K8s deployment if a sidecar / mesh expects a different binding.
CMD ["python", "-m", "bvinference.app"]
