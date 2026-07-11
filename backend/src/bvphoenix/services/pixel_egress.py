"""Burned-in-pixel gate for the PUBLIC serve paths (download + DICOMweb).

``services.deidentify`` scrubs headers at egress; pixels ship untouched. For
publicly-readable studies that is a PHI leak vector (ultrasound banners,
secondary captures) — the M0 gate covered ``training_cohort_export`` but the
per-download serve surfaces (``/instances/{id}/file``, WADO-RS retrieve /
frames / bulkdata) grew later without it. This module is the shared gate.

Scope: the gate applies to the PUBLIC visibility tiers only — an in-place t3
(training pool) study and contributed public studies (``is_public`` without an
external ``source_collection``). Private de-identified share links between
colleagues stay header-only by design (epic decision), and externally-imported
public datasets (TCIA, ...) are de-identified upstream and served verbatim.

Admission for a gated high-risk instance comes exclusively from persisted
human-verified state (``instances.pixel_deid_status == 'approved'``, written by
the contribution promote): serve the verified-clean blob
(``pixel_clean_s3_*`` pointer, or the stored bytes themselves for a t4 public
clone that is clean at rest) — otherwise withhold. ``classify_pixel_risk``
never trusts ``BurnedInAnnotation=NO``, so there is deliberately no way to
"re-check" raw bytes into admission.

**Single source of truth.** The risk decision reads the persisted
``instances.pixel_phi_risk`` (written at ingest by ``classify_pixel_risk`` over
the immutable stored header) — so the download and DICOMweb egress paths reach
the SAME verdict for the same instance. ``classify_pixel_risk`` is a pure
function of header attributes that we never mutate, so the persisted value
equals a fresh re-classification for a given classifier version. A NULL value
(legacy rows pre-migration 0034) is re-classified from the bytes. **Operational
contract:** a change to the classifier's rules requires a ``pixel_phi_risk``
backfill, exactly as a ``deid_method_version`` bump requires a re-scrub —
otherwise stale rows keep their old verdict on both paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bvphoenix.services.face_deid import get_defacer
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes


def pixel_gate_needed(study: Any) -> bool:
    """True when ``study``'s tier makes burned-in-pixel PHI a public leak.

    Mirrors the study-tier half of ``should_deidentify`` (t3, or contributed
    ``is_public`` with no ``source_collection``) but deliberately NOT its
    share-grant half: a private de-identified share is not a public egress.
    """
    if getattr(study, "contribution_tier", None) == "t3":
        return True
    return bool(
        getattr(study, "is_public", False) and not getattr(study, "source_collection", None)
    )


@dataclass(frozen=True)
class PixelGateResult:
    """``bytes_out`` is what may ship (None = withhold, fail-closed);
    ``substituted`` is True when the verified-clean blob replaced the input."""

    bytes_out: bytes | None
    substituted: bool = False


def resolve_public_pixel_bytes(storage: Any, instance: Any, raw: bytes) -> PixelGateResult:
    """Gate one instance's bytes on a public path. Sync (boto3 + pydicom
    header parse); callers offload to a thread.

    The risk verdict comes from the persisted ``instance.pixel_phi_risk`` (the
    single source of truth — see the module docstring); a NULL value (legacy
    rows) is re-classified from ``raw``. Face-risk (``low``) participates only
    when de-facing is enabled, matching the training-cohort export gate + the
    DICOMweb plan.
    """
    persisted = getattr(instance, "pixel_phi_risk", None)
    level = persisted if persisted is not None else classify_pixel_risk_bytes(raw).level
    deface_on = get_defacer() is not None
    if not (level == "high" or (deface_on and level == "low")):
        return PixelGateResult(bytes_out=raw)
    if getattr(instance, "pixel_deid_status", None) != "approved":
        return PixelGateResult(bytes_out=None)
    bucket = getattr(instance, "pixel_clean_s3_bucket", None)
    key = getattr(instance, "pixel_clean_s3_key", None)
    if bucket and key:
        try:
            clean = storage.get_object_bytes(bucket=bucket, key=key)
        except Exception:
            # Pointer dangling (blob lost): withhold rather than fall back to
            # the raw high-risk bytes.
            return PixelGateResult(bytes_out=None)
        return PixelGateResult(bytes_out=clean, substituted=True)
    # ``approved`` with no pointer: the stored bytes ARE the verified-clean
    # blob (t4 public clone) — serve them as-is.
    return PixelGateResult(bytes_out=raw)


def instance_gate_plan(instance: Any, *, deface_on: bool) -> str:
    """DB-only pre-decision for multipart planning (no byte fetch):

    * ``"serve"``       — persisted risk says no gate applies;
    * ``"substitute"``  — approved, verified-clean pointer to fetch;
    * ``"approved"``    — approved, clean at rest (serve stored bytes);
    * ``"withhold"``    — gated and not human-approved;
    * ``"classify"``    — persisted risk is NULL (legacy row): the caller must
      load the bytes and run :func:`resolve_public_pixel_bytes`.
    """
    risk = getattr(instance, "pixel_phi_risk", None)
    if risk is None:
        return "classify"
    if not (risk == "high" or (deface_on and risk == "low")):
        return "serve"
    if getattr(instance, "pixel_deid_status", None) != "approved":
        return "withhold"
    if getattr(instance, "pixel_clean_s3_bucket", None) and getattr(
        instance, "pixel_clean_s3_key", None
    ):
        return "substitute"
    return "approved"


__all__ = [
    "PixelGateResult",
    "instance_gate_plan",
    "pixel_gate_needed",
    "resolve_public_pixel_bytes",
]
