# Production image for the FastAPI backend.
# Use from repo root: docker build -f infra/dockerfiles/backend.Dockerfile -t bvphoenix-backend .
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Workspace setup. The backend pyproject pulls bvmcp from the
# workspace (single source of truth for the MCP scope catalog
# consumed by /api/ai-assistants), so the build context needs the
# workspace root + both members. Without mcp/ uv sync fails with
# "Failed to parse entry: bvmcp".
COPY pyproject.toml uv.lock /app/
COPY backend /app/backend
COPY mcp /app/mcp

WORKDIR /app/backend
# ``--extra ai`` pulls torch + sentence-transformers so chunk_search can
# fall through to semantic vector retrieval instead of FTS-only when a
# query has no exact keyword match. Worth the +500 MB on the image.
# ``--extra idc`` adds the idc-index client (lazy-imported, used only by the
# bvphoenix-public-import `idc` adapter to ingest IDC-hosted collections such
# as NLST that the NBIA v1 API does not serve). Small next to the ai extra.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra ai --extra idc --no-install-project --no-dev || uv sync --extra ai --extra idc --no-install-project
RUN --mount=type=cache,target=/root/.cache/uv uv sync --extra ai --extra idc --no-dev

# FlagEmbedding (BGE-M3 sparse + ColBERT query encoding) installed --no-deps:
# real inference deps are pinned in the `ai` extra; --no-deps drops the
# import-dead ir-datasets (zlib-state C ext / gcc). Pinned, out of uv.lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps "FlagEmbedding==1.4.0"

# Model weights are NOT baked into the image (task d11a0b0f fix 2).
# MiniLM + BGE-M3 (+ FlagEmbedding heads) live in
# s3://bvphoenix-models-prod/hf-cache/v1/; the ``model-sync``
# initContainer in backend-deployment.yaml syncs them into the
# HF_HOME emptyDir at pod start (intra-cloud, fast). This cuts
# ~2.8 GB off the image and removes the HuggingFace download — the
# recurring 429 build flake — from the CI critical path entirely.
# Import-only smoke test: a missed --no-deps transitive dep must stay
# a BUILD failure, not a prod 500 (no weights are downloaded here).
RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/python -c "import FlagEmbedding, sentence_transformers"

# ---

FROM python:3.12-slim
# uv places the workspace virtualenv at /app/.venv (workspace root),
# NOT /app/backend/.venv. Adjust PATH so the ``uvicorn`` console
# script resolves.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" HF_HOME=/app/.cache/huggingface

# Memory: bound glibc's per-thread malloc arenas. Unpacking a packed
# DICOM volume allocates 100-500 MB float32 arrays per ROI/wash-out
# request; with the default arena count (8 x nproc) glibc keeps the
# freed chunks on per-arena free lists and the resident set climbs
# request-after-request (measured: fresh pod ~200 MB, after viewer
# traffic ~1.6 GB against the 2Gi limit) until the next big unpack
# OOMKills the pod (exit 137 -> client 502s). MALLOC_ARENA_MAX=2 keeps
# freed chunks poolable in 2 arenas; services/memory.release_memory()
# (malloc_trim) hands the free pages back to the kernel after each op.
ENV MALLOC_ARENA_MAX=2

# Build identity baked into the image; surfaced by GET /api/version
# and by the build-info card on /settings. Empty outside CI.
ARG VERSION=""
ARG GIT_SHA=""
ARG BUILD_DATE=""
ENV BVP_APP_VERSION=$VERSION \
    BVP_APP_GIT_SHA=$GIT_SHA \
    BVP_APP_BUILD_DATE=$BUILD_DATE

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        p7zip-full \
        # cairosvg native deps. libcairo2 is the renderer; libpango +
        # libpangocairo handle text shaping/layout so CJK and ligatures
        # don't fall back to .notdef boxes in the care-timeline PDF.
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        # Whole-slide imaging support (pathology / vetrini).
        # libvips42 is dlopen'd by pyvips for thumbnail / DZI
        # generation; the *-dev package is not needed at runtime.
        # libopenslide is NOT installed via apt: openslide-bin (pip)
        # ships the native binary in the wheel so we get a uniform
        # version across Linux / macOS dev without an apt drift risk.
        libvips42 \
        tesseract-ocr \
        # The 24 official EU languages plus the European non-EU
        # languages a clinical document might realistically arrive
        # in. Each traineddata pack is ~5-15 MB so the full set adds
        # ~250 MB to the image; runtime cost is paid only for the
        # subset that actually loads (``BVP_OCR_LANGUAGES`` setting +
        # per-call ``language`` override on the ``run_ocr`` API).
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
# second layer — measured, it doubled the compressed image to ~10.2GB
# (two identical ~5.0GB layers). ``COPY --chown`` avoids the duplicate.
# Uid/gid 1000 are baked into the image and pinned in the k8s
# securityContext (runAsNonRoot: true) to keep filesystem ownership
# aligned; a missing USER directive would fail admission.
RUN groupadd --system --gid 1000 bvp && \
    useradd --system --uid 1000 --gid bvp --home-dir /app --shell /usr/sbin/nologin bvp

WORKDIR /app/backend
# Bring the whole workspace (venv + backend + mcp source) so the
# entrypoint can resolve both bvphoenix and bvmcp imports.
COPY --from=builder --chown=bvp:bvp /app /app

# Drop root before runtime.
USER 1000:1000

EXPOSE 8000
CMD ["uvicorn", "bvphoenix.main:app", "--host", "0.0.0.0", "--port", "8000"]
