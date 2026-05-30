"""Radiology synonym expansion for full-text queries.

Wraps the ``search_synonyms`` table (migration 0013) so the FTS query
side can OR-expand acronyms and bilingual equivalents ("TC" -> "computed
tomography" / "tomografia computerizzata") without a redeploy. The
expansion is applied ONLY to the sparse (FTS) query, never to the dense
vector — the embedding models already capture semantics, and double
expanding would pollute the vector.

The active thesaurus is loaded into a module cache once at startup (it is
small and hot), so :func:`expand_tsquery` is synchronous and adds no
per-query DB round-trip. :func:`load_thesaurus` is idempotent and may be
re-run to pick up edits. If the table is absent (older DB) the cache
stays empty and expansion is a no-op — search degrades to plain
dual-config FTS rather than failing.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from bvphoenix.services.fts import dual_tsquery

__all__ = [
    "expand_tsquery",
    "load_thesaurus",
    "thesaurus_version",
]

# Cap the number of OR'd variant clauses so a pathological multi-term
# query cannot build an enormous tsquery.
_MAX_VARIANTS = 24

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")

_synonyms: dict[str, list[str]] = {}
_version: int = 0


async def load_thesaurus(db: AsyncSession) -> int:
    """Load the active synonym set into the module cache. Returns the row
    count loaded (0 if the table is absent)."""
    global _synonyms, _version
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT lower(term) AS term, variants, version "
                    "FROM public.search_synonyms WHERE is_active = true"
                )
            )
        ).all()
    except ProgrammingError:
        await db.rollback()
        _synonyms = {}
        _version = 0
        return 0
    table: dict[str, list[str]] = {}
    max_version = 0
    for term, variants, version in rows:
        table[term] = list(variants or [])
        max_version = max(max_version, int(version or 0))
    _synonyms = table
    _version = max_version
    return len(table)


def thesaurus_version() -> int:
    """Version of the loaded thesaurus (0 if none) — pin it in eval runs."""
    return _version


def expand_tsquery(q: str) -> ColumnElement:
    """Build a dual-config tsquery for ``q`` OR'd with the dual-config
    tsquery of each synonym surface form of the query's tokens.

    Falls back to the bare dual-config query when nothing expands, so the
    result is always at least as permissive as plain FTS.
    """
    base = dual_tsquery(q)
    if not _synonyms:
        return base
    seen: set[str] = set()
    variants: list[str] = []
    for token in _TOKEN_RE.findall(q.lower()):
        for variant in _synonyms.get(token, ()):
            key = variant.lower()
            if key in seen:
                continue
            seen.add(key)
            variants.append(variant)
            if len(variants) >= _MAX_VARIANTS:
                break
        if len(variants) >= _MAX_VARIANTS:
            break
    expr = base
    for variant in variants:
        # ``||`` is the tsquery OR operator (same as dual_tsquery uses).
        expr = expr.op("||")(dual_tsquery(variant))
    return expr
