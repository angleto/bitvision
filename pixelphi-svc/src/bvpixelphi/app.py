"""FastAPI app for the CPU ONNX burned-in-pixel PHI detector microservice.

Exposes:
- ``POST /detect``, take one base64 PNG frame, return text bounding boxes for
  the backend to mask.
- ``GET /healthz``, liveness/readiness probe.

Storage isolation: this app only ever sees a single decoded frame and returns
coordinates. It never touches S3 or the patient database and persists nothing;
the frame is held in memory for the duration of the request only. The backend
only ever POSTs here over an in-cluster, allowlisted host (see
``services.pixel_phi_engine.HttpPixelPhiEngine``).
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from PIL import Image, UnidentifiedImageError

from bvpixelphi.config import get_settings
from bvpixelphi.engine import MODEL_ID, DetectorEngine
from bvpixelphi.schemas import BoxOut, DetectRequest, DetectResponse, HealthResponse

logger = logging.getLogger("bvpixelphi")

# Module-level engine; populated in the lifespan handler so a single
# DetectorEngine (and therefore a single ONNX session) is shared by every
# request handled by this worker process.
_engine: DetectorEngine | None = None


def _get_engine() -> DetectorEngine:
    if _engine is None:  # pragma: no cover - guarded by lifespan
        raise HTTPException(status_code=503, detail="engine not initialised")
    return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the engine once per process; optionally warm the ONNX session."""
    global _engine
    settings = get_settings()
    _engine = DetectorEngine(settings)
    if settings.eager_load:
        try:
            _engine.warmup()
        except Exception:
            # Log and keep serving /healthz; lazy load will retry.
            logger.exception("eager ONNX warmup failed; will retry lazily")
    yield
    _engine = None


app = FastAPI(title="bvphoenix pixelphi-svc", lifespan=lifespan)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    engine = _get_engine()
    return HealthResponse(
        status="ok",
        model_loaded=engine.model_loaded,
        model_id=MODEL_ID,
    )


@app.post("/detect", response_model=DetectResponse)
async def detect(req: DetectRequest) -> DetectResponse:
    engine = _get_engine()
    try:
        raw = base64.b64decode(req.image_png_b64, validate=True)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (binascii.Error, ValueError, UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid image: {exc}") from exc

    boxes = engine.detect(img)
    return DetectResponse(
        boxes=[BoxOut(x=x, y=y, w=w, h=h, conf=c) for (x, y, w, h, c) in boxes],
        model_id=MODEL_ID,
        model_loaded=engine.model_loaded,
    )


def main() -> None:
    """Console entry point: run uvicorn with the configured host/port."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "bvpixelphi.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
