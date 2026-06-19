"""Marker: pathology_slide target + polygon/point kinds.

The whole-slide-image (WSI) viewer reuses the generic ``Marker`` for its
annotations instead of a parallel table, so it inherits etag/If-Match,
soft-delete + restore, ``MarkerRevision`` history and agent provenance.

Two CHECK widenings:

* ``ck_markers_target_kind`` gains ``'pathology_slide'`` so a marker can
  anchor to a slide (target_id = pathology_slides.id).
* ``ck_markers_kind`` gains ``'measurement.polygon'`` (closed-polygon ROI)
  and ``'measurement.point'`` (cell / mitotic counter point).

Kind list is inlined as a point-in-time snapshot (a future taxonomy
change ships its own migration), mirroring 0019_marker_tracking.

Idempotent re-add of constraints; safe to re-run.

Revision ID: 0033_marker_pathology_target
Revises: 0032_pathology_dzi_tiles
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op

revision = "0033_marker_pathology_target"
down_revision = "0032_pathology_dzi_tiles"
branch_labels = None
depends_on = None


_KINDS_NEW = (
    "measurement.distance",
    "measurement.angle",
    "measurement.area",
    "measurement.ellipse",
    "measurement.freehand",
    "measurement.arrow",
    "measurement.text",
    "measurement.probe",
    "measurement.bbox",
    "measurement.polygon",
    "measurement.point",
    "measurement.sphere",
    "bbox.lesion",
    "bbox.exclusion",
    "fiducial",
    "reading-note",
    "text-overlay",
)
_KINDS_OLD = tuple(k for k in _KINDS_NEW if k not in ("measurement.polygon", "measurement.point"))

_TARGETS_NEW = ("study", "series", "instance", "pathology_slide")
_TARGETS_OLD = ("study", "series", "instance")


def _kinds_array_sql(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'::character varying" for k in kinds)


def _targets_in_sql(targets: tuple[str, ...]) -> str:
    return ", ".join(f"'{t}'" for t in targets)


def upgrade() -> None:
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_kind CHECK "
        f"(((kind)::text = ANY ((ARRAY[{_kinds_array_sql(_KINDS_NEW)}])::text[])))"
    )
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_target_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_target_kind "
        f"CHECK (target_kind IN ({_targets_in_sql(_TARGETS_NEW)}))"
    )


def downgrade() -> None:
    # Drop rows that the narrowed constraints would reject.
    op.execute("DELETE FROM markers WHERE target_kind = 'pathology_slide'")
    op.execute("DELETE FROM markers WHERE kind IN ('measurement.polygon','measurement.point')")

    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_target_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_target_kind "
        f"CHECK (target_kind IN ({_targets_in_sql(_TARGETS_OLD)}))"
    )
    op.execute("ALTER TABLE markers DROP CONSTRAINT IF EXISTS ck_markers_kind")
    op.execute(
        "ALTER TABLE markers ADD CONSTRAINT ck_markers_kind CHECK "
        f"(((kind)::text = ANY ((ARRAY[{_kinds_array_sql(_KINDS_OLD)}])::text[])))"
    )
