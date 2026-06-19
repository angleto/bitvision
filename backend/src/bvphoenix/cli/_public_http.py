"""Shared HTTP download helpers for the public-dataset import CLIs.

Both ``cli.public_import`` (radiology DICOM, TCIA/OsiriX) and
``cli.public_import_pathology`` (WSI, OpenSlide/CAMELYON/TCIA) need the
same bounded-retry streaming download against flaky public archives.
The retry contract is deliberately conservative: back off three times
on transient transport errors, fail fast (no retry) on a 4xx so a wrong
source URL aborts that one subject instead of looping.

Read timeout is intentionally large (default 600s). The TCIA getImage
endpoint does not stream chunked — the server assembles the whole
per-series ZIP before sending the first byte, which on a thin-slice CT
can sit silent for 1-3 minutes. A multi-GB WSI download over a slow
mirror has the same shape. Env-overridable so prod can tune without a
rebuild cycle.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import click
import httpx

__all__ = [
    "HTTP_CONNECT_TIMEOUT_SEC",
    "HTTP_READ_TIMEOUT_SEC",
    "HTTP_RETRIES",
    "HTTP_TIMEOUT",
    "RETRY_BACKOFF_SEC",
    "_http_get_json_with_retry",
    "_http_get_with_retry",
]

HTTP_CONNECT_TIMEOUT_SEC = float(os.environ.get("BVP_PUBLIC_IMPORT_HTTP_CONNECT_TIMEOUT", "30"))
HTTP_READ_TIMEOUT_SEC = float(os.environ.get("BVP_PUBLIC_IMPORT_HTTP_READ_TIMEOUT", "600"))
HTTP_TIMEOUT = httpx.Timeout(HTTP_READ_TIMEOUT_SEC, connect=HTTP_CONNECT_TIMEOUT_SEC)
HTTP_RETRIES = 3
RETRY_BACKOFF_SEC = 5.0


def _http_get_with_retry(client: httpx.Client, url: str, out_path: Path, *, what: str) -> None:
    """Stream a URL to disk with bounded retries.

    Bandwidth at the public archives is fine but they occasionally
    return 503 / drop the connection; the retry is what lets a
    100-subject manifest finish unattended. We do NOT retry on 4xx (the
    source URL is wrong, fail fast for that subject).
    """
    last_exc: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with client.stream("GET", url) as resp:
                if 400 <= resp.status_code < 500:
                    raise click.ClickException(
                        f"{what}: HTTP {resp.status_code} (client error, no retry) {url}"
                    )
                resp.raise_for_status()
                with out_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        fh.write(chunk)
            return
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < HTTP_RETRIES:
                click.echo(
                    f"  retry {attempt}/{HTTP_RETRIES} after {RETRY_BACKOFF_SEC}s: {exc}",
                    err=True,
                )
                time.sleep(RETRY_BACKOFF_SEC * attempt)
    raise click.ClickException(f"{what}: gave up after {HTTP_RETRIES} attempts ({last_exc})")


def _http_get_json_with_retry(client: httpx.Client, url: str, *, what: str) -> object:
    """Same retry contract as :func:`_http_get_with_retry` but for the
    small JSON listing endpoints (TCIA getSeries / GDC /files). The REST
    frontends occasionally drop the connection between connect and first
    byte ('Server disconnected without sending a response'), which
    without retry kills the whole subject.
    """
    last_exc: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = client.get(url)
            if 400 <= resp.status_code < 500:
                raise click.ClickException(
                    f"{what}: HTTP {resp.status_code} (client error, no retry) {url}"
                )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt < HTTP_RETRIES:
                click.echo(
                    f"  retry {attempt}/{HTTP_RETRIES} after {RETRY_BACKOFF_SEC}s: {exc}",
                    err=True,
                )
                time.sleep(RETRY_BACKOFF_SEC * attempt)
    raise click.ClickException(f"{what}: gave up after {HTTP_RETRIES} attempts ({last_exc})")
