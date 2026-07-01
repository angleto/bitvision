"""Allow a coarse ``study`` text target on the embedding stores.

The 4th `/api/search/hybrid` arm (``text_dense``) projects coarse
``report_content`` / ``finding`` vectors to a study. Real exams carry DICOM
SR (not app-level ``report_content``) and few app findings, so in prod the
arm contributes ~0 (task 0ece383b). Give every study a coarse whole-object
vector composed from its structural metadata (study_description + modalities
+ series body parts) — the target_kind ``study`` (``target_id`` IS the study
id, no projection needed). This also covers the public OpenData studies,
which have no reports/findings and therefore no dense text today.

Purely additive to the CHECK constraints (no data rewrite), mirroring
migration 0026. Downgrade drops the newly-admitted rows first so the
narrower constraint can be re-added.

Revision ID: 0041_text_embeddings_study_target
Revises: 0040_finding_vocab_snomed_codes
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op

revision = "0041_text_embeddings_study_target"
down_revision = "0040_finding_vocab_snomed_codes"
branch_labels = None
depends_on = None

_NEW = (
    "series",
    "report",
    "report_content",
    "annotation",
    "consultation",
    "document",
    "patient",
    "document_chunk",
    "finding",
    "study",
)
_OLD = tuple(k for k in _NEW if k != "study")


def _check_sql(table: str, constraint: str, kinds: tuple[str, ...]) -> str:
    arr = ", ".join(f"'{k}'::text" for k in kinds)
    return (
        f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
        f"CHECK ((target_kind = ANY (ARRAY[{arr}])))"
    )


def _reset(table: str, constraint: str, kinds: tuple[str, ...]) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
    op.execute(_check_sql(table, constraint, kinds))


def upgrade() -> None:
    _reset("text_embeddings", "ck_text_embeddings_target_kind", _NEW)
    _reset("text_embeddings_bge_m3", "ck_text_embeddings_bge_m3_target_kind", _NEW)


def downgrade() -> None:
    op.execute("DELETE FROM text_embeddings WHERE target_kind = 'study'")
    _reset("text_embeddings", "ck_text_embeddings_target_kind", _OLD)
    op.execute("DELETE FROM text_embeddings_bge_m3 WHERE target_kind = 'study'")
    _reset("text_embeddings_bge_m3", "ck_text_embeddings_bge_m3_target_kind", _OLD)
