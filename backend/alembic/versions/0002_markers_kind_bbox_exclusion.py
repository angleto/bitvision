"""Widen ck_markers_kind to allow 'bbox.exclusion'.

The new kind is used by the viewer to persist axis-aligned regions
the operator wants ROI stats and hot-spot search to ignore (typically
kidneys and bladder on PET when an automatic anatomic segmentation
mask is unavailable). Geometry shape is identical to ``bbox.lesion``:
``{"min_ijk": [i, j, k], "max_ijk": [i', j', k']}``.

Revision ID: 0002_markers_kind_bbox_exclusion
Revises: 0001_initial_schema
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "0002_markers_kind_bbox_exclusion"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


_KINDS_WITH_EXCLUSION = (
    "measurement.distance",
    "measurement.angle",
    "measurement.area",
    "measurement.ellipse",
    "measurement.freehand",
    "measurement.arrow",
    "measurement.text",
    "measurement.probe",
    "measurement.bbox",
    "bbox.lesion",
    "bbox.exclusion",
    "fiducial",
    "reading-note",
    "text-overlay",
)

_KINDS_WITHOUT_EXCLUSION = tuple(k for k in _KINDS_WITH_EXCLUSION if k != "bbox.exclusion")


def _kinds_array_sql(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'::character varying" for k in kinds)


def upgrade() -> None:
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_kind CHECK "
        f"(((kind)::text = ANY ((ARRAY[{_kinds_array_sql(_KINDS_WITH_EXCLUSION)}])::text[])))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM markers WHERE kind = 'bbox.exclusion'")
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_kind CHECK "
        f"(((kind)::text = ANY ((ARRAY[{_kinds_array_sql(_KINDS_WITHOUT_EXCLUSION)}])::text[])))"
    )
