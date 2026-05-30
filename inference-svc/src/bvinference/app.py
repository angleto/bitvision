"""FastAPI app for the CPU ONNX BiomedCLIP inference microservice.

Exposes:
- ``POST /encode`` — batch-encode images (base64 PNG) or text strings into
  L2-normalised 512-dim BiomedCLIP vectors.
- ``GET /healthz`` — liveness/readiness probe.

Storage isolation: this app only ever sees decoded pixel arrays / text
strings and returns vectors. It never touches S3 or the patient database
and persists nothing.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from PIL import Image, UnidentifiedImageError

from bvinference.config import get_settings
from bvinference.engine import (
    EMBEDDING_DIM,
    IMAGE_MODEL_ID,
    TEXT_MODEL_ID,
    InferenceEngine,
)
from bvinference.schemas import EncodeRequest, EncodeResponse, HealthResponse

logger = logging.getLogger("bvinference")

# Module-level engine; populated in the lifespan handler so a single
# InferenceEngine (and therefore a single set of ONNX sessions) is shared
# by every request handled by this worker process.
_engine: InferenceEngine | None = None


def _get_engine() -> InferenceEngine:
    if _engine is None:  # pragma: no cover - guarded by lifespan
        raise HTTPException(status_code=503, detail="engine not initialised")
    return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the engine once per process; optionally warm the sessions."""
    global _engine
    settings = get_settings()
    _engine = InferenceEngine(settings)
    if settings.eager_load:
        try:
            _engine.warmup()
        except Exception:
            # Log and keep serving /healthz; lazy load will retry.
            logger.exception("eager ONNX warmup failed; will retry lazily")
    yield
    _engine = None


app = FastAPI(
    title="bvinference",
    description="CPU ONNX BiomedCLIP inference microservice",
    version="0.0.1",
    lifespan=lifespan,
)


def _decode_png(b64: str) -> Image.Image:
    """Decode a base64 PNG string into a PIL image, or 422 on bad input."""
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid base64") from exc
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=422, detail="invalid image bytes") from exc
    return img


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness/readiness: app is up and reports which sessions are loaded.

    Returns 200 as soon as the process can serve; the ONNX sessions may
    still be lazy (``loaded`` reflects that). This intentionally does not
    force a model load so a probe never triggers a ~500 MB allocation.
    """
    settings = get_settings()
    loaded = _engine.loaded if _engine is not None else {"image": False, "text": False}
    return HealthResponse(status="ok", model_dir=settings.model_dir, loaded=loaded)


@app.post("/encode", response_model=EncodeResponse)
async def encode(req: EncodeRequest) -> EncodeResponse:
    """Encode a batch of inputs into L2-normalised 512-dim vectors."""
    settings = get_settings()
    if len(req.inputs) > settings.max_batch:
        raise HTTPException(
            status_code=413,
            detail=f"batch too large: {len(req.inputs)} > {settings.max_batch}",
        )

    engine = _get_engine()

    try:
        if req.modality == "text":
            vectors = engine.encode_text(req.inputs)
            model_id = TEXT_MODEL_ID
        else:
            images = [_decode_png(item) for item in req.inputs]
            vectors = engine.encode_image(images)
            model_id = IMAGE_MODEL_ID
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        # The ONNX graph / tokenizer is missing from the image. This is a
        # deploy-time error, not a client error.
        logger.exception("model artefact missing")
        raise HTTPException(status_code=503, detail="model artefact missing") from exc
    except Exception as exc:
        # Surface any other failure as a 500 with a short detail.
        logger.exception("inference failed")
        raise HTTPException(status_code=500, detail="inference failed") from exc

    return EncodeResponse(
        vectors=[[float(x) for x in row] for row in vectors],
        model_id=model_id,
        dim=EMBEDDING_DIM,
    )


def main() -> None:
    """Console entry point: run uvicorn with the configured host/port."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "bvinference.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
