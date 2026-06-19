"""Content-safety screening seam (CSAM / NSFW) for public contributions.

Anything offered to the public OpenData library must be screened for illegal /
abusive imagery before a human reviewer ever opens it. Actual detection is a
third-party service (perceptual-hash matching / classifier); this module defines
only the seam + a fail-safe null default. A positive hit BLOCKS the submission
(``blocked`` has no human-overridable accept edge in the state machine), so a
reviewer is never shown such content.

Wire a real provider by implementing :class:`ContentSafetyScreener` and
returning it from :func:`get_screener` (gated on config). Until then the
``NullScreener`` passes but records that no screening ran — the absence is
visible, never a silent "safe".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ScreenResult:
    verdict: str  # "pass" | "block" | "error"
    categories: tuple[str, ...] = ()
    provider: str = "null"


@runtime_checkable
class ContentSafetyScreener(Protocol):
    async def screen(self, image_bytes: bytes) -> ScreenResult: ...


class NullScreener:
    """Default when no provider is configured."""

    async def screen(self, image_bytes: bytes) -> ScreenResult:
        return ScreenResult(verdict="pass", provider="null")


def get_screener() -> ContentSafetyScreener:
    """Resolve the configured screener. Only the null provider ships today; a
    real provider (e.g. a hash-matching service) plugs in here behind config."""
    # config knobs (BVP_CONTENT_SAFETY_PROVIDER / _ENDPOINT) land with the real
    # provider; absent config -> NullScreener.
    return NullScreener()


__all__ = ["ContentSafetyScreener", "NullScreener", "ScreenResult", "get_screener"]
