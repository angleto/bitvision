"""Runtime configuration for the inference service.

All knobs are env-driven (prefix ``BVP_INFERENCE_``) so the same image
behaves identically across local, CI and Kapsule without code edits.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings, populated from the environment.

    ``model_dir`` is the directory holding the exported ONNX graphs and the
    text tokenizer. The export script (``scripts/export_onnx.py``) writes
    them there at build time; the runtime mounts/copies the same dir.
    """

    model_config = SettingsConfigDict(
        env_prefix="BVP_INFERENCE_",
        env_file=None,
        extra="ignore",
    )

    # Where the exported graphs + tokenizer live inside the image.
    model_dir: str = "/app/models"

    # File names inside ``model_dir``. Overridable but defaulted to what
    # ``export_onnx.py`` writes.
    image_onnx_name: str = "biomedclip_image.onnx"
    text_onnx_name: str = "biomedclip_text.onnx"
    tokenizer_name: str = "tokenizer.json"

    # onnxruntime intra-op thread count. 0 lets ORT pick (number of CPU
    # cores). On small ARM nodes capping this avoids oversubscription when
    # several replicas share a node.
    onnx_intra_op_threads: int = 0

    # Load both ONNX sessions at startup instead of lazily on first
    # request. Off by default so a pod that only ever serves text queries
    # never pays the image-graph load cost.
    eager_load: bool = False

    # Max number of inputs accepted per /encode call. Guards against a
    # caller sending an unbounded batch that would blow the memory limit.
    max_batch: int = 64

    # Server bind. Mirrors the convention used by the other bvphoenix
    # services (host/port overridable in the deployment).
    host: str = "0.0.0.0"
    port: int = 8090


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (one parse per process)."""
    return Settings()
