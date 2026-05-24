"""Per-user storage quota (F11.3).

Design (DESIGN.md §9 "storage free tier 10 GB per user; T3/T4 don't count"):

* Each user gets a **10 GiB** free tier for tiers **T1 (private) + T2
  (shared controlled)**. Uploads assigned to T3 (training opt-in) or
  T4 (public CC) do not count against the cap — the platform absorbs
  that storage in exchange for the consent to include the data in the
  training pool.
* Count is the sum of ``instances.size_bytes`` over studies owned by
  the user and currently in tier T1/T2. Derivatives (thumbnails, MPR
  cache, packed NIfTI) are generated artefacts; we do not bill them
  against the user's quota.
* Documents (``patient_documents``) have no ``size_bytes`` column yet
  and in practice are sub-MB; we exclude them. If the product ever
  opens PDF storage to large files this assumption has to be revisited.

The check is intentionally **best-effort** and **pre-commit**:

* Pre-commit: the upload handler estimates the incoming payload size
  (sum of UploadFile.read() lengths for drag-drop, request content
  length for STOW-RS, form-part totals for bulk) and raises 413 before
  touching S3 if the total would exceed the cap.
* Best-effort: a race where two concurrent uploads both pass the check
  and together cross the cap is possible. We accept that; the next
  upload will be rejected, and the overshoot is bounded by the
  per-upload cap (``MAX_STOW_BYTES``, ``MAX_FILE_BYTES``).

If a stricter guarantee is ever needed (e.g., paid tiers with hard
caps), move the check into a SERIALIZABLE transaction that locks the
studies row set, or precompute the usage in a dedicated table
maintained by triggers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ImagingStudy, Instance, Series

logger = logging.getLogger(__name__)


# 10 GiB (binary) — matches the "10 GB per user" promise in DESIGN.md §9.
# Using GiB because storage vendors and OS tools both report binary GiB
# under the "GB" label, and quoting the smaller decimal GB (10**9)
# would be a surprise loss of ~7.4 %.
STORAGE_FREE_TIER_BYTES: int = 10 * 1024**3


# Tiers that consume quota. T3 / T4 opt-in to the data commons; the
# platform absorbs their storage cost.
_QUOTA_BEARING_TIERS: tuple[str, ...] = ("t1", "t2")


@dataclass(frozen=True, slots=True)
class StorageUsage:
    used_bytes: int
    quota_bytes: int

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.quota_bytes - self.used_bytes)

    @property
    def exceeded(self) -> bool:
        return self.used_bytes > self.quota_bytes


async def get_user_storage_usage(
    db: AsyncSession, user_subject_id: uuid.UUID | str
) -> StorageUsage:
    """Return the user's current T1+T2 DICOM-instance footprint in bytes.

    The cap honours per-user overrides set from the admin dashboard:
    if ``users.storage_quota_bytes`` is non-NULL it wins over the
    platform default. NULL falls back to ``STORAGE_FREE_TIER_BYTES``
    so existing accounts behave exactly as before.
    """
    from bvphoenix.db.models import User

    q = (
        select(func.coalesce(func.sum(Instance.size_bytes), 0))
        .select_from(Instance)
        .join(Series, Series.id == Instance.series_id)
        .join(ImagingStudy, ImagingStudy.id == Series.study_id)
        .where(
            ImagingStudy.owner_subject_id == user_subject_id,
            ImagingStudy.contribution_tier.in_(_QUOTA_BEARING_TIERS),
        )
    )
    used = int((await db.execute(q)).scalar_one() or 0)

    override = (
        await db.execute(select(User.storage_quota_bytes).where(User.subject_id == user_subject_id))
    ).scalar_one_or_none()
    quota = int(override) if override is not None else STORAGE_FREE_TIER_BYTES
    return StorageUsage(used_bytes=used, quota_bytes=quota)


async def check_quota_or_raise(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID | str,
    tier: str,
    incoming_bytes: int,
) -> StorageUsage:
    """Enforce the free-tier cap before an upload commits.

    When ``tier`` is T3 or T4 the check is a no-op (returns the current
    usage for telemetry but does not raise). When ``tier`` is T1 or T2
    and ``current + incoming`` would exceed the cap, raises 413 with a
    structured detail so the client can show a useful message.
    """
    usage = await get_user_storage_usage(db, user_subject_id)
    if tier not in _QUOTA_BEARING_TIERS:
        return usage
    projected = usage.used_bytes + max(0, int(incoming_bytes))
    if projected > usage.quota_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "error": "storage_quota_exceeded",
                "used_bytes": usage.used_bytes,
                "quota_bytes": usage.quota_bytes,
                "incoming_bytes": int(incoming_bytes),
                "hint": (
                    "T1/T2 storage is capped at 10 GiB. Upgrade individual "
                    "studies to T3 (training opt-in) or T4 (public CC) to "
                    "free up quota, or delete unused uploads."
                ),
            },
        )
    return usage


__all__ = [
    "STORAGE_FREE_TIER_BYTES",
    "StorageUsage",
    "check_quota_or_raise",
    "get_user_storage_usage",
]
