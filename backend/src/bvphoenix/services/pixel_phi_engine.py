"""Pluggable hard-case engine for burned-in-pixel redaction (M5, VLM tier).

The cheap Tesseract tier (``services.pixel_deid.clean_pixel_data``) handles the
common case. When it finds NO text on a high-risk frame, the dense-overlay /
low-contrast / non-Latin case where OCR misses (red-team finding #7), this
engine is consulted for additional redaction boxes. It is **opt-in**
(``BVP_PIXEL_PHI_VLM_ENABLED``); off by default the Tesseract tier + the human
review quarantine are the safety floor.

Two implementations:

* :class:`NullPixelPhiEngine`, no model. Over-redacts the WHOLE uncertain frame
  (a single full-frame box), so a frame OCR couldn't read is fully masked toward
  the human reviewer rather than shipped. Recall-maximising, zero infra.
* :class:`HttpPixelPhiEngine`, calls an in-cluster service (``pixelphi-svc``:
  PaddleOCR detector + small classifier). It mirrors ``inference_client`` (env
  base URL, short timeout, fail-safe) and adds a **host allowlist**: a
  PHI-bearing crop is NEVER POSTed to a host outside the in-cluster/loopback
  allowlist. On a disallowed host or any error it falls back to over-redaction
  (fail-CLOSED toward masking, never toward a clean image), so the model is
  self-hosted by construction.

A stronger GPU model drops in behind the same Protocol without touching callers.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x, y, w, h


@runtime_checkable
class PixelPhiVlmEngine(Protocol):
    def detect_boxes(self, frame_gray: np.ndarray) -> list[Box]: ...


def _whole_frame(frame_gray: np.ndarray) -> list[Box]:
    h, w = int(frame_gray.shape[0]), int(frame_gray.shape[1])
    return [(0, 0, w, h)]


class NullPixelPhiEngine:
    """No model: over-redact the whole (uncertain) frame. Safe + shippable."""

    def detect_boxes(self, frame_gray: np.ndarray) -> list[Box]:
        return _whole_frame(frame_gray)


def _to_png_b64(frame_gray: np.ndarray) -> str:
    img = Image.fromarray(np.asarray(frame_gray).astype(np.uint8), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@dataclass
class HttpPixelPhiEngine:
    """Calls the in-cluster pixelphi-svc. Fail-closed toward over-redaction."""

    url: str
    allowed_hosts: frozenset[str]
    timeout: float = 8.0

    def detect_boxes(self, frame_gray: np.ndarray) -> list[Box]:
        host = (httpx.URL(self.url).host or "").strip()
        if host not in self.allowed_hosts:
            # Storage isolation: never POST a PHI-bearing crop to a host outside
            # the in-cluster allowlist (a misconfig must not leak to a public API).
            logger.error(
                "pixelphi engine host %r not in allowlist %s; over-redacting instead",
                host,
                sorted(self.allowed_hosts),
            )
            return _whole_frame(frame_gray)
        try:
            payload = {"image_png_b64": _to_png_b64(frame_gray)}
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.url.rstrip('/')}/detect", json=payload)
                resp.raise_for_status()
                data = resp.json()
            boxes = [
                (int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"]))
                for b in (data.get("boxes") or [])
            ]
            # A reachable model that returns nothing on an OCR-blank high-risk
            # frame is still suspicious → over-redact rather than trust "clean".
            return boxes or _whole_frame(frame_gray)
        except Exception as exc:  # any failure (timeout, non-2xx, bad body) → fail-closed
            logger.warning("pixelphi engine call failed (%s); over-redacting", exc)
            return _whole_frame(frame_gray)


def get_pixel_phi_engine() -> PixelPhiVlmEngine | None:
    """Resolve the configured engine, or None when the VLM tier is disabled
    (default), callers then rely on the Tesseract tier + human review."""
    from bvphoenix.config import get_settings

    s = get_settings()
    if not getattr(s, "pixel_phi_vlm_enabled", False):
        return None
    url = (getattr(s, "pixel_phi_svc_url", "") or "").strip()
    if not url:
        # Enabled but no service URL → NullEngine (over-redact uncertain frames).
        return NullPixelPhiEngine()
    allowed = frozenset(
        h.strip() for h in (getattr(s, "pixel_phi_allowed_hosts", "") or "").split(",") if h.strip()
    )
    return HttpPixelPhiEngine(url=url, allowed_hosts=allowed)


__all__ = [
    "HttpPixelPhiEngine",
    "NullPixelPhiEngine",
    "PixelPhiVlmEngine",
    "get_pixel_phi_engine",
]
