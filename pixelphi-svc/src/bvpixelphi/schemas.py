"""Request/response models for pixelphi-svc.

The contract is intentionally minimal and matches what the backend
``HttpPixelPhiEngine`` (``services/pixel_phi_engine.py``) sends/expects: a
single grayscale frame in, a list of text bounding boxes out. The backend
masks those boxes; this service NEVER returns image bytes (it never produces a
redacted image), only coordinates, consistent with the RSNA-2025 detector→box
pattern and the rule "never trust the model to emit a clean image".
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DetectRequest(BaseModel):
    image_png_b64: str = Field(
        description="base64-encoded PNG of a single (grayscale) frame to scan",
    )


class BoxOut(BaseModel):
    x: int
    y: int
    w: int
    h: int
    # Detector confidence for the text region. -1.0 when not reported.
    conf: float = -1.0


class DetectResponse(BaseModel):
    boxes: list[BoxOut]
    model_id: str
    # True when a detector graph was actually loaded and run; False means the
    # model was absent so no boxes could be produced (the backend then fails
    # closed to over-redaction). Surfaced for observability, not trust.
    model_loaded: bool


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_id: str
