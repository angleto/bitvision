"""AppSetting — generic instance-wide key/value store.

A small, cache-friendly table for runtime configuration that admins
can change without a redeploy. The viewer reads e.g.
``viewer.marker.fade.range`` from here; the publish flow may read
``deid.llm.enabled_by_default``; etc. Two scopes:

* ``scope='public'`` — readable by any authenticated user. The UI
  layer caches these and uses them to drive client-side rendering
  toggles (fade range, default colors, …). Never put secrets here.
* ``scope='admin'`` — admin-only. Backend behaviour toggles, billing
  thresholds, anything operational.

Conventions for ``key``:
  * dot-namespaced, lowercase (``viewer.marker.fade.range``)
  * the namespace mirrors the surface that consumes the value
  * stick to <128 chars

The ``value`` column is JSONB so a single row can hold a number,
a bool, a string, or a structured object — whatever the caller
needs. Type discipline is the consumer's job.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from bvphoenix.db.base import Base

SETTING_SCOPES: tuple[str, ...] = ("public", "admin")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool] = mapped_column(JSONB, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="admin")
    description: Mapped[str | None] = mapped_column(Text)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
    )

    __table_args__ = (CheckConstraint("scope IN ('public','admin')", name="ck_app_settings_scope"),)
