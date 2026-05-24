"""User-facing storage usage endpoint.

``GET /api/me/storage`` returns the current bytes-used + quota for
the live user, plus a friendly percent + a small breakdown of the
top-3 patients by bytes so the UI can hint where the space went.

Strictly user-scoped: never lists patients the caller does not
manage. Anonymous callers get 401 (we cannot resolve a subject).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import require_user
from bvphoenix.db.models import User
from bvphoenix.db.session import get_db
from bvphoenix.services.storage_quota import GB_IN_BYTES, get_storage_usage

router = APIRouter(tags=["storage"])


class TopPatientOut(BaseModel):
    patient_id: str
    display_name: str | None
    bytes_used: int


class StorageUsageOut(BaseModel):
    bytes_used: int
    bytes_quota: int
    quota_gb: float
    used_gb: float
    percent: float
    is_workspace_default: bool
    top_patients: list[TopPatientOut]


_TOP_PATIENTS_QUERY = sql_text(
    """
    WITH owned AS (
        SELECT id, display_name FROM patients WHERE managed_by_subject_id = :sid
    ),
    dicom AS (
        SELECT s.patient_id AS pid, COALESCE(SUM(i.size_bytes), 0) AS b
        FROM instances i
        JOIN series ser ON ser.id = i.series_id
        JOIN imaging_studies s ON s.id = ser.study_id
        WHERE s.patient_id IN (SELECT id FROM owned)
        GROUP BY s.patient_id
    ),
    docs AS (
        SELECT d.patient_id AS pid, COALESCE(SUM(df.size_bytes), 0) AS b
        FROM document_files df
        JOIN documents d ON d.id = df.document_id
        WHERE d.patient_id IN (SELECT id FROM owned)
        GROUP BY d.patient_id
    ),
    totals AS (
        SELECT pid, SUM(b)::bigint AS bytes
        FROM (SELECT * FROM dicom UNION ALL SELECT * FROM docs) u
        GROUP BY pid
    )
    SELECT o.id, o.display_name, COALESCE(t.bytes, 0) AS bytes
    FROM owned o
    LEFT JOIN totals t ON t.pid = o.id
    ORDER BY bytes DESC
    LIMIT 3
    """
)


@router.get("/me/storage", response_model=StorageUsageOut)
async def get_my_storage(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
) -> StorageUsageOut:
    """Return the live user's storage usage + top-3 patients by bytes.

    The quota is resolved per-subject (workspace default + admin
    override). Percent caps at 100 for the UI bar — the caller can
    inspect ``bytes_used > bytes_quota`` to detect the (rare) over-
    quota state caused by an admin lowering the cap retroactively.
    """
    usage = await get_storage_usage(db, subject_id=user.subject_id)

    top_rows = (await db.execute(_TOP_PATIENTS_QUERY, {"sid": user.subject_id})).all()

    percent = (usage.bytes_used / usage.bytes_quota) * 100.0 if usage.bytes_quota > 0 else 0.0

    return StorageUsageOut(
        bytes_used=usage.bytes_used,
        bytes_quota=usage.bytes_quota,
        quota_gb=usage.quota_gb,
        used_gb=round(usage.bytes_used / GB_IN_BYTES, 3),
        percent=round(min(percent, 999.0), 1),
        is_workspace_default=usage.is_workspace_default,
        top_patients=[
            TopPatientOut(
                patient_id=str(r[0]),
                display_name=r[1],
                bytes_used=int(r[2] or 0),
            )
            for r in top_rows
            if (r[2] or 0) > 0
        ],
    )


__all__ = ["router"]
