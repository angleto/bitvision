"""Runtime configuration for pixelphi-svc.

All knobs are env-driven (prefix ``BVP_PIXELPHI_``) so the same image behaves
identically across local, CI and Kapsule without code edits. Mirrors
``inference-svc``'s settings pattern.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings, populated from the environment.

    ``model_dir`` holds the exported PaddleOCR ONNX detector graph (and, later,
    the optional PHI-vs-clinical text classifier). The graph is produced
    out-of-band by ``scripts/export_onnx.py`` and uploaded to the model bucket;
    at deploy a model-sync init container populates this dir from
    ``s3://bvphoenix-models-prod/pixelphi/v1/``. Weights are NEVER baked into the
    image (the Dockerfile leaves this dir empty).
    """

    model_config = SettingsConfigDict(
        env_prefix="BVP_PIXELPHI_",
        env_file=None,
        extra="ignore",
    )

    # Where the exported detector graph lives inside the image / mounted volume.
    model_dir: str = "/app/models"
    detector_model: str = "ppocrv5_det.onnx"

    # ONNX Runtime CPU threading. Kept small: this is a gated, batch-style tier
    # (~1-2% of frames), latency is not real-time critical.
    onnx_intra_op_threads: int = 2
    onnx_inter_op_threads: int = 1

    # DBNet post-processing thresholds (PaddleOCR PP-OCRv5 detector defaults).
    det_db_thresh: float = 0.3
    det_db_box_thresh: float = 0.5
    det_db_unclip_ratio: float = 1.6

    # Guardrails. A frame larger than this on its longest side is downscaled
    # before detection (boxes are rescaled back); bounds CPU/RAM per request.
    max_image_side: int = 4096

    # Warm the ONNX session at startup instead of on first request.
    eager_load: bool = False

    # Bind address. 8091 to avoid colliding with inference-svc (8090) when both
    # run locally; host/port overridable in the deployment.
    host: str = "0.0.0.0"
    port: int = 8091


@lru_cache
def get_settings() -> Settings:
    return Settings()
