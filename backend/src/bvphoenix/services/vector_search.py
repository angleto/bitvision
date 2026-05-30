"""Per-request pgvector HNSW query tuning.

Centralises the ``SET LOCAL`` knobs that govern the recall/latency
trade-off of every ANN search so the four vector sites (study
similarity, semantic text-to-anything, the hybrid image signal, and
per-patient chunk RAG) tune them the same way instead of hard-coding
magic numbers inline or, worse, leaving them at defaults.

Two knobs:

* ``hnsw.ef_search`` — the size of the dynamic candidate list a probe
  keeps. Higher = better recall, more CPU. The default is 40, tuned for
  ``k≈10``; our hybrid / semantic paths over-fetch ``k×3..4`` and rerank,
  so they want a wider list. Settable on any pgvector that ships HNSW
  (>= 0.5), so it is always applied.

* ``hnsw.iterative_scan`` — when the WHERE filter is *more* selective
  than the index can exploit (the per-patient case: one patient out of
  thousands, or a visibility set that excludes most rows), plain HNSW
  post-filtering returns far fewer than ``LIMIT`` rows — the classic
  "filtered HNSW under-returns" trap that silently drops relevant hits.
  Iterative scan keeps pulling from the graph until enough *filtered*
  rows are found, capped by ``hnsw.max_scan_tuples``. Added in pgvector
  0.8, so it is gated on the version probed once at startup
  (:func:`set_iterative_scan_supported`).

The capability flag is a module global rather than threaded through
``request.app.state`` so in-process callers (MCP tools, A2A skills) that
never see a ``Request`` get the same tuning as the HTTP path. It
defaults to ``False`` (safe: we simply never emit the 0.8-only GUC)
until the startup probe flips it.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ITERATIVE_SCAN_MIN_VERSION",
    "ef_search_for_k",
    "iterative_scan_supported",
    "parse_pgvector_version",
    "set_iterative_scan_supported",
    "tune_vector_query",
]

# pgvector release that introduced ``hnsw.iterative_scan``.
ITERATIVE_SCAN_MIN_VERSION: tuple[int, int, int] = (0, 8, 0)

# Upper bound on tuples an iterative scan will visit before giving up —
# stops a pathologically-selective filter from walking the whole graph.
_MAX_SCAN_TUPLES = 20000

_iterative_scan_supported: bool = False


def set_iterative_scan_supported(value: bool) -> None:
    """Record whether the connected pgvector supports iterative scan.

    Called once from the app startup probe. Idempotent.
    """
    global _iterative_scan_supported
    _iterative_scan_supported = bool(value)


def iterative_scan_supported() -> bool:
    """True iff ``hnsw.iterative_scan`` may be set on this deployment."""
    return _iterative_scan_supported


def parse_pgvector_version(raw: str | None) -> tuple[int, ...] | None:
    """Parse ``pg_extension.extversion`` (e.g. ``'0.8.0'``) into a tuple.

    Returns ``None`` on anything unparseable so the caller can default
    to "feature unavailable".
    """
    if not raw:
        return None
    parts: list[int] = []
    for chunk in raw.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def ef_search_for_k(k: int) -> int:
    """Map a requested result count to an ``hnsw.ef_search`` value.

    Three tiers: autocomplete-grade for small ``k``, a wider list for the
    typical over-fetched candidate slab, widest for deep requests. Capped
    so a caller passing ``k=100`` cannot blow the latency budget.
    """
    if k <= 10:
        return 64
    if k <= 50:
        return 100
    return 160


async def tune_vector_query(db: AsyncSession, *, k: int, filtered: bool = False) -> None:
    """Apply HNSW tuning for the next vector query on this transaction.

    ``filtered`` should be true whenever the query restricts the result
    set with a WHERE predicate the HNSW index cannot satisfy (per-patient
    scope, visibility set, modality): that is when iterative scan earns
    its keep. ``SET LOCAL`` keeps every knob scoped to the current
    transaction, so there is no cross-request leakage.
    """
    # ef_search takes an integer literal; it is computed here (never
    # caller-supplied as a string) so the inline format is injection-safe.
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search_for_k(k)}"))
    if filtered and _iterative_scan_supported:
        await db.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        await db.execute(text(f"SET LOCAL hnsw.max_scan_tuples = {_MAX_SCAN_TUPLES}"))
