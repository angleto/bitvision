# Production image for the Arq workers.
# Use from repo root: docker build -f infra/dockerfiles/workers.Dockerfile -t bvphoenix-workers .
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# This is a uv workspace: workers depend on the sibling ``bvphoenix``
# backend package via ``[tool.uv.sources] bvphoenix = { workspace =
# true }``. The workspace metadata + lockfile live at the repo root,
# so we stage that first, then the backend tree (so the workspace
# member resolves), then the workers tree.
COPY pyproject.toml uv.lock /app/
COPY backend /app/backend
# Backend pulls bvmcp from the workspace (single source of truth for
# the MCP scope catalog consumed by /api/ai-assistants). The mcp tree
# must be present even when workers don't import bvmcp directly,
# otherwise uv sync fails to resolve the workspace member.
COPY mcp /app/mcp

WORKDIR /app/workers
COPY workers/pyproject.toml workers/README.md ./
# ``--extra ai`` brings torch + sentence-transformers + open-clip so the
# chunk_and_embed_* tasks can populate text_embeddings (semantic
# retrieval). Without it the worker writes zero vectors and the search
# layer degrades to FTS-only.
# ``--extra seg`` brings TotalSegmentator (+ nnunetv2) so segment_auto
# can produce the ``segmentations/{series}/{label}.bin`` masks the
# viewer uses for organ exclusion on PET hot-spots and ROI stats.
# Without it the segment_auto task raises ``ModuleNotFoundError`` and
# the HotSpotsPanel "Esegui segmentazione automatica" button reports
# engine_error.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra ai --extra seg --no-install-project --no-dev || uv sync --extra ai --extra seg --no-install-project

COPY workers/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --extra ai --extra seg --no-dev

# FlagEmbedding (BGE-M3 sparse lexical_weights + ColBERT colbert_vecs)
# installed --no-deps: its real inference deps are pinned in the `ai` extra
# above; --no-deps drops the import-DEAD ir-datasets, whose zlib-state C
# extension is the only thing that needs gcc (the Phase-1 build failure).
# Pinned exactly because it is intentionally out of uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps "FlagEmbedding==1.4.0"

# Pre-fetch HF checkpoint, same reasoning as the backend image.
ENV HF_HOME=/app/.cache/huggingface
RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"
# Pre-bake BGE-M3 (BAAI/bge-m3, 1024-d dense via sentence-transformers,
# ~2.3 GB) so workers never download it at runtime — runtime HF fetch is
# slow/rate-limited and times out on CPU ARM (proven with BiomedCLIP).
# Cached in HF_HOME, copied into the runtime image below.
RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"
# Build-time smoke test of the FULL FlagEmbedding read-out path (construct +
# encode dense/sparse/colbert). Turns any missed --no-deps transitive dep
# into a BUILD failure, not a prod 500. The dense transformer loads from the
# ST-baked cache above; FlagEmbedding's sparse_linear/colbert_linear heads are
# fetched from HF here (CI egress, same as the ST pre-bakes) into HF_HOME, so
# they are baked into the runtime image — no runtime HF fetch.
RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/python -c "from FlagEmbedding import BGEM3FlagModel; m=BGEM3FlagModel('BAAI/bge-m3', use_fp16=False); m.encode(['ciao mondo'], return_dense=True, return_sparse=True, return_colbert_vecs=True)"

# ---

FROM python:3.12-slim
# uv places the workspace virtualenv at /app/.venv (workspace root),
# NOT /app/workers/.venv. Adjust PATH so the ``arq`` console script
# resolves.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" HF_HOME=/app/.cache/huggingface

# Build identity baked into the image; mirrored across all bvphoenix
# images so an operator can confirm at a glance which release is live.
ARG VERSION=""
ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV BVP_APP_VERSION=$VERSION \
    BVP_APP_GIT_SHA=$GIT_SHA \
    BVP_APP_BUILD_DATE=$BUILD_DATE

# System deps for DICOM / NIfTI processing + procps for pgrep
# (the k8s livenessProbe greps for the arq process name) +
# Tesseract OCR with all 24 official EU languages plus the European
# non-EU languages a clinical document might realistically arrive in
# (Norwegian, Icelandic, Ukrainian, Russian, Turkish, Albanian,
# Serbian Cyrillic + Latin, Catalan, Welsh, Basque, Galician).
# pyproject.toml mandates tesseract here because pytesseract is a
# thin subprocess wrapper — without the binary, _tesseract_fallback
# in services/ocr.py raises FileNotFoundError on every image-only
# PDF. The full pack adds ~250 MB to the image; runtime cost is paid
# only for the subset that actually loads (``BVP_OCR_LANGUAGES``
# setting + per-call ``language`` override on the OCR worker task).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        dcm2niix \
        libgl1 \
        libglib2.0-0 \
        procps \
        tesseract-ocr \
        tesseract-ocr-bul \
        tesseract-ocr-ces \
        tesseract-ocr-dan \
        tesseract-ocr-deu \
        tesseract-ocr-ell \
        tesseract-ocr-eng \
        tesseract-ocr-est \
        tesseract-ocr-fin \
        tesseract-ocr-fra \
        tesseract-ocr-gle \
        tesseract-ocr-hrv \
        tesseract-ocr-hun \
        tesseract-ocr-isl \
        tesseract-ocr-ita \
        tesseract-ocr-lav \
        tesseract-ocr-lit \
        tesseract-ocr-mlt \
        tesseract-ocr-nld \
        tesseract-ocr-nor \
        tesseract-ocr-pol \
        tesseract-ocr-por \
        tesseract-ocr-ron \
        tesseract-ocr-rus \
        tesseract-ocr-slk \
        tesseract-ocr-slv \
        tesseract-ocr-spa \
        tesseract-ocr-sqi \
        tesseract-ocr-srp \
        tesseract-ocr-srp-latn \
        tesseract-ocr-swe \
        tesseract-ocr-tur \
        tesseract-ocr-ukr \
        tesseract-ocr-cat \
        tesseract-ocr-cym \
        tesseract-ocr-eus \
        tesseract-ocr-glg \
    && rm -rf /var/lib/apt/lists/*

# Create the runtime user BEFORE the COPY so ownership is set during the
# copy, in a single layer. A post-copy ``chown -R /app`` rewrites every
# file's metadata and overlayfs copies-up the WHOLE ~5GB /app tree into a
# second layer — measured, it doubled the compressed image to ~10.5GB
# (two identical ~5.2GB layers). ``COPY --chown`` avoids the duplicate.
RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp

WORKDIR /app/workers
# Copy the entire /app tree from the builder so the workspace venv
# (/app/.venv) AND the bvphoenix sibling source (/app/backend/src,
# referenced by the venv's pth) AND the workers code come along.
COPY --from=builder --chown=bvp:bvp /app /app

# Drop root before runtime (matches backend.Dockerfile).
USER 1000:1000

CMD ["arq", "bvworkers.main.WorkerSettings"]
