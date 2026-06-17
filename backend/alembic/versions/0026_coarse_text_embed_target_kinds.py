"""Allow coarse text targets on the embedding stores (report_content + finding/BGE-M3).

Auto-embed coarse text targets on write (Flow task 84220e21). Two gaps:

* ``report_content`` was a /search/semantic coarse arm (api.search_semantic
  ``_candidates_report``) but no store's CHECK constraint allowed the kind,
  so nothing could ever be written there. Widen BOTH stores to admit it.
* ``finding`` text was embedded into the MiniLM store only (migration 0021
  deliberately left BGE-M3 untouched "until BGE-M3 becomes the default text
  model"). Now that coarse embedding fans out over every active text model,
  widen the BGE-M3 store to admit ``finding`` too.

Purely additive to the CHECK constraints (no data rewrite). Downgrade drops
the newly-admitted rows first so the narrower constraint can be re-added.

Revision ID: 0026_coarse_text_embed_target_kinds
Revises: 0025_patient_inbox
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op

revision = "0026_coarse_text_embed_target_kinds"
down_revision = "0025_patient_inbox"
branch_labels = None
depends_on = None

# MiniLM store (text_embeddings): already admits 'finding' (migration 0021);
# this revision adds 'report_content'.
_ML_NEW = (
    "series",
    "report",
    "report_content",
    "annotation",
    "consultation",
    "document",
    "patient",
    "document_chunk",
    "finding",
)
_ML_OLD = tuple(k for k in _ML_NEW if k != "report_content")

# BGE-M3 store (text_embeddings_bge_m3): adds both 'report_content' and
# 'finding' (0021 left this store untouched).
_BGE_NEW = (
    "series",
    "report",
    "report_content",
    "annotation",
    "consultation",
    "document",
    "patient",
    "document_chunk",
    "finding",
)
_BGE_OLD = tuple(k for k in _BGE_NEW if k not in ("report_content", "finding"))


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
    _reset("text_embeddings", "ck_text_embeddings_target_kind", _ML_NEW)
    _reset("text_embeddings_bge_m3", "ck_text_embeddings_bge_m3_target_kind", _BGE_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM text_embeddings WHERE target_kind = 'report_content'")
    _reset("text_embeddings", "ck_text_embeddings_target_kind", _ML_OLD)
    op.execute(
        "DELETE FROM text_embeddings_bge_m3 WHERE target_kind IN ('report_content', 'finding')"
    )
    _reset("text_embeddings_bge_m3", "ck_text_embeddings_bge_m3_target_kind", _BGE_OLD)
