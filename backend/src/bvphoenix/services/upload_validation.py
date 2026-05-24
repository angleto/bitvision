"""Reusable helpers for validating uploaded files.

Endpoints accept files via FastAPI `UploadFile`; before persisting to S3
we check the declared `Content-Type` against an allow-list and enforce a
maximum payload size. The helpers here raise `HTTPException` with the
conventional HTTP status codes:

- ``415 Unsupported Media Type`` when the MIME type is not allowed.
- ``413 Request Entity Too Large`` when the upload exceeds the size limit.

Callers typically validate ``Content-Type`` upfront (cheap header check)
and then read the file into memory, calling ``validate_size`` on the
byte length of the resulting buffer.
"""

from __future__ import annotations

from fastapi import HTTPException, status

# Default allow-list shared by reports and patient documents: common
# clinical document formats plus rasterised scans of paper reports.
DEFAULT_ALLOWED_MIME: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "image/png",
        "image/jpeg",
    }
)

# 50 MiB ceiling — enough for a multi-page PDF referto with embedded
# diagnostic images, small enough to keep memory usage predictable since
# we buffer uploads before streaming to S3.
DEFAULT_MAX_UPLOAD_MB: int = 50


def validate_mime(
    content_type: str | None, allowed: frozenset[str] | set[str] | None = None
) -> None:
    """Reject uploads whose declared Content-Type is not in ``allowed``.

    ``content_type`` may include parameters (e.g. ``"image/jpeg; charset=binary"``);
    only the media type (before ``;``) is compared.
    """

    allow = allowed if allowed is not None else DEFAULT_ALLOWED_MIME
    if not content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="missing content type",
        )
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in allow:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported media type: {media_type}",
        )


def validate_size(size: int, max_mb: int = DEFAULT_MAX_UPLOAD_MB) -> None:
    """Reject uploads larger than ``max_mb`` megabytes.

    ``size`` is the payload length in bytes. Negative values are treated
    as unknown and accepted — callers should pass the real length once
    the body has been read into memory.
    """

    if size < 0:
        return
    max_bytes = max_mb * 1024 * 1024
    if size > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds maximum size of {max_mb} MB",
        )
