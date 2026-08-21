"""Generate a BiomedCLIP embedding for a DICOM series.

Runs as an arq background job. Downloads the middle slice of the series,
decodes pixel data, converts to a PIL Image, runs BiomedCLIP inference,
and stores the resulting 512-dim vector in the ``embeddings`` table.

BiomedCLIP (microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224) is
a contrastive vision-language model trained on PubMed image-text pairs.
The visual encoder produces 512-dim embeddings ideal for medical image
similarity search.

Requires the ``ai`` extra: ``uv sync --extra ai``
"""

from __future__ import annotations

import io
import uuid

import numpy as np
import pydicom
from botocore.client import Config
from bvphoenix.db.engine import make_async_engine
from bvphoenix.services.embeddable import (
    SeriesNotEmbeddable,
    is_embeddable_modality,
    is_embeddable_sop_class,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bvworkers.config import get_settings

MODEL_ID = "biomedclip-v1"
EMBEDDING_DIM = 512
MODEL_HUB_ID = "hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224"

# Lazy-loaded globals — set on first call so the model loads once per worker.
_model = None
_preprocess = None
_tokenizer = None


def _ensure_model():
    """Load BiomedCLIP model on first use. Expects torch + open_clip.

    BiomedCLIP is a single dual-encoder model: the same ``_model`` exposes
    both ``encode_image`` (visual tower) and ``encode_text`` (text tower).
    Loading it once here is enough for both the series/image path and the
    cross-modal text path used by ``embed_text``.
    """
    global _model, _preprocess, _tokenizer
    if _model is not None:
        return

    import open_clip

    model, _preprocess_train, preprocess_val = open_clip.create_model_and_transforms(MODEL_HUB_ID)
    _tokenizer = open_clip.get_tokenizer(MODEL_HUB_ID)
    model.eval()
    _model = model
    _preprocess = preprocess_val


def _text_encoder():
    """Return the loaded BiomedCLIP model + text tokenizer.

    Shared helper for cross-modal workers (see ``embed_text``) so the 512-d
    text vectors live in the exact same space as the image vectors produced
    by ``_compute_embedding``. Triggers lazy model load on first call.
    """
    _ensure_model()
    return _model, _tokenizer


def _dicom_to_pil(dcm_bytes: bytes):
    """Convert DICOM bytes to a PIL RGB Image suitable for BiomedCLIP."""
    from PIL import Image

    ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
    if "PixelData" not in ds:
        raise SeriesNotEmbeddable("no_pixel_data")

    arr = np.asarray(ds.pixel_array)

    # Reduce to a displayable 2D (H,W) or RGB (H,W,3) frame. Multi-frame
    # series — 4D (frames,H,W,C) or 3D (frames,H,W) grayscale — collapse to a
    # representative middle frame; stray singleton axes are squeezed. Anything
    # that still isn't a 2D / HxWx3 image (e.g. a degenerate (1,1,N) buffer
    # that is not a displayable image — a SEG label map is the canonical case)
    # is a terminal skip, NOT a failure: raising SeriesNotEmbeddable lets the
    # worker return a ``skipped`` status instead of crashing on PIL's cryptic
    # "Cannot handle this data type" TypeError and triggering arq retries.
    if arr.ndim == 4 or (arr.ndim == 3 and arr.shape[-1] not in (3, 4)):
        arr = arr[arr.shape[0] // 2]
    arr = np.squeeze(arr)
    if not (arr.ndim == 2 or (arr.ndim == 3 and arr.shape[-1] in (3, 4))):
        raise SeriesNotEmbeddable("unsupported_pixel_layout")

    arr = arr.astype(np.float32)
    slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
    intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
    if slope != 1.0 or intercept != 0.0:
        arr = arr * slope + intercept

    # Normalize to 0-255 for PIL
    vmin, vmax = arr.min(), arr.max()
    arr = (arr - vmin) / (vmax - vmin) * 255.0 if vmax > vmin else np.zeros_like(arr)
    arr = arr.astype(np.uint8)

    img = Image.fromarray(arr)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _compute_embedding(pil_image) -> list[float]:
    """Run BiomedCLIP visual encoder on a PIL Image, return 512-dim vector."""
    import torch

    _ensure_model()
    image_tensor = _preprocess(pil_image).unsqueeze(0)
    with torch.no_grad():
        image_features = _model.encode_image(image_tensor)
        # L2-normalize for cosine similarity
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
    return image_features.squeeze(0).tolist()


def _embed_slice_count() -> int:
    """How many slices to sample per series (``BVP_EMBED_SLICES``, default 1).

    1 reproduces the original middle-slice behaviour exactly. N>1 samples
    N evenly-spaced slices and mean-pools them — a finding (a lung nodule,
    a small lesion) is rarely on the central slice, so a single middle
    slice systematically under-represents volumetric series.
    """
    import os

    try:
        return max(1, int(os.environ.get("BVP_EMBED_SLICES", "1")))
    except ValueError:
        return 1


def _select_slice_indices(total: int, n: int) -> list[int]:
    """Indices of the slices to embed: the middle one for ``n<=1``, else
    ``n`` evenly-spaced interior slices (deduped, never first/last-biased)."""
    if total <= 0:
        return []
    if n <= 1 or total == 1:
        return [total // 2]
    n = min(n, total)
    step = total / (n + 1)
    return sorted({min(total - 1, max(0, round(step * (i + 1)))) for i in range(n)})


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """Mean of L2-normalised slice vectors, renormalised to unit length so
    the pooled series vector lives in the same cosine space."""
    if not vectors:
        raise ValueError("no vectors to pool")
    if len(vectors) == 1:
        return vectors[0]
    arr = np.asarray(vectors, dtype=np.float32)
    mean = arr.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm > 0:
        mean = mean / norm
    return mean.tolist()


def _classify_error(exc: BaseException) -> str:
    """Best-effort short tag for the dashboard "why did this fail" column.

    The tag is free-form; we look for known substrings so the UI can
    bucket failures without the admin having to read every message.
    Keep the vocabulary small — when in doubt fall back to the concrete
    exception class name.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if "no pixel data" in msg or name == "NoPixelDataError":
        return "NoPixelDataError"
    if "cuda" in msg and ("out of memory" in msg or "oom" in msg):
        return "CudaOOM"
    if "out of memory" in msg:
        return "OutOfMemory"
    if any(s in msg for s in ("timeout", "timed out")):
        return "TimeoutError"
    if any(s in msg for s in ("connection", "dns", "resolve", "unreachable", "ssl")):
        return "NetworkError"
    if "s3" in msg or "nosuchkey" in msg or "bucket" in msg:
        return "S3Error"
    return name


async def _record_embedding_error(
    engine, *, target_kind: str, target_id: uuid.UUID, model_id: str, exc: BaseException
) -> None:
    """Persist a row in ``embedding_errors`` + bump retry_count.

    Uses a separate short-lived session so the original task's session
    (which may already be in a failed-transaction state) does not get in
    the way. Silently swallows its own DB errors — if we can't write the
    error row we still want the original exception to propagate to arq.
    """
    try:
        async with AsyncSession(engine) as err_db:
            existing = await err_db.execute(
                text(
                    "SELECT retry_count FROM embedding_errors "
                    "WHERE target_kind = :kind AND target_id = :tid AND model_id = :model "
                    "ORDER BY failed_at DESC LIMIT 1"
                ),
                {"kind": target_kind, "tid": target_id, "model": model_id},
            )
            row = existing.first()
            next_retry = (int(row[0]) + 1) if row else 0
            await err_db.execute(
                text(
                    "INSERT INTO embedding_errors "
                    "(target_kind, target_id, model_id, error_message, error_class, retry_count) "
                    "VALUES (:kind, :tid, :model, :msg, :cls, :rc)"
                ),
                {
                    "kind": target_kind,
                    "tid": target_id,
                    "model": model_id,
                    # Truncate to keep pathological tracebacks from
                    # bloating the table; the full traceback still
                    # shows up in worker stdout.
                    "msg": str(exc)[:4000] or type(exc).__name__,
                    "cls": _classify_error(exc),
                    "rc": next_retry,
                },
            )
            await err_db.commit()
    except Exception:
        # Deliberately swallowed — see docstring.
        pass


async def embed_series(ctx: dict, series_id: str) -> dict:  # type: ignore[type-arg]
    """Arq task: generate BiomedCLIP embedding for a series.

    Takes the middle slice, runs visual encoder, stores in embeddings table.

    On any exception we log a row to ``embedding_errors`` (so the admin
    dashboard can report it) and re-raise so arq applies its normal
    retry / failure accounting.
    """
    import asyncio

    import boto3

    settings = get_settings()
    engine = make_async_engine(settings.database_url, pool_pre_ping=True)

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    sid = uuid.UUID(series_id)

    try:
        async with AsyncSession(engine) as db:
            # Check if embedding already exists
            existing = await db.execute(
                text(
                    "SELECT id FROM embeddings "
                    "WHERE target_kind = 'series' AND target_id = :sid AND model_id = :model"
                ),
                {"sid": sid, "model": MODEL_ID},
            )
            if existing.first():
                return {"status": "already_embedded", "series_id": series_id}

            # Whole-series modality gate. A non-image series (SR, PR, SEG,
            # ...) is a TERMINAL SKIP, not a failure — returning a status
            # (below) keeps arq from retrying it and keeps embedding_errors
            # clean. This is the worker-side backstop for series that slipped
            # past the enqueue-time modality filter (null / mislabeled
            # modality). See bvphoenix.services.embeddable.
            modality = (
                await db.execute(
                    text("SELECT modality FROM series WHERE id = :sid"),
                    {"sid": sid},
                )
            ).scalar_one_or_none()
            if not is_embeddable_modality(modality):
                raise SeriesNotEmbeddable(f"non_image_modality:{modality}")

            # Get instances ordered by instance number
            result = await db.execute(
                text(
                    "SELECT s3_bucket, s3_key, sop_class_uid FROM instances "
                    "WHERE series_id = :sid ORDER BY instance_number ASC NULLS LAST"
                ),
                {"sid": sid},
            )
            instances = result.all()
            if not instances:
                return {"status": "no_instances", "series_id": series_id}

            # Keep only instances that can carry a diagnostic image. An
            # embeddable-modality series can still co-store non-image SOP
            # objects (an SR or presentation state); picking the middle of
            # ALL instances could land on one of those, so filter to image
            # instances before the slice picker sees them. No image instance
            # at all is a terminal skip.
            image_instances = [
                (bucket, key) for (bucket, key, scu) in instances if is_embeddable_sop_class(scu)
            ]
            if not image_instances:
                raise SeriesNotEmbeddable("no_image_instances")

            # Pick the slice(s) to embed: middle slice by default, or N
            # evenly-spaced slices mean-pooled when BVP_EMBED_SLICES > 1.
            picks = [
                image_instances[i]
                for i in _select_slice_indices(len(image_instances), _embed_slice_count())
            ]

            # Download DICOM(s), decode, compute + mean-pool embedding (CPU-bound)
            def _do_embed():
                vecs = []
                for bucket, key in picks:
                    resp = s3.get_object(Bucket=bucket, Key=key)
                    dcm_bytes = resp["Body"].read()
                    vecs.append(_compute_embedding(_dicom_to_pil(dcm_bytes)))
                return _mean_pool(vecs)

            vector = await asyncio.to_thread(_do_embed)

            # Store embedding
            # pgvector expects array literal format
            vec_str = "[" + ",".join(str(v) for v in vector) + "]"
            await db.execute(
                text(
                    "INSERT INTO embeddings (target_kind, target_id, model_id, vector) "
                    "VALUES ('series', :sid, :model, :vec) "
                    "ON CONFLICT (target_kind, target_id, model_id) DO UPDATE SET "
                    "vector = EXCLUDED.vector, created_at = NOW()"
                ),
                {"sid": sid, "model": MODEL_ID, "vec": vec_str},
            )
            await db.commit()

        return {
            "status": "embedded",
            "series_id": series_id,
            "model_id": MODEL_ID,
            "dim": len(vector),
        }
    except SeriesNotEmbeddable as exc:
        # Terminal skip — NOT a failure. No embedding_errors row and no
        # re-raise, so arq marks the job done and never retries. Covers
        # non-image series (SR / PR / SEG), no-pixel objects, and odd pixel
        # layouts that slipped past the enqueue-time modality filter.
        return {"status": "skipped", "reason": exc.reason, "series_id": series_id}
    except Exception as exc:
        # Log for the dashboard, then re-raise so arq sees the failure
        # and applies its retry policy.
        await _record_embedding_error(
            engine,
            target_kind="series",
            target_id=sid,
            model_id=MODEL_ID,
            exc=exc,
        )
        raise
    finally:
        await engine.dispose()
