"""S3-compatible object storage helpers. The platform is
provider-agnostic (DESIGN.md §2) — anything speaking the S3 API works:
MinIO in dev, Cloudflare R2 in prod, Backblaze B2 as fallback.
"""

from bvphoenix.storage.s3 import S3Storage, default_put_extra_args, get_s3_storage

__all__ = ["S3Storage", "default_put_extra_args", "get_s3_storage"]
