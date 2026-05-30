"""Request / response models for the /encode endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EncodeRequest(BaseModel):
    """Encode a batch of inputs of a single modality.

    For ``modality='text'`` each item in ``inputs`` is a plain string.
    For ``modality='image'`` each item is a base64-encoded PNG byte
    string (the service decodes it to a PIL image before preprocessing).
    """

    modality: Literal["image", "text"]
    inputs: list[str] = Field(..., min_length=1)


class EncodeResponse(BaseModel):
    """L2-normalised vectors plus the model identity that produced them."""

    vectors: list[list[float]]
    model_id: str
    dim: int


class HealthResponse(BaseModel):
    status: str
    model_dir: str
    loaded: dict[str, bool]
