"""Per-subject storage quota with configurable limits.

The platform ships a workspace-wide default (5 GB) hard quota on the
total bytes a Subject owns across DICOM series instances + uploaded
documents. The admin can grant individual subjects a higher (or
lower) quota via a per-user override stored in ``app_settings`` —
same pattern as the AI tier resolver.

Resolution order (mirrors ``ai_tiers.resolve_tier_for_user``):

  1. ``app_settings['storage.user_quota_gb:<subject_id>']`` — admin
     override for that specific user. Wins when present and the
     workspace ``storage.allow_user_override`` flag is true (default).
  2. ``app_settings['storage.default_quota_gb']`` — workspace
     default. Defaults to ``DEFAULT_QUOTA_GB`` when missing.
  3. ``DEFAULT_QUOTA_GB`` hardcoded fallback.

What counts toward the quota
----------------------------
* ``dicom_instances.size_bytes`` — every uploaded DICOM image.
* ``document_files.size_bytes`` — every original blob stored for a
  patient document (PDFs, scans, DVD images, photos).
* ``patient_documents.s3_size_bytes`` (legacy single-file path) —
  for documents created before the multi-file refactor.

We attribute bytes to the patient's ``managed_by_subject_id``: the
clinician/organisation who owns the fascicolo eats the storage cost,
not the patient. Anonymous / orphan rows fall on the platform owner
and are not enforced (admin can clean them up out-of-band).

What it does NOT do
-------------------
* No ledger debit. This is a *hard quota*, not pay-as-you-go billing.
  The wallet stays AI-only per the round-5 user decision ("solo
  crediti, niente subscription"). When the user wants to lift the
  cap on a specific subject, the admin patches the per-user setting.
* No deletion. If somehow a subject is over quota (admin lowered the
  cap, bulk import slipped through), existing data stays — only new
  uploads are refused. Medical records are never auto-deleted.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import AppSetting

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_GB: float = 5.0
"""Workspace fallback quota in GB when no app_setting is present."""

KEY_DEFAULT_QUOTA_GB = "storage.default_quota_gb"
KEY_ALLOW_USER_OVERRIDE = "storage.allow_user_override"
KEY_USER_QUOTA_PREFIX = "storage.user_quota_gb:"

GB_IN_BYTES = 1024**3


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """Resolved quota state for one subject.

    All values are derived from authoritative DB queries; the caller
    can serialise this dataclass straight to the wire (no PHI).
    """

    bytes_used: int
    bytes_quota: int
    quota_gb: float
    is_workspace_default: bool
    """True when the active quota is the workspace default; False when
    an admin has set a per-user override on this subject."""


# ---------------------------------------------------------------------------
# Quota resolution
# ---------------------------------------------------------------------------


async def _read_setting(db: AsyncSession, key: str) -> object | None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    return row.value if row else None


def _coerce_gb(raw: object | None) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v


async def resolve_quota_gb(db: AsyncSession, *, subject_id: uuid.UUID) -> tuple[float, bool]:
    """Compute the active quota in GB for ``subject_id``.

    Returns ``(quota_gb, is_workspace_default)``: the second value
    distinguishes "this user has been bumped by the admin" from
    "this user is on the workspace default" so the UI can show a
    badge.
    """
    allow_override_raw = await _read_setting(db, KEY_ALLOW_USER_OVERRIDE)
    allow_override = True if allow_override_raw is None else bool(allow_override_raw)
    if allow_override:
        per_user = _coerce_gb(await _read_setting(db, f"{KEY_USER_QUOTA_PREFIX}{subject_id}"))
        if per_user is not None:
            return per_user, False

    # ``users.storage_quota_bytes`` is the canonical per-user override the
    # admin dashboard writes, and it is already honored by
    # ``quota.check_quota_or_raise`` (the free-tier gate). Honor it here too so
    # ONE admin-set value is the effective per-user quota for BOTH storage
    # gates. Without this, the two checks read different override stores: a
    # quota raised in the UI (users.storage_quota_bytes) left this hard-cap
    # gate on DEFAULT_QUOTA_GB, 413-ing uploads even though the user's
    # configured quota was far higher. Honored regardless of
    # ``allow_override`` (which only governs the app_setting layer above),
    # mirroring quota.py so the two gates can never diverge on the override.
    from bvphoenix.db.models import User

    override_bytes = (
        await db.execute(select(User.storage_quota_bytes).where(User.subject_id == subject_id))
    ).scalar_one_or_none()
    if override_bytes is not None:
        return int(override_bytes) / GB_IN_BYTES, False

    default = _coerce_gb(await _read_setting(db, KEY_DEFAULT_QUOTA_GB))
    if default is not None:
        return default, True
    return DEFAULT_QUOTA_GB, True


async def resolve_quota_bytes(db: AsyncSession, *, subject_id: uuid.UUID) -> int:
    gb, _ = await resolve_quota_gb(db, subject_id=subject_id)
    return int(gb * GB_IN_BYTES)


# ---------------------------------------------------------------------------
# Usage measurement
# ---------------------------------------------------------------------------


_USAGE_QUERY = sql_text(
    """
    WITH owned_patients AS (
        SELECT id FROM patients WHERE managed_by_subject_id = :sid
    ),
    dicom_total AS (
        SELECT COALESCE(SUM(i.size_bytes), 0) AS bytes
        FROM instances i
        JOIN series ser ON ser.id = i.series_id
        JOIN imaging_studies s ON s.id = ser.study_id
        WHERE s.patient_id IN (SELECT id FROM owned_patients)
    ),
    docfile_total AS (
        SELECT COALESCE(SUM(df.size_bytes), 0) AS bytes
        FROM document_files df
        JOIN documents d ON d.id = df.document_id
        WHERE d.patient_id IN (SELECT id FROM owned_patients)
    )
    SELECT (SELECT bytes FROM dicom_total) + (SELECT bytes FROM docfile_total) AS total
    """
)


async def compute_subject_storage_bytes(db: AsyncSession, *, subject_id: uuid.UUID) -> int:
    """Sum DICOM-instance + document-file bytes attributed to ``subject_id``.

    Attribution path: bytes belong to a patient → patient is managed
    by a Subject. We count DICOM (the dominant volume on radiology
    workloads) plus document_files (PDFs, scans, ISOs). Soft-deleted
    documents still count — the row is a tombstone, the bytes are
    still in S3 until ``purge_expired_documents`` runs.
    """
    row = (await db.execute(_USAGE_QUERY, {"sid": subject_id})).first()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


async def get_storage_usage(db: AsyncSession, *, subject_id: uuid.UUID) -> StorageUsage:
    bytes_used = await compute_subject_storage_bytes(db, subject_id=subject_id)
    quota_gb, is_default = await resolve_quota_gb(db, subject_id=subject_id)
    return StorageUsage(
        bytes_used=bytes_used,
        bytes_quota=int(quota_gb * GB_IN_BYTES),
        quota_gb=quota_gb,
        is_workspace_default=is_default,
    )


# ---------------------------------------------------------------------------
# Pre-flight gate
# ---------------------------------------------------------------------------


async def check_storage_quota(
    db: AsyncSession,
    *,
    subject_id: uuid.UUID,
    additional_bytes: int = 0,
) -> StorageUsage:
    """Raise HTTP 413 when ``additional_bytes`` would push ``subject_id``
    over their quota.

    Call this at the top of every upload endpoint before reading the
    request body. ``additional_bytes`` is the size we are *about* to
    add; on streaming uploads where the size is unknown, pass ``0``
    and call again after the upload commits — the pre-flight catches
    the obvious "already over" case and the post-flight refuses
    further uploads.

    Returns the live usage so the caller can include it in the
    response (e.g. for displaying remaining bytes after a successful
    upload).
    """
    usage = await get_storage_usage(db, subject_id=subject_id)
    projected = usage.bytes_used + max(0, additional_bytes)
    if projected > usage.bytes_quota:
        # 413 Payload Too Large is the cleanest HTTP code for "your
        # storage cap stops this upload"; clients can show a
        # dedicated "ricarica spazio" UX without confusing this with
        # 402 (credits) or 403 (auth).
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "storage_quota_exceeded",
                "bytes_used": usage.bytes_used,
                "bytes_quota": usage.bytes_quota,
                "quota_gb": usage.quota_gb,
                "additional_bytes": additional_bytes,
                "is_workspace_default": usage.is_workspace_default,
            },
        )
    return usage


__all__ = [
    "DEFAULT_QUOTA_GB",
    "GB_IN_BYTES",
    "KEY_ALLOW_USER_OVERRIDE",
    "KEY_DEFAULT_QUOTA_GB",
    "KEY_USER_QUOTA_PREFIX",
    "StorageUsage",
    "check_storage_quota",
    "compute_subject_storage_bytes",
    "get_storage_usage",
    "resolve_quota_bytes",
    "resolve_quota_gb",
]
