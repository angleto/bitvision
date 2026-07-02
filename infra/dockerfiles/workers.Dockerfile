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

# Model weights are NOT baked into the image (task d11a0b0f fix 2).
# They live in s3://bvphoenix-models-prod/hf-cache/v1/ and the
# ``model-sync`` initContainer in workers-deployment.yaml syncs them
# into the HF_HOME emptyDir at pod start (intra-cloud, fast). This
# cuts ~2.8 GB off the image and removes the HuggingFace download —
# the recurring 429 build flake — from the CI critical path entirely.
# Import-only smoke test: a missed --no-deps transitive dep must stay
# a BUILD failure, not a prod 500 (no weights are downloaded here).
RUN --mount=type=cache,target=/root/.cache/uv \
    /app/.venv/bin/python -c "import FlagEmbedding, open_clip, sentence_transformers"

# Interactive click-to-segment engine: SAM-2 (Segment Anything 2), CPU-only.
#   * There is no PyPI release, so pin an exact upstream commit.
#   * ``SAM2_BUILD_CUDA=0`` skips the optional CUDA extension (the nodes are
#     CPU-only); ``SAM2_BUILD_ALLOW_ERRORS=1`` keeps a missing ext non-fatal.
#   * ``--no-deps`` (mirrors FlagEmbedding above): sam2 pins torch>=2.5.1 /
#     torchvision, and letting the resolver see that would REINSTALL torch and
#     bloat/skew the ARM image. torch/torchvision/numpy/tqdm/pillow already come
#     from the ``ai`` extra; only hydra-core + iopath are genuinely new, added
#     explicitly. ``--no-build-isolation`` reuses the venv's torch/setuptools
#     for the (extension-free) build instead of fetching a second torch.
ENV SAM2_BUILD_CUDA=0 SAM2_BUILD_ALLOW_ERRORS=1
# Installed from the GitHub archive TARBALL, not ``git+…``: the slim builder
# has no ``git`` binary (which uv would need for a git URL), and a commit-SHA
# tarball pins the exact revision just as tightly. No ``name @`` prefix either
# — the sdist's metadata name is ``sam-2`` (hyphen) and uv rejects a
# ``sam2 @ …`` spec as a name mismatch; letting uv read the name from the
# built metadata avoids that.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python setuptools wheel && \
    uv pip install --python /app/.venv/bin/python --no-deps --no-build-isolation \
      "https://github.com/facebookresearch/sam2/archive/2b90b9f5ceec907a1c18123530e92e794ad901a4.tar.gz" && \
    uv pip install --python /app/.venv/bin/python hydra-core iopath

# Bake the Apache-2.0 SAM-2.1 hiera-tiny checkpoint (~149 MB) from Meta's
# stable CDN (NOT HuggingFace — this path has no 429 flake). Unlike the big
# embedding models (which live in the model-sync bucket keyed on the HF hub
# layout), this single small checkpoint is baked so the feature is fully
# self-contained and works OFFLINE at runtime — no bucket seeding, no per-node
# cache re-sync. The default engine is deliberately the Apache checkpoint;
# MedSAM-2 (research/education-only license) is an operator opt-in via
# BVP_MEDSAM_CKPT/BVP_MEDSAM_CFG. The config yaml ships inside the sam2 package.
RUN mkdir -p /app/models/sam2 && \
    /app/.venv/bin/python -c "import urllib.request; urllib.request.urlretrieve('https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt', '/app/models/sam2/sam2.1_hiera_tiny.pt')" && \
    /app/.venv/bin/python -c "import os; assert os.path.getsize('/app/models/sam2/sam2.1_hiera_tiny.pt') > 100_000_000, 'sam2 checkpoint truncated'"

# Import + config-resolution smoke test: a broken sam2 install (or a moved
# config path in a future pin bump) must be a BUILD failure, not a prod 502 on
# the first click. Import only — no weights loaded, no inference run here.
RUN /app/.venv/bin/python -c "import sam2, hydra, iopath; from sam2.build_sam import build_sam2; from sam2.sam2_image_predictor import SAM2ImagePredictor"

# ---

FROM python:3.12-slim
# uv places the workspace virtualenv at /app/.venv (workspace root),
# NOT /app/workers/.venv. Adjust PATH so the ``arq`` console script
# resolves.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" HF_HOME=/app/.cache/huggingface

# pack_volume unpacks 100-500MB DICOM stacks into float32 NumPy volumes; up
# to ``max_jobs`` (10) can run concurrently. The glibc allocator keeps freed
# arenas resident, so RSS ratchets up across packs and a later large stack can
# OOMKill the pod. Cap the arenas (same rationale as the backend image) so
# pages are returned to the OS between packs.
ENV MALLOC_ARENA_MAX=2

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
        libvips42 \
        libopenslide0 \
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

# WSI tiling smoke test: pyvips is an FFI wrapper that dlopen's
# libvips.so.42 lazily, and ``Image.openslideload`` only exists when
# libvips was built/linked against libopenslide. A missing native lib
# must be a BUILD failure here, not a prod 500 when the first
# ``tile_wsi`` job fires (mirrors the FlagEmbedding import smoke test
# in the builder stage). ``openslideload`` covers SVS/NDPI/MRXS/DICOM-WSI;
# the core JPEG/PNG/TIFF loaders (ordinary gross/micrograph images)
# come with libvips42 unconditionally.
RUN /app/.venv/bin/python -c "import pyvips; assert pyvips.type_find('VipsOperation', 'openslideload'), 'libvips built without openslide support — apt libopenslide0 missing'"

# Drop root before runtime (matches backend.Dockerfile).
USER 1000:1000

CMD ["arq", "bvworkers.main.WorkerSettings"]
