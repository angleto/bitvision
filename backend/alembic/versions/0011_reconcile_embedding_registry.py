"""Reconcile the embedding-model registry with reality.

The ``embedding_models`` registry was meant to be the source of truth
for "which model does search use", but its seed drifted from what the
workers actually write:

  * The image model is registered as ``biomedclip-image-v1`` but the
    ``embed_series`` worker writes ``embeddings.model_id = 'biomedclip-v1'``.
    Resolving the default image model from the registry therefore yielded
    a name that matches *no* stored row. Rename it to ``biomedclip-v1``.

  * The default-for-kind text model is ``biomedclip-text-v1`` (the
    BiomedCLIP text tower), but **nothing enqueues** that indexer — the
    only text embeddings actually produced are ``minilm-multi-v1`` (the
    multilingual MiniLM used by chunk search / ``embed_text_ml``). The
    registry default thus pointed at an empty model. Flip the
    default-for-kind to ``minilm-multi-v1``.

After this migration ``get_default_model('image').name`` /
``get_default_model('text').name`` equal the ``model_id`` strings the
code and workers use, so the registry can be consulted (and a startup
drift-guard alerts if it ever diverges again).

``biomedclip-text-v1`` is left active (a future cross-modal text path may
populate it) but no longer the default.

All statements are name-targeted and affect zero rows if a deployment
already reconciled the registry, so the migration is safe to re-run.

Revision ID: 0011_reconcile_embedding_registry
Revises: 0010_dual_config_fts
Create Date: 2026-05-29
"""

from __future__ import annotations

from alembic import op

revision = "0011_reconcile_embedding_registry"
down_revision = "0010_dual_config_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE public.embedding_models SET name = 'biomedclip-v1' "
        "WHERE name = 'biomedclip-image-v1'"
    )
    # Clear-then-set so the "one default per kind" partial unique index is
    # satisfied between the two statements (mirrors activate_model()).
    op.execute(
        "UPDATE public.embedding_models SET is_default_for_kind = false "
        "WHERE name = 'biomedclip-text-v1' AND kind = 'text'"
    )
    op.execute(
        "UPDATE public.embedding_models SET is_default_for_kind = true "
        "WHERE name = 'minilm-multi-v1' AND kind = 'text'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE public.embedding_models SET is_default_for_kind = false "
        "WHERE name = 'minilm-multi-v1' AND kind = 'text'"
    )
    op.execute(
        "UPDATE public.embedding_models SET is_default_for_kind = true "
        "WHERE name = 'biomedclip-text-v1' AND kind = 'text'"
    )
    op.execute(
        "UPDATE public.embedding_models SET name = 'biomedclip-image-v1' "
        "WHERE name = 'biomedclip-v1'"
    )
