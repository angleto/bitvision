"""Content-safety screening seam (CSAM / NSFW) for public contributions.

Anything offered to the public OpenData library must be screened for illegal /
abusive imagery before a human reviewer ever opens it. A positive hit BLOCKS the
submission (``blocked`` has no human-overridable accept edge in the state
machine), so a reviewer is never shown such content.

Two implementations behind one Protocol:

* :class:`NullScreener`, the default when no provider is configured. It passes
  but records ``provider="null"`` so the *absence* of screening is visible in
  provenance, never a silent "safe". This is the only honest default: blocking
  every contribution when nobody wired a screener would just break the pipeline,
  while silently marking them "screened" would be a lie.
* :class:`HttpContentSafetyScreener`, calls an in-cluster screening service.
  It mirrors the storage-isolation posture of ``pixel_phi_engine``: a
  **host allowlist** means an image is NEVER POSTed to a host outside the
  in-cluster set, and any failure (disallowed host, timeout, non-2xx, bad body)
  fails **CLOSED to "block"**, a configured-but-broken screener withholds the
  contribution rather than letting unscreened content through to a reviewer.

The asymmetry is deliberate: *not configured* → pass with a visible gap;
*configured but unable to run* → block. A real provider plugs in by extending
this module; no caller changes.

Note on storage isolation: external hash-matching providers (PhotoDNA/PDQ) must
be fed a robust **hash**, not the raw PHI-bearing pixels. The HTTP seam here
posts image bytes to an *in-cluster* service by default (allowlist); wiring an
external provider requires that service to hash locally and forward only the
hash, and the operator to add its host to the allowlist explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenResult:
    verdict: str  # "pass" | "block" | "error"
    categories: tuple[str, ...] = ()
    provider: str = "null"


@runtime_checkable
class ContentSafetyScreener(Protocol):
    async def screen(self, image_bytes: bytes) -> ScreenResult: ...


class NullScreener:
    """Default when no provider is configured: pass, but record the absence."""

    async def screen(self, image_bytes: bytes) -> ScreenResult:
        return ScreenResult(verdict="pass", provider="null")


@dataclass
class HttpContentSafetyScreener:
    """Calls an in-cluster screening service. Fail-closed toward ``block``."""

    endpoint: str
    allowed_hosts: frozenset[str]
    timeout: float = 8.0
    provider_name: str = "http"

    async def screen(self, image_bytes: bytes) -> ScreenResult:
        host = (httpx.URL(self.endpoint).host or "").strip()
        if host not in self.allowed_hosts:
            # Storage isolation + fail-closed: never POST an image to a host
            # outside the allowlist, and treat the misconfig as "block" so
            # unscreened content is withheld rather than shown.
            logger.error(
                "content-safety endpoint host %r not in allowlist %s; blocking",
                host,
                sorted(self.allowed_hosts),
            )
            return ScreenResult(
                verdict="block", categories=("misconfigured",), provider=self.provider_name
            )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.endpoint.rstrip('/')}/screen",
                    content=image_bytes,
                    headers={"content-type": "application/octet-stream"},
                )
                resp.raise_for_status()
                data = resp.json()
            verdict = str(data.get("verdict", "")).lower()
            if verdict not in {"pass", "block"}:
                # An unrecognised verdict is not trustworthy → block.
                logger.error("content-safety returned unknown verdict %r; blocking", verdict)
                return ScreenResult(
                    verdict="block", categories=("bad_response",), provider=self.provider_name
                )
            categories = tuple(str(c) for c in (data.get("categories") or ()))
            return ScreenResult(verdict=verdict, categories=categories, provider=self.provider_name)
        except Exception as exc:  # any failure (timeout, non-2xx, bad body) → fail-closed
            logger.warning("content-safety screen failed (%s); blocking", exc)
            return ScreenResult(
                verdict="block", categories=("screen_error",), provider=self.provider_name
            )


def get_screener() -> ContentSafetyScreener:
    """Resolve the configured screener from config. Absent/``null`` provider →
    NullScreener; ``http`` → HttpContentSafetyScreener (host-allowlisted)."""
    from bvphoenix.config import get_settings

    s = get_settings()
    provider = (getattr(s, "content_safety_provider", "") or "").strip().lower()
    if provider in {"", "null"}:
        return NullScreener()
    if provider == "http":
        endpoint = (getattr(s, "content_safety_endpoint", "") or "").strip()
        allowed = frozenset(
            h.strip()
            for h in (getattr(s, "content_safety_allowed_hosts", "") or "").split(",")
            if h.strip()
        )
        if not endpoint:
            # Provider says http but no endpoint → cannot screen → fail closed by
            # returning a screener that always blocks (a Null pass here would be
            # a silent gap with the provider explicitly enabled).
            logger.error("content_safety_provider=http but no endpoint configured; blocking all")
            return _AlwaysBlockScreener()
        return HttpContentSafetyScreener(
            endpoint=endpoint, allowed_hosts=allowed, timeout=s.content_safety_timeout
        )
    logger.error("unknown content_safety_provider %r; blocking all", provider)
    return _AlwaysBlockScreener()


class _AlwaysBlockScreener:
    """Used when a provider is enabled but misconfigured: fail-closed to block."""

    async def screen(self, image_bytes: bytes) -> ScreenResult:
        return ScreenResult(verdict="block", categories=("misconfigured",), provider="error")


__all__ = [
    "ContentSafetyScreener",
    "HttpContentSafetyScreener",
    "NullScreener",
    "ScreenResult",
    "get_screener",
]
