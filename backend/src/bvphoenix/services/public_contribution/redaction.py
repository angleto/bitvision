"""Staged pixel redaction for public-contribution submissions.

What the reviewer approves must be what ships. The auto-check pass stages the
redacted bytes of every pixel-gated component (high burned-in risk, or face
risk with de-facing enabled) under a deterministic S3 key; the review UI
previews exactly those bytes; the promote hook stamps and publishes them; a
reject purges them. Re-deriving the redaction at egress would be unsound:
``classify_pixel_risk`` deliberately never trusts ``BurnedInAnnotation=NO``,
so admission can only come from persisted human-verified state — and the state
must vouch for concrete bytes.

Key layout mirrors the inbox staging convention (``_inbox/``): staged blobs
live in the raw bucket OUTSIDE the canonical fascicolo keyspace, keyed by
submission + instance so a re-run of the checks overwrites in place
(idempotent) and the purge can guard on the prefix.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field

import pydicom

from bvphoenix.config import get_settings
from bvphoenix.services.deid.errors import DeidVerificationError, RequiresReview
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.pixel_deid import clean_pixel_data
from bvphoenix.storage import S3Storage

# Staged (reviewed-but-unpublished) redacted blobs. Sibling of the inbox
# ``_inbox/`` prefix; both are outside ``patients/``.
CONTRIB_STAGED_PREFIX = "_contrib"
# Published verified-clean blobs for in-place tiers (t3): instance-keyed so a
# promote retry overwrites deterministically and any egress path can resolve
# the blob straight from the Instance row's pointer.
PIXEL_CLEAN_PREFIX = "_pixel-clean"

# Manifest cap: dense multi-frame US can produce thousands of OCR boxes; the
# manifest is a JSONB audit surface, not the mask store (the staged BYTES are).
MAX_MANIFEST_REDACTIONS = 500


def contrib_staged_prefix(submission_id: uuid.UUID | str) -> str:
    return f"{CONTRIB_STAGED_PREFIX}/{submission_id}"


def staged_redacted_key(submission_id: uuid.UUID | str, instance_id: str) -> str:
    return f"{contrib_staged_prefix(submission_id)}/{instance_id}.dcm"


def pixel_clean_key(instance_id: uuid.UUID | str) -> str:
    return f"{PIXEL_CLEAN_PREFIX}/{instance_id}.dcm"


@dataclass
class StagedRedaction:
    """Outcome of staging one component. ``key`` is None when nothing was
    uploaded (header de-id failure or undecodable pixels) — the component then
    has no publishable rendition and can never ship."""

    key: str | None
    sha256: str | None
    residual_suspect: bool
    reason: str | None = None
    redactions: list[dict] = field(default_factory=list)
    redactions_truncated: bool = False
    face_deid_applied: bool = False
    face_deid_reason: str | None = None
    deid_method_version: str | None = None


def stage_component_redaction(
    storage: S3Storage,
    *,
    submission_id: uuid.UUID | str,
    instance_id: str,
    raw: bytes,
    defacer: object = None,
) -> StagedRedaction:
    """Header-scrub + pixel-redact ``raw`` and upload the result to the staged
    key. Sync (pydicom + tesseract + boto3); callers offload to a thread.

    Fail-closed: a header-scrub failure (SR/encapsulated ``RequiresReview``,
    residual-PHI ``DeidVerificationError``, unparseable input) stages NOTHING —
    bytes whose header cannot be verified must never gain a publishable
    rendition. Undecodable pixels likewise upload nothing.
    """
    settings = get_settings()
    try:
        scrubbed = deidentify_dicom_bytes(raw)
    except RequiresReview as exc:
        return StagedRedaction(
            key=None, sha256=None, residual_suspect=True, reason=f"header_requires_review:{exc}"
        )
    except DeidVerificationError as exc:
        return StagedRedaction(
            key=None, sha256=None, residual_suspect=True, reason=f"header_residual_phi:{exc}"
        )
    except Exception as exc:
        return StagedRedaction(
            key=None,
            sha256=None,
            residual_suspect=True,
            reason=f"header_scrub_error:{type(exc).__name__}",
        )

    result = clean_pixel_data(scrubbed, face_defacer=defacer)
    if result.decode_failed:
        return StagedRedaction(key=None, sha256=None, residual_suspect=True, reason="decode_failed")

    key = staged_redacted_key(submission_id, instance_id)
    storage.upload_bytes(result.out_bytes, bucket=settings.s3_bucket_raw, key=key)
    truncated = len(result.redactions) > MAX_MANIFEST_REDACTIONS
    return StagedRedaction(
        key=key,
        sha256=hashlib.sha256(result.out_bytes).hexdigest(),
        residual_suspect=result.residual_suspect,
        redactions=list(result.redactions[:MAX_MANIFEST_REDACTIONS]),
        redactions_truncated=truncated,
        face_deid_applied=bool(result.face_deid_reason and result.redactions),
        face_deid_reason=result.face_deid_reason,
        deid_method_version=settings.deid_method_version,
    )


def stamp_clean_provenance(blob: bytes, *, risk_level: str, face_deid_applied: bool) -> bytes:
    """Stamp the human-accept provenance onto staged bytes: BurnedInAnnotation=NO
    + CID 7050 ``113101`` for redacted high-risk pixels, RecognizableVisualFeatures=NO
    + ``113102`` when a de-facing mask was applied. Call ONLY from the promote
    hook — after a human accepted the reviewed rendition (MIDI-B rule)."""
    from bvphoenix.services.pixel_deid import mark_pixels_clean, mark_visual_features_removed

    ds = pydicom.dcmread(io.BytesIO(blob))
    if risk_level == "high":
        mark_pixels_clean(ds)
    if face_deid_applied:
        mark_visual_features_removed(ds)
    out = io.BytesIO()
    ds.save_as(out, write_like_original=False)
    return out.getvalue()


__all__ = [
    "CONTRIB_STAGED_PREFIX",
    "MAX_MANIFEST_REDACTIONS",
    "PIXEL_CLEAN_PREFIX",
    "StagedRedaction",
    "contrib_staged_prefix",
    "pixel_clean_key",
    "stage_component_redaction",
    "staged_redacted_key",
    "stamp_clean_provenance",
]
