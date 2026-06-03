"""Thin wrapper around boto3 for S3 / S3-compatible backends.

Keeps provider details (endpoint URL, signature version, path-style
addressing) in one place. Callers use the ``S3Storage`` class and never
import boto3 directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

import boto3
from botocore.client import Config

from bvphoenix.config import Settings, get_settings


def default_put_extra_args(settings: Settings | None = None) -> dict[str, Any]:
    """Return the server-side encryption kwargs for every ``put_object`` /
    ``upload_fileobj`` call.

    Centralised so every caller — backend, worker, CLI — applies the
    same policy. The dict is splatted into boto3 calls; an empty dict
    disables encryption (dev against MinIO without KES, local tests).
    """
    s = settings or get_settings()
    if s.s3_encryption == "AES256":
        return {"ServerSideEncryption": "AES256"}
    if s.s3_encryption == "aws:kms":
        extra: dict[str, Any] = {"ServerSideEncryption": "aws:kms"}
        if s.s3_kms_key_arn:
            extra["SSEKMSKeyId"] = s.s3_kms_key_arn
        return extra
    return {}


@dataclass(frozen=True, slots=True)
class UploadResult:
    bucket: str
    key: str
    size_bytes: int


class S3Storage:
    """Minimal upload / presign surface over an S3-compatible endpoint.

    Designed so adding new providers (R2, B2, AWS) never requires touching
    callers: the endpoint URL and credentials flow in via ``Settings``.

    ``public_endpoint_url`` (when set) is used only when signing URLs
    handed to the browser, so the hostname resolvable from the client
    can differ from the one the backend uses internally.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        access_key: str,
        secret_key: str,
        public_endpoint_url: str = "",
        put_extra_args: dict[str, Any] | None = None,
    ) -> None:
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._public_endpoint = public_endpoint_url or endpoint_url
        self._put_extra: dict[str, Any] = dict(put_extra_args or {})
        # ``max_pool_connections`` defaults to 10 in botocore, which
        # silently serialises any prefetch loop above that count. The
        # streaming export prefetch pool runs at 32+ concurrent S3
        # GetObject calls; without the bump it caps out at 10 and the
        # rest queue up on the connection pool, defeating the point.
        # 64 is generous headroom for foreseeable parallelism without
        # exploding the FD budget.
        _client_config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            max_pool_connections=64,
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=_client_config,
        )
        self._public_client = (
            boto3.client(
                "s3",
                endpoint_url=public_endpoint_url,
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=_client_config,
            )
            if public_endpoint_url
            else self._client
        )

    def ensure_bucket(self, name: str) -> None:
        """Create the bucket if it doesn't already exist. Safe to call on
        every startup — MinIO and R2 both tolerate this idempotently.

        Some IAM policies (e.g. Scaleway's ObjectStorageBucketsRead) do
        not include ``s3:ListBucket`` for the head_bucket probe, so
        ``head_bucket`` may answer 403 even when the bucket exists. In
        that case the create_bucket fallback fires, and we have to also
        treat ``BucketAlreadyOwnedByYou`` / ``BucketAlreadyExists`` as a
        soft success — the bucket already there is exactly the
        invariant ensure_bucket promises.
        """
        try:
            self._client.head_bucket(Bucket=name)
            return
        except self._client.exceptions.ClientError:
            pass
        try:
            self._client.create_bucket(Bucket=name)
        except (
            self._client.exceptions.BucketAlreadyOwnedByYou,
            self._client.exceptions.BucketAlreadyExists,
        ):
            return

    def upload_file(self, path: Path, *, bucket: str, key: str) -> UploadResult:
        size = path.stat().st_size
        # boto3's high-level ``upload_file`` runs the bytes through the
        # S3 transfer manager: it auto-switches to a multipart upload
        # past a threshold (default 8 MiB) and never holds the whole
        # body in RAM, so a multi-GiB ISO streams without OOM-killing
        # the caller. Single-PUT ``put_object`` would also fail outright
        # past the 5 GiB single-PUT cap; multipart sidesteps that too.
        self._client.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=key,
            ExtraArgs=dict(self._put_extra),
        )
        return UploadResult(bucket=bucket, key=key, size_bytes=size)

    def upload_bytes(self, data: bytes | BinaryIO, *, bucket: str, key: str) -> UploadResult:
        body = data.read() if hasattr(data, "read") else data
        assert isinstance(body, bytes)
        self._client.put_object(Bucket=bucket, Key=key, Body=body, **self._put_extra)
        return UploadResult(bucket=bucket, key=key, size_bytes=len(body))

    def upload_iter(
        self,
        chunks: Iterator[bytes],
        *,
        bucket: str,
        key: str,
        part_size: int = 8 * 1024 * 1024,
        content_type: str | None = None,
    ) -> UploadResult:
        """Streaming multipart upload — accept an iterator of bytes and
        relay it to S3 as a multipart object without buffering the
        whole payload in RAM.

        S3's multipart minimum is 5 MiB per part (the last part is
        exempt); we default to 8 MiB so a small ZIP still fits in 1-2
        parts and bigger ones don't make 1000-part requests.

        Used by the patient / folder / bulk export pipeline to relay a
        stream-zip iterator directly to S3 — for a multi-GB DICOM
        export the worker's resident memory stays around 16 MiB
        (one part being assembled, one being uploaded).

        Failures abort the multipart upload so we don't leak orphan
        parts that would later be billed by the storage provider.
        """
        if part_size < 5 * 1024 * 1024:
            raise ValueError("S3 multipart parts must be ≥ 5 MiB")

        create_kwargs: dict[str, Any] = dict(self._put_extra)
        if content_type:
            create_kwargs["ContentType"] = content_type
        mp = self._client.create_multipart_upload(
            Bucket=bucket,
            Key=key,
            **create_kwargs,
        )
        upload_id = mp["UploadId"]
        parts: list[dict[str, Any]] = []
        total_bytes = 0
        buf = bytearray()
        part_number = 1

        def _flush() -> None:
            nonlocal part_number
            # Skip empty buffers — they'd produce a zero-byte part
            # which is valid but wasteful; the only case we accept is
            # the final flush of an empty body, handled by the caller.
            if not buf:
                return
            resp = self._client.upload_part(
                Bucket=bucket,
                Key=key,
                PartNumber=part_number,
                UploadId=upload_id,
                Body=bytes(buf),
            )
            parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
            part_number += 1
            buf.clear()

        try:
            for chunk in chunks:
                if not chunk:
                    continue
                buf.extend(chunk)
                total_bytes += len(chunk)
                # Drain whole parts as long as we have ≥ part_size
                # buffered. The condition fires multiple times when a
                # huge chunk lands.
                while len(buf) >= part_size:
                    overflow = bytes(buf[part_size:])
                    del buf[part_size:]
                    _flush()
                    if overflow:
                        buf.extend(overflow)
            # Last part: whatever is left, even if < 5 MiB.
            _flush()
            if not parts:
                # Empty body: multipart with zero parts is illegal, so
                # complete with a single empty part.
                resp = self._client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    PartNumber=1,
                    UploadId=upload_id,
                    Body=b"",
                )
                parts.append({"PartNumber": 1, "ETag": resp["ETag"]})
            self._client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            # Clean up so the storage provider doesn't keep billing
            # orphan parts indefinitely.
            try:
                self._client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            except Exception:
                pass
            raise
        return UploadResult(bucket=bucket, key=key, size_bytes=total_bytes)

    # --- Discrete multipart primitives -----------------------------------
    # Drive ONE S3 multipart upload across SEPARATE HTTP requests — the
    # resumable chunked-upload session: each client PATCH appends one part.
    # The caller persists (upload_id, parts, received_offset) per file so a
    # dropped connection resumes from the last acked part. ``upload_iter``
    # above is the one-shot in-process pipeline; these expose the same boto3
    # calls individually. Encryption policy (``self._put_extra``) is applied
    # on create so every part inherits it. All bytes still flow THROUGH the
    # backend — no presigned PUT, storage isolation preserved.

    def create_multipart(self, *, bucket: str, key: str, content_type: str | None = None) -> str:
        """Begin a multipart upload; return its ``UploadId``."""
        create_kwargs: dict[str, Any] = dict(self._put_extra)
        if content_type:
            create_kwargs["ContentType"] = content_type
        mp = self._client.create_multipart_upload(Bucket=bucket, Key=key, **create_kwargs)
        return mp["UploadId"]

    def upload_part(
        self, *, bucket: str, key: str, upload_id: str, part_number: int, body: bytes
    ) -> str:
        """Upload one part; return its ``ETag``.

        ``part_number`` is 1-based and MUST be derived deterministically from
        the byte offset by the caller (``offset // part_size + 1``) so a
        re-sent chunk at an already-acked offset maps to the same part and is
        idempotent. Every part except the last must be ≥ 5 MiB (S3 rule); the
        caller enforces a fixed chunk size and exempts the final part.
        """
        resp = self._client.upload_part(
            Bucket=bucket,
            Key=key,
            PartNumber=part_number,
            UploadId=upload_id,
            Body=body,
        )
        return resp["ETag"]

    def complete_multipart(
        self, *, bucket: str, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        """Finalize the object from its uploaded parts.

        ``parts`` is ``[{"PartNumber": int, "ETag": str}, ...]``; we sort by
        PartNumber (S3 requires ascending order) so the caller can persist
        them in receipt order.
        """
        parts_sorted = sorted(parts, key=lambda p: int(p["PartNumber"]))
        self._client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts_sorted},
        )

    def abort_multipart(self, *, bucket: str, key: str, upload_id: str) -> None:
        """Abort an in-flight multipart upload, dropping its parts immediately.

        The GC sweeper calls this on abandoned sessions so incomplete parts
        are released within minutes instead of waiting up to a day for the
        ``AbortIncompleteMultipartUpload`` lifecycle rule.
        """
        self._client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)

    def put_extra_args(self) -> dict[str, Any]:
        """Expose the encryption kwargs so callers that reach boto3 directly
        (e.g. workers) can keep the same policy without depending on config.
        """
        return dict(self._put_extra)

    def presigned_get_url(
        self,
        *,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        response_content_disposition: str | None = None,
        response_content_type: str | None = None,
    ) -> str:
        """Generate a presigned GET URL.

        Optional ``response_content_disposition`` (e.g. ``inline; filename=...``)
        and ``response_content_type`` overrides let the caller steer how the
        browser renders the payload — useful for inline previews of PDFs and
        images served from a derivatives bucket.
        """
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if response_content_disposition is not None:
            params["ResponseContentDisposition"] = response_content_disposition
        if response_content_type is not None:
            params["ResponseContentType"] = response_content_type
        return self._public_client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        resp = self._client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    def iter_object(
        self,
        *,
        bucket: str,
        key: str,
        chunk_size: int = 64 * 1024,
    ) -> tuple[Iterator[bytes], int | None, str | None]:
        """Stream an object from storage in chunks.

        Returns ``(iterator, content_length, content_type)``. Caller wraps
        the iterator in ``StreamingResponse``. Content stays inside the
        backend pod end-to-end — no presigned URL, no client-side S3
        round-trip — which is the precondition for the storage-isolation
        contract (``feedback_storage_isolation``): the bucket name must
        never appear in any response or Location header that crosses the
        backend boundary.
        """
        resp = self._client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]
        length: int | None = None
        try:
            length = (
                int(resp.get("ContentLength")) if resp.get("ContentLength") is not None else None
            )
        except (TypeError, ValueError):
            length = None
        ctype = resp.get("ContentType") or None

        def _iter() -> Iterator[bytes]:
            try:
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

        return _iter(), length, ctype

    def list_objects(self, *, bucket: str, prefix: str) -> list[tuple[str, int]]:
        """Return ``(key, size_bytes)`` for every object under ``prefix``.
        Paginates internally so callers don't have to care about the
        1000-key page limit. Size comes straight from the list response —
        avoid per-object HEADs."""
        out: list[tuple[str, int]] = []
        token: str | None = None
        while True:
            kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                out.append((obj["Key"], int(obj.get("Size", 0))))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return out

    def get_object_range(self, *, bucket: str, key: str, start: int, length: int) -> bytes:
        """Ranged GET — fetch only ``length`` bytes starting at ``start``.
        Useful for reading just the header of a large cached volume."""
        end = start + length - 1
        resp = self._client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")
        return resp["Body"].read()

    def iter_object_with_range(
        self,
        *,
        bucket: str,
        key: str,
        range_header: str | None,
        chunk_size: int = 64 * 1024,
    ) -> tuple[Iterator[bytes], int, int, str | None, str | None]:
        """Stream an object, honouring an optional HTTP ``Range`` header.

        Returns ``(iterator, returned_length, total_length, content_type,
        content_range_header)``. ``content_range_header`` is set only when
        the caller asked for a range — the API endpoint emits it on
        ``206 Partial Content`` responses.

        Range parsing accepts ``bytes=A-B``, ``bytes=A-`` (open suffix),
        and ``bytes=-N`` (last-N bytes). Malformed ranges fall back to a
        full-body response so a buggy proxy can't 416 the user — the
        browser will retry without the header on its own. Multi-range
        (``bytes=0-99,200-299``) is rejected because it requires
        multipart/byteranges encoding which we don't emit.
        """
        kwargs: dict[str, object] = {"Bucket": bucket, "Key": key}
        ranged = False
        if range_header:
            spec = range_header.strip().lower()
            if spec.startswith("bytes=") and "," not in spec:
                kwargs["Range"] = range_header
                ranged = True
        resp = self._client.get_object(**kwargs)
        body = resp["Body"]
        ctype = resp.get("ContentType") or None
        returned_length: int = 0
        try:
            returned_length = (
                int(resp.get("ContentLength")) if resp.get("ContentLength") is not None else 0
            )
        except (TypeError, ValueError):
            returned_length = 0
        # ``ContentRange`` is the authoritative source for total length on
        # a 206; in the 200 case we fall back to the full ContentLength.
        total_length = returned_length
        content_range = resp.get("ContentRange")
        if ranged and content_range:
            # boto3 returns lowercase keys; the spec form is "bytes A-B/T".
            try:
                total_length = int(str(content_range).rsplit("/", 1)[1])
            except (IndexError, ValueError):
                total_length = returned_length

        def _iter() -> Iterator[bytes]:
            try:
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

        return (
            _iter(),
            returned_length,
            total_length,
            ctype,
            (str(content_range) if ranged and content_range else None),
        )

    def delete_object(self, *, bucket: str, key: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=key)

    def object_exists(self, *, bucket: str, key: str) -> bool:
        """True if ``key`` exists in ``bucket``. Cheap (HEAD-only).

        Used by ops scripts that need to check the presence of a
        specific object without paginating ``list_objects``. Returns
        False on any client error (404, NoSuchKey, NoSuchBucket) —
        callers that need to distinguish should call ``head_object``
        on ``self._client`` directly.
        """
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def copy_object(
        self,
        *,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> None:
        """S3 server-side copy — no bytes transit through this process.

        Used by the bulk-upload worker to move ISO archives from the
        ``_ingest_jobs/`` staging prefix into a stable
        ``patients/<id>/iso/`` prefix once the rest of the ingest is
        confirmed. boto3's ``copy()`` handles single-PUT under 5 GiB
        and switches to multipart-copy automatically above that, so a
        ~1.3 GB CD image is one cheap header-only request.
        """
        self._client.copy(
            CopySource={"Bucket": src_bucket, "Key": src_key},
            Bucket=dst_bucket,
            Key=dst_key,
            ExtraArgs=self._put_extra or None,
        )


@lru_cache
def get_s3_storage() -> S3Storage:
    s = get_settings()
    return S3Storage(
        endpoint_url=s.s3_endpoint_url,
        region=s.s3_region,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        public_endpoint_url=s.s3_public_endpoint_url,
        put_extra_args=default_put_extra_args(s),
    )
