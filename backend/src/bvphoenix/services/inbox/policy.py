"""Which ingress paths go through the review queue (fbbf5270 §9).

One function, one truth table — the upload endpoints and the e-mail
pipeline both ask here instead of hard-coding their own posture:

* e-mail            → always reviewed (the sender is unauthenticated);
* public ingress    → always reviewed (the other profile's business);
* owner upload (UI) → direct ingest, review opt-in per request;
* agent/MCP upload  → reviewed by default, the owner may trust the
  agent per request (the API still records provenance either way).
"""

from __future__ import annotations

from typing import Literal

IngressChannel = Literal["email", "upload_ui", "upload_mcp"]


def should_require_review(
    channel: IngressChannel,
    *,
    is_agent: bool,
    review_requested: bool = False,
) -> bool:
    """Decide whether an ingress lot is staged for review or ingested
    directly. ``review_requested`` is the per-request opt-in (a human
    uploading on behalf of a third party may *want* the queue)."""
    if channel == "email":
        return True
    if is_agent or channel == "upload_mcp":
        return True
    return review_requested


__all__ = ["IngressChannel", "should_require_review"]
