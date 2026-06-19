"""Worker settings, pulled from env (prefix BVP_ shared with backend)."""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BVP_",
        env_file=(".env", "../.env"),
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+asyncpg://bvphoenix:bvphoenix@localhost:5432/bvphoenix"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    s3_endpoint_url: str = Field(default="http://localhost:9000")
    s3_region: str = Field(default="us-east-1")
    s3_access_key: str = Field(default="bvphoenix")
    s3_secret_key: str = Field(default="bvphoenix-dev-secret")
    s3_bucket_raw: str = Field(default="bvphoenix-raw")
    s3_bucket_derivatives: str = Field(default="bvphoenix-derivatives")
    # Versioned-artifact bucket (registration transforms, etc.). Mirrors
    # bvphoenix.config; the register_series and propagate_lesion workers
    # write the rigid ``.tfm`` here, so it must exist on the worker Settings.
    s3_bucket_versioning: str = Field(default="bvphoenix-versioning")

    # Match backend config — workers write derivatives and must honour the
    # same SSE policy. See backend ``bvphoenix.config`` for the source of
    # truth; these keys are reflected here so workers read the same env.
    s3_encryption: Literal["none", "AES256", "aws:kms"] = Field(default="AES256")
    s3_kms_key_arn: str | None = Field(default=None)

    # Review queue: comma-separated import paths of the consumer modules
    # that register review profiles — resolved by the generic
    # ``run_review_checks`` task before looking a profile up by name.
    # Mirrors backend ``bvphoenix.config.review_profile_modules``.
    review_profile_modules: str = Field(default="")

    # Patient inbound inbox — mirrors the backend settings of the same
    # name (the maintenance sweep enforces retention worker-side).
    inbound_email_raw_retention_days: int = Field(default=90, ge=1)

    # Auto-tag stage-2 toggle. Off by default so the worker runs purely
    # on the deterministic lexicon; flip BVP_AUTOTAG_USE_LLM=1 to call
    # the configured LLM provider on long-form text.
    autotag_use_llm: bool = Field(default=False)
    llm_default_model: str = Field(default="claude-sonnet-4-6")
    anthropic_api_key: str = Field(default="")

    # --- WSI tiling (tile_wsi task) ----------------------------------
    # Tiling decodes the source + emits a full DZI tile tree on local
    # disk, unlike ingest which only streams the source to S3. So the
    # tiling cap is LOWER than the ingest cap (BVP_WSI_MAX_BYTES, 30 GiB
    # backend-side): a 30 GiB source co-resident with its tile tree will
    # not fit a 60 GiB node. Above this, tiling is deferred (the slide
    # still ingests + serves thumbnail/macro).
    wsi_tile_max_bytes: int = Field(default=10 * 1024 * 1024 * 1024)
    # Slides ABOVE this are not pre-generated: a full gigapixel DZI pyramid
    # (CAMELYON-scale: ~150k tiles + per-tile S3 uploads) exceeds the worker
    # job timeout and would loop forever. The backend serves these on the
    # fly instead (services.wsi_tiles, OpenSlide random-access from a bounded
    # S3-source cache). 512 MiB: ordinary slides pre-generate (cached,
    # cheap); CAMELYON-scale slides are tiled live. The worker marks them
    # with a non-fatal dzi_error so the backfill (dzi_error IS NULL) skips them.
    wsi_pregenerate_max_bytes: int = Field(default=512 * 1024 * 1024)
    # Scratch dir for source download + dzsave output. In production this
    # points at an emptyDir with a sizeLimit so an oversize render evicts
    # only this pod instead of tainting the node with DiskPressure. None
    # falls back to the system temp dir (dev).
    wsi_scratch_dir: str | None = Field(default=None)

    def put_extra_args(self) -> dict[str, Any]:
        if self.s3_encryption == "AES256":
            return {"ServerSideEncryption": "AES256"}
        if self.s3_encryption == "aws:kms":
            extra: dict[str, Any] = {"ServerSideEncryption": "aws:kms"}
            if self.s3_kms_key_arn:
                extra["SSEKMSKeyId"] = self.s3_kms_key_arn
            return extra
        return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
