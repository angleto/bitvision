"""The S3 client must retry transient connection errors.

A Scaleway S3 connection blip (botocore EndpointConnectionError, "Could
not connect to the endpoint URL") once failed a single instance PUT and
took down a whole subject in a bulk public-dataset import. boto3's
default "legacy" retry mode is weak; we pin "standard" mode so every S3
call retries connection/throttling/5xx with exponential backoff. This
guards the Config (no network: boto3 client construction is lazy).
"""

from __future__ import annotations

from bvphoenix.storage.s3 import S3Storage


def _storage() -> S3Storage:
    return S3Storage(
        endpoint_url="http://localhost:9000",
        region="fr-par",
        access_key="x",
        secret_key="y",
    )


def test_s3_client_uses_standard_retry_mode() -> None:
    cfg = _storage()._client.meta.config
    assert cfg.retries["mode"] == "standard"
    # max_attempts=5 normalises to total_max_attempts=6 (initial + 5 retries).
    assert cfg.retries["total_max_attempts"] >= 5
    # Regression guard for the connection-pool bump (streaming export prefetch).
    assert cfg.max_pool_connections == 64
