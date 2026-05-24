"""Document taxonomy catalog API.

Single source of truth for the controlled vocabularies that back the
``documents`` table FK columns:

* ``kind_id``       → ``document_kinds``
* ``provenance_id`` → ``document_provenances``
* ``authority_id`` → ``document_authorities``

The frontend used to hard-code three parallel lists which drifted from
the DB seed (migration 0072): a user could pick a kind from the
dropdown that did not exist in ``document_kinds`` and the PATCH would
fail with a raw ``IntegrityError`` surfaced as 500. This endpoint is
the catalog any picker must consume so the UI cannot offer a value the
DB will reject.

The payload is locale-agnostic: ``display_name`` ships the full JSONB
i18n map (currently ``it`` + ``en``); the frontend resolves the
preferred locale at render time. Inactive rows are returned too so the
UI can label legacy documents whose kind has been retired without
losing the human-readable display.

Auth: ``optional_user`` — the catalog is a vocabulary, not PHI; any
authenticated session can read it. Anonymous callers get 401 when
``require_auth_globally`` is on (the default), preventing scraping.

Cache: server-side ``ETag`` from ``max(updated_at)`` across the three
tables, plus a short ``Cache-Control: max-age``. Catalog churn is
essentially zero outside migrations, so the round-trip stays cheap.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth import optional_user
from bvphoenix.db.models import User
from bvphoenix.db.models.document_catalog import (
    DocumentAuthority,
    DocumentKind,
    DocumentProvenance,
)
from bvphoenix.db.session import get_db

router = APIRouter(tags=["document-catalog"])


class DocumentKindOut(BaseModel):
    id: str
    display_name: dict[str, str]
    description: str | None = None
    is_active: bool
    sort_order: int
    loinc_code: str | None = None
    fhir_resource: str | None = None


class DocumentProvenanceOut(BaseModel):
    id: str
    display_name: dict[str, str]
    description: str | None = None
    is_active: bool
    sort_order: int
    is_digital: bool
    is_imaging: bool


class DocumentAuthorityOut(BaseModel):
    id: str
    display_name: dict[str, str]
    description: str | None = None
    is_active: bool
    sort_order: int
    trust_score: int


class DocumentCatalogOut(BaseModel):
    kinds: list[DocumentKindOut]
    provenances: list[DocumentProvenanceOut]
    authorities: list[DocumentAuthorityOut]


def _norm_display(d: Any) -> dict[str, str]:
    """Coerce a JSONB column value to a flat ``{locale: label}`` map.

    Catalog rows store this as a JSONB object with i18n keys; very old
    or partially-seeded rows may be NULL or empty. We always hand the
    frontend a dict so it can safely call ``[locale]`` with a fallback.
    """
    if not isinstance(d, dict):
        return {}
    return {str(k): str(v) for k, v in d.items() if isinstance(v, str)}


@router.get("/document-catalog", response_model=DocumentCatalogOut)
async def get_document_catalog(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User | None, Depends(optional_user)],
) -> DocumentCatalogOut:
    # Latest mutation across the three tables → ETag basis. ``COALESCE``
    # protects against an empty table (no rows means the catalog has
    # not been seeded yet, which is its own bug, but the endpoint
    # should still respond instead of 500-ing).
    latest = (
        await db.execute(
            select(
                func.greatest(
                    func.coalesce(
                        select(func.max(DocumentKind.updated_at)).scalar_subquery(),
                        func.now(),
                    ),
                    func.coalesce(
                        select(func.max(DocumentProvenance.updated_at)).scalar_subquery(),
                        func.now(),
                    ),
                    func.coalesce(
                        select(func.max(DocumentAuthority.updated_at)).scalar_subquery(),
                        func.now(),
                    ),
                )
            )
        )
    ).scalar_one()
    etag = f'"{int(latest.timestamp())}"' if latest else '"0"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})  # type: ignore[return-value]

    kinds = (
        (
            await db.execute(
                select(DocumentKind).order_by(
                    DocumentKind.sort_order.asc(),
                    DocumentKind.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    provenances = (
        (
            await db.execute(
                select(DocumentProvenance).order_by(
                    DocumentProvenance.sort_order.asc(),
                    DocumentProvenance.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    authorities = (
        (
            await db.execute(
                select(DocumentAuthority).order_by(
                    DocumentAuthority.sort_order.asc(),
                    DocumentAuthority.id.asc(),
                )
            )
        )
        .scalars()
        .all()
    )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=300"
    return DocumentCatalogOut(
        kinds=[
            DocumentKindOut(
                id=k.id,
                display_name=_norm_display(k.display_name),
                description=k.description,
                is_active=k.is_active,
                sort_order=k.sort_order,
                loinc_code=k.loinc_code,
                fhir_resource=k.fhir_resource,
            )
            for k in kinds
        ],
        provenances=[
            DocumentProvenanceOut(
                id=p.id,
                display_name=_norm_display(p.display_name),
                description=p.description,
                is_active=p.is_active,
                sort_order=p.sort_order,
                is_digital=p.is_digital,
                is_imaging=p.is_imaging,
            )
            for p in provenances
        ],
        authorities=[
            DocumentAuthorityOut(
                id=a.id,
                display_name=_norm_display(a.display_name),
                description=a.description,
                is_active=a.is_active,
                sort_order=a.sort_order,
                trust_score=a.trust_score,
            )
            for a in authorities
        ],
    )


__all__ = ["router"]
