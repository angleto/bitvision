"""Tiny shared HTTP helpers used by router modules.

Kept minimal on purpose: only utilities that are needed by more than
one ``api/*`` module live here. If a helper is single-use it stays in
the calling module.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from starlette.background import BackgroundTask


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """Build an HTTP-safe ``Content-Disposition`` value.

    Starlette encodes response headers as latin-1 (the HTTP/1.1
    default). Document titles routinely carry characters outside
    latin-1 — em-dash (U+2014), accented letters, smart quotes — so a
    naive ``filename="<title>"`` raises ``UnicodeEncodeError`` and
    surfaces as a 500 to the browser.

    RFC 6266 §4.3 says: include both an ASCII fallback (``filename=``)
    and a percent-encoded UTF-8 form (``filename*=UTF-8''<encoded>``);
    modern browsers prefer the starred form. We sanitise the ASCII
    fallback by replacing every non-ASCII char with ``_`` so older
    clients still get a readable name.
    """
    name = (filename or "document").strip().replace('"', "_")
    ascii_safe = name.encode("ascii", errors="replace").decode("ascii").replace("?", "_")
    ascii_safe = ascii_safe.strip("_") or "document"
    encoded = urllib.parse.quote(name, safe="")
    return f"{disposition}; filename=\"{ascii_safe}\"; filename*=UTF-8''{encoded}"


async def proxy_s3_object(
    *,
    request: Request,
    bucket: str,
    key: str,
    filename: str,
    fallback_content_type: str = "application/octet-stream",
    background: BackgroundTask | None = None,
) -> StreamingResponse:
    """Proxy-stream an S3 object to the client with full HTTP hygiene.

    Single source of truth for download endpoints. Owns:

    * ``Range`` parsing (``bytes=A-B`` / ``bytes=A-`` / ``bytes=-N``,
      malformed / multi-range falls back to a 200 full body — see
      :meth:`bvphoenix.storage.s3.S3Storage.iter_object_with_range`).
    * ``Accept-Ranges: bytes`` so download managers (Chrome / Safari)
      offer Resume on a dropped connection.
    * ``Content-Disposition`` via :func:`content_disposition` (RFC
      6266 §4.3 — ASCII fallback + percent-encoded UTF-8 ``filename*``).
    * ``Cache-Control: private, max-age=0`` so multi-tenant proxies
      don't accidentally cache PHI.
    * 200 full body vs 206 Partial Content distinction.
    * Storage isolation: the bucket name and S3 endpoint never appear
      in the response or in any forwarded headers (memory
      ``feedback_storage_isolation``).

    Raises HTTPException(404) when the object cannot be fetched (any
    storage exception). Callers handle higher-level gates (auth,
    permissions, ownership) and audit logging upstream.

    ``background`` is forwarded to the StreamingResponse so callers
    can hook side effects that fire AFTER the body has streamed
    (e.g. bumping ``download_count`` only on a successful 200).
    """
    from bvphoenix.storage import get_s3_storage

    storage = get_s3_storage()
    range_header = request.headers.get("range")
    try:
        body_iter, returned_len, _total, content_type, content_range = await asyncio.to_thread(
            storage.iter_object_with_range,
            bucket=bucket,
            key=key,
            range_header=range_header,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="object unavailable") from exc

    headers: dict[str, str] = {
        "cache-control": "private, max-age=0",
        "accept-ranges": "bytes",
        "content-disposition": content_disposition(filename, disposition="attachment"),
    }
    if returned_len:
        headers["content-length"] = str(returned_len)
    status_code = 200
    if content_range:
        headers["content-range"] = content_range
        status_code = 206

    # 206 Partial Content responses are NOT a "complete download" —
    # the BackgroundTask is suppressed for them so callers' counters
    # (download_count, etc.) only reflect full-body deliveries.
    effective_background = background if status_code == 200 else None

    return StreamingResponse(
        body_iter,
        media_type=content_type or fallback_content_type,
        headers=headers,
        status_code=status_code,
        background=effective_background,
    )


__all__ = ["content_disposition", "proxy_s3_object"]
