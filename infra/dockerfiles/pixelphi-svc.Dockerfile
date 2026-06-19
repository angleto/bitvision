# Production image for the CPU ONNX burned-in-pixel PHI detector service
# (M5 of the anonymizer hardening: the "hard case" tier consulted by the
# backend HttpPixelPhiEngine when the cheap Tesseract tier finds no text on a
# high-risk frame).
# ===============================================================
# Two stages:
#
#   1. builder, installs ONLY the lean runtime deps (fastapi, uvicorn,
#      onnxruntime, numpy, pillow, opencv-headless, pydantic) into a venv.
#      paddlepaddle / paddleocr / paddle2onnx are NOT installed here: they live
#      in the `export` extra and are used out-of-band by scripts/export_onnx.py
#      to produce the .onnx graph, never at build or runtime.
#
#   2. runtime, copies the venv in. The PP-OCRv5 detector graph is NOT baked
#      into the image: at deploy a model-sync init container pulls it from
#      s3://bvphoenix-models-prod/pixelphi/v1/ into BVP_PIXELPHI_MODEL_DIR
#      (/app/models), mirroring the embedding-model sync pattern. If the model
#      is absent the service still serves /healthz (model_loaded=false) and
#      /detect returns no boxes, the backend then fails closed to
#      over-redaction, so a missing model is safe, never a leak.
#
# Targets Scaleway Kapsule ARM64 nodes, CPU-only. onnxruntime and
# opencv-python-headless both ship manylinux aarch64 wheels, so no source build.
#
# Build from repo root:
#   docker build -f infra/dockerfiles/pixelphi-svc.Dockerfile \
#     -t bvphoenix-pixelphi-svc .
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/pixelphi-svc
COPY pixelphi-svc/pyproject.toml pixelphi-svc/README.md ./
# Runtime deps only, no `export` extra, so paddle* are absent. No `|| uv sync`
# fallback: a real dependency/lockfile error must fail the build loudly rather
# than silently fall back to a venv that includes the dev group.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev

COPY pixelphi-svc/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev

# ---

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/pixelphi-svc/.venv/bin:$PATH" \
    BVP_PIXELPHI_MODEL_DIR=/app/models

# opencv-python-headless + onnxruntime need libgomp1; opencv also needs
# libglib2.0-0 at import time even in the headless build.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Build identity baked into the image; matched across all bvphoenix images for
# at-a-glance release confirmation.
ARG VERSION=""
ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV BVP_APP_VERSION=$VERSION \
    BVP_APP_GIT_SHA=$GIT_SHA \
    BVP_APP_BUILD_DATE=$BUILD_DATE

# Create the runtime user before the COPY so ``--chown`` sets ownership in one
# layer; a post-copy ``chown -R /app`` would duplicate the whole tree.
RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp

WORKDIR /app/pixelphi-svc
COPY --from=builder --chown=bvp:bvp /app/pixelphi-svc /app/pixelphi-svc
# Empty model dir; the model-sync init container populates it at deploy. Owned
# by the runtime uid so a shared emptyDir volume is writable by the init step
# and readable here.
RUN install -d -o bvp -g bvp /app/models

# Drop root before runtime.
USER 1000:1000

EXPOSE 8091
# Bind 0.0.0.0:8091 by default; override BVP_PIXELPHI_HOST/PORT in the K8s
# deployment if a sidecar / mesh expects a different binding.
CMD ["python", "-m", "bvpixelphi.app"]
