"""Widen the bge-m3 SPARSE + COLBERT stores' target_kind CHECK to match dense.

``embed_bge_m3_all`` writes DENSE + SPARSE + ColBERT in ONE transaction. The
dense store admitted the coarse kinds (``report_content``/``finding`` via
migration 0026, ``study`` via 0041), but the auxiliary SPARSE + COLBERT stores
were never widened — so a bge-m3 embed of any of those kinds failed the sparse
CHECK and rolled back the whole transaction (0 vectors). It only surfaced now
because the study backfill is the first HIGH-VOLUME bge-m3 write; report_content
/ finding are sparse in the corpus so it was latent since 0026.

Widen both auxiliary stores to the SAME full set as the dense store. Purely
additive; downgrade drops the newly-admitted rows first (mirrors 0026 / 0041).

Revision ID: 0042_bge_m3_aux_target_kinds
Revises: 0041_text_embeddings_study_target
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op

revision = "0042_bge_m3_aux_target_kinds"
down_revision = "0041_text_embeddings_study_target"
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
_OLD = ("series", "report", "annotation", "consultation", "document", "patient", "document_chunk")

_TABLES = (
    ("text_embeddings_bge_m3_sparse", "ck_text_embeddings_bge_m3_sparse_target_kind"),
    ("text_embeddings_bge_m3_colbert", "ck_text_embeddings_bge_m3_colbert_target_kind"),
)


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
    for table, constraint in _TABLES:
        _reset(table, constraint, _NEW)


def downgrade() -> None:
    newly = tuple(k for k in _NEW if k not in _OLD)
    arr = ", ".join(f"'{k}'" for k in newly)
    for table, constraint in _TABLES:
        op.execute(f"DELETE FROM {table} WHERE target_kind IN ({arr})")
        _reset(table, constraint, _OLD)
