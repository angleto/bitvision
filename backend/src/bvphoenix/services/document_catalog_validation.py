"""Translate FK violations on the document catalog columns into 422.

The ``documents`` row has three FK columns into the controlled-vocab
catalog tables (``kind_id`` → ``document_kinds``, ``provenance_id`` →
``document_provenances``, ``authority_id`` → ``document_authorities``).
A PATCH that sets one of these to a value not in the catalog raises a
Postgres ``foreign_key_violation`` which surfaces as a SQLAlchemy
``IntegrityError``. Without explicit handling FastAPI's default
exception path emits a 500 — opaque to agents and to the FE form, and
the user has no actionable feedback.

This helper inspects the error and, when it matches one of the three
catalog FKs, builds a structured ``problem(422, ...)`` carrying the
slug + the offending field. Other ``IntegrityError`` instances (unique
violations, NOT NULL on title, etc.) return ``None`` so the caller can
re-raise and let the global handler take over.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from bvphoenix.db.models.document_catalog import (
    DocumentAuthority,
    DocumentKind,
    DocumentProvenance,
)
from bvphoenix.middleware.problem_details import problem


@dataclass(slots=True, frozen=True)
class CatalogActiveIds:
    """Cached snapshot of the active IDs across the three catalog tables.

    Held for the duration of one PATCH / bulk PATCH request so the
    pre-validation pass below can check membership in O(1) without
    re-querying per item. Inactive rows are intentionally excluded:
    a kind that has been soft-deleted via ``is_active=False`` should
    not be selectable on the write side, even though existing
    documents that still reference it stay readable.
    """

    kinds: frozenset[str]
    provenances: frozenset[str]
    authorities: frozenset[str]


async def load_active_catalog_ids(db: AsyncSession) -> CatalogActiveIds:
    """Read the active IDs from the three catalog tables in one round-trip."""
    kinds = (
        (await db.execute(select(DocumentKind.id).where(DocumentKind.is_active.is_(True))))
        .scalars()
        .all()
    )
    provenances = (
        (
            await db.execute(
                select(DocumentProvenance.id).where(DocumentProvenance.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    authorities = (
        (
            await db.execute(
                select(DocumentAuthority.id).where(DocumentAuthority.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    return CatalogActiveIds(
        kinds=frozenset(kinds),
        provenances=frozenset(provenances),
        authorities=frozenset(authorities),
    )


def validate_kind_id(value: str | None, catalog: CatalogActiveIds) -> str | None:
    """Return an error message if ``value`` is not a valid active kind.

    ``None`` is a no-op (the field was not supplied). Empty / whitespace
    strings are rejected separately by the existing input-shape check;
    here we only catch the "value missing from catalog" case.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v not in catalog.kinds:
        return (
            f"kind_id {v!r} is not a known active document kind. "
            "Fetch the live list from GET /api/document-catalog."
        )
    return None


def translate_catalog_fk_violation(exc: IntegrityError) -> StarletteHTTPException | None:
    """Return a 422 ``problem`` if ``exc`` is a catalog FK violation.

    Detection works on either the constraint name (``documents_<col>_fkey``,
    the auto-generated Postgres name for the FKs declared in
    ``Document``) or, defensively, on the column-name substring in the
    ``DETAIL`` line. Both paths cover psycopg / asyncpg drivers and
    survive future renames as long as the column name stays.
    """
    raw = str(exc.orig) if exc.orig is not None else str(exc)
    lower = raw.lower()
    # Order matters: column-name substrings can appear in unrelated
    # error messages, so we check the constraint name first.
    if "documents_kind_id_fkey" in lower or "(kind_id)" in lower:
        slug, field, table = "invalid_kind_id", "kind_id", "document_kinds"
    elif "documents_provenance_id_fkey" in lower or "(provenance_id)" in lower:
        slug, field, table = "invalid_provenance_id", "provenance_id", "document_provenances"
    elif "documents_authority_id_fkey" in lower or "(authority_id)" in lower:
        slug, field, table = "invalid_authority_id", "authority_id", "document_authorities"
    else:
        return None
    return problem(
        422,
        slug,
        f"{field} value is not present in {table}; "
        "fetch the live list from GET /api/document-catalog",
        extra={"field": field, "catalog_table": table},
    )


__all__ = ["translate_catalog_fk_violation"]
