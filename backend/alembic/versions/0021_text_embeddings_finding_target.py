"""Allow target_kind='finding' on text_embeddings (semantic search over findings).

P3 (semantic slice) of the annotation-layer overhaul: a Finding's text
(coded type + anatomy + morphology + free-text description) is embedded
with the active multilingual MiniLM model under ``target_kind='finding'``
so findings join /search/semantic. Only the active minilm store is
widened here; the BGE-M3 stores are left untouched (findings are embedded
via embed_text_ml only) and would be widened when/if BGE-M3 becomes the
default text model.

Revision ID: 0021_text_embeddings_finding_target
Revises: 0020_findings_entity
Create Date: 2026-06-06
"""

from __future__ import annotations

from alembic import op

revision = "0021_text_embeddings_finding_target"
down_revision = "0020_findings_entity"
branch_labels = None
depends_on = None

_WITH_FINDING = (
    "series",
    "report",
    "annotation",
    "consultation",
    "document",
    "patient",
    "document_chunk",
    "finding",
)
_WITHOUT_FINDING = tuple(k for k in _WITH_FINDING if k != "finding")


def _check_sql(kinds: tuple[str, ...]) -> str:
    arr = ", ".join(f"'{k}'::text" for k in kinds)
    return (
        "ALTER TABLE text_embeddings ADD CONSTRAINT ck_text_embeddings_target_kind "
        f"CHECK ((target_kind = ANY (ARRAY[{arr}])))"
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE text_embeddings DROP CONSTRAINT IF EXISTS ck_text_embeddings_target_kind"
    )
    op.execute(_check_sql(_WITH_FINDING))


def downgrade() -> None:
    op.execute("DELETE FROM text_embeddings WHERE target_kind = 'finding'")
    op.execute(
        "ALTER TABLE text_embeddings DROP CONSTRAINT IF EXISTS ck_text_embeddings_target_kind"
    )
    op.execute(_check_sql(_WITHOUT_FINDING))
