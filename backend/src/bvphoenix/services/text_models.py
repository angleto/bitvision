"""Registry-backed routing for text-chunk embedding models.

The fact that maps a registry text model (by ``model_id`` -- equal to the
worker ``MODEL_ID`` and to the value written into the store's ``model_id``
column) to (a) the arq task that produces its vectors and (b) the pgvector
store table those vectors live in, LIVES IN THE ``embedding_models`` ROW:
``model_metadata`` carries ``arq_task`` / ``store_table`` (+ the optional
``sparse_store_table`` / ``colbert_store_table`` auxiliary BGE-M3 stores)
and ``dim`` is the row's own column. Migration 0023 seeded the routing for
``minilm-multi-v1`` and ``bge-m3-v1``; new models get theirs via
``embedding_models.set_text_routing`` (CLI: ``bvphoenix-embed-models
set-routing``).

History: the same fact was first hand-duplicated across the read path
(``services/chunk_search.py``), the write path (workers
``chunk_and_embed.py``), the backfill CLI and the admin coverage API; then
unified into an in-code ``TEXT_MODELS`` dict (commit 866899b); now it is
data-driven from the row ``get_default_model`` already loads, so adding or
re-routing a text model is a registry write, not a code change. This module
keeps the parsing + validation of that row data.

Data-only on purpose (no model weights, no encoder callables, no ORM) so
the lean CLI and the worker can import it without dragging
``sentence-transformers`` or the model registry into import time. The
query-time encoder dispatch stays in ``chunk_search`` (which owns the lazy
``ai``-extra import) keyed on the model-id constants below: an encoder is
code by nature, so a model whose id has no registered encoder simply
contributes no dense arm.

Injection guard: ``store_table`` & friends are f-string-interpolated into
SQL by every consumer (table names cannot be bind parameters). Now that
they come from a DB row instead of an in-code literal, ``spec_from_registry``
refuses any value that is not a plain lowercase SQL identifier -- a
malformed registry row degrades that model to "unrouted" instead of
reaching a query string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

# Registry model_id values (== worker MODEL_ID == the stored model_id
# column == the value pinned by each store table's CHECK constraint).
# Kept in code because the query-path encoder dispatch needs them; the
# routing itself is registry data.
MULTILINGUAL_MODEL_ID = "minilm-multi-v1"
BGE_M3_MODEL_ID = "bge-m3-v1"

# model_metadata keys that make up a text model's routing.
ROUTING_KEYS = ("arq_task", "store_table", "sparse_store_table", "colbert_store_table")

# Plain lowercase SQL identifier / arq task name. Deliberately strict:
# every value is interpolated into SQL or handed to arq verbatim.
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class TextModelSpec:
    """How to produce, and where to store, one text model's chunk vectors.

    ``sparse_store_table`` / ``colbert_store_table`` are the optional auxiliary
    BGE-M3 stores (lexical sparsevec + packed ColBERT token-vectors) of the
    SAME model; None for dense-only models (MiniLM). The query path adds the
    sparse RRF arm + the ColBERT MaxSim rerank ONLY when these are set, so a
    registry flip-back to a dense-only model disables both arms automatically.
    """

    model_id: str
    arq_task: str
    store_table: str
    dim: int
    sparse_store_table: str | None = None
    colbert_store_table: str | None = None


def _require_ident(value: Any, key: str, model_name: str) -> str:
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise ValueError(
            f"embedding model {model_name!r}: routing key {key!r} = {value!r} "
            "is not a plain lowercase identifier"
        )
    return value


def spec_from_registry(
    name: str,
    dim: int,
    metadata: dict[str, Any] | None,
) -> TextModelSpec | None:
    """Parse one ``embedding_models`` row into a :class:`TextModelSpec`.

    Returns ``None`` when the row carries no routing at all (neither
    ``arq_task`` nor ``store_table`` -- e.g. the dormant
    ``biomedclip-text-v1`` row), so callers can skip unrouted models.
    Raises :class:`ValueError` on partial or malformed routing: half a
    route is an operator mistake that must surface, not silently behave
    like "no route".
    """
    meta = metadata or {}
    if "arq_task" not in meta and "store_table" not in meta:
        return None
    if "arq_task" not in meta or "store_table" not in meta:
        raise ValueError(
            f"embedding model {name!r}: routing requires both 'arq_task' and "
            f"'store_table' (got {sorted(k for k in ROUTING_KEYS if k in meta)})"
        )
    sparse = meta.get("sparse_store_table")
    colbert = meta.get("colbert_store_table")
    return TextModelSpec(
        model_id=name,
        arq_task=_require_ident(meta["arq_task"], "arq_task", name),
        store_table=_require_ident(meta["store_table"], "store_table", name),
        dim=int(dim),
        sparse_store_table=(
            _require_ident(sparse, "sparse_store_table", name) if sparse is not None else None
        ),
        colbert_store_table=(
            _require_ident(colbert, "colbert_store_table", name) if colbert is not None else None
        ),
    )


# Active, non-deprecated text models -- the worker dual-write loop and the
# admin surface route off this set. Raw SQL (no ORM import) on purpose;
# see module docstring.
_ACTIVE_TEXT_MODELS_SQL = text(
    "SELECT name, dim, model_metadata FROM embedding_models "
    "WHERE kind = 'text' AND is_active AND deprecated_at IS NULL "
    "ORDER BY name"
)


def _specs_from_rows(rows: Any) -> dict[str, TextModelSpec]:
    specs: dict[str, TextModelSpec] = {}
    for name, dim, metadata in rows:
        spec = spec_from_registry(name, dim, metadata)
        if spec is not None:
            specs[name] = spec
    return specs


async def load_text_model_specs(db: Any) -> dict[str, TextModelSpec]:
    """Load every routed, active, non-deprecated text model from the registry.

    ``db`` is an ``AsyncSession`` (or anything with an async ``execute``).
    Unrouted rows are skipped; a malformed routing raises ``ValueError``
    (see :func:`spec_from_registry`).
    """
    rows = (await db.execute(_ACTIVE_TEXT_MODELS_SQL)).all()
    return _specs_from_rows(rows)


def load_text_model_specs_sync(session: Any) -> dict[str, TextModelSpec]:
    """Sync twin of :func:`load_text_model_specs` for the backfill CLI."""
    rows = session.execute(_ACTIVE_TEXT_MODELS_SQL).all()
    return _specs_from_rows(rows)
