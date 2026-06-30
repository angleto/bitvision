"""Public dataset catalog API — browsable, citable OpenData commons.

The read surface over the OpenData library: it turns the per-study
provenance the importer (``services.public_dataset``) stamps onto public
studies into TCIA/IDC-style *collections* a visitor can browse and cite.

Three endpoints, all public-by-design (anonymous OK, no auth dep, like
``/api/transparency``) and rate-limited to keep a scraper from hammering
them:

* ``GET /api/catalog/collections`` — every public collection with its
  aggregate counts, modalities, license, and citation requirement.
* ``GET /api/catalog/collections/{slug}`` — one collection in full,
  including a DataCite-4 metadata block, the upstream citation, the
  stable local PID, and a few sample studies for the landing preview.
* ``GET /api/catalog/collections/{slug}/citation?format=...`` — the
  citation in ``text`` / ``bibtex`` / ``ris`` / ``datacite`` form,
  ready to paste into a paper or reference manager.

Storage isolation holds throughout: only aggregate counts and the
attribution metadata leave the service. The aggregation hard-codes
``is_public`` and never consults a caller identity, so there is no path
here that could surface a private study. Counts are cheap indexed
aggregates over the commons-sized public subset; a short ``Cache-Control``
lets browsers and any CDN absorb repeat views.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix import __version__
from bvphoenix.config import get_settings
from bvphoenix.db.session import get_db
from bvphoenix.services import dataset_catalog as catalog
from bvphoenix.services.rate_limit import SEARCH_LIMIT, limiter

router = APIRouter(prefix="/catalog", tags=["catalog"])

_CACHE_CONTROL = "public, max-age=300"

_CITATION_MEDIA_TYPES = {
    "text": "text/plain; charset=utf-8",
    "bibtex": "application/x-bibtex; charset=utf-8",
    "ris": "application/x-research-info-systems; charset=utf-8",
}


class CollectionSummary(BaseModel):
    slug: str = Field(description="Stable URL slug derived from the source collection handle.")
    pid: str = Field(description="Stable local persistent identifier (bitvision:dataset:<slug>).")
    collection: str = Field(
        description="Upstream source-collection handle, e.g. 'TCIA/QIN-BREAST'."
    )
    title: str
    subjects: int
    studies: int
    series: int
    instances: int
    modalities: list[str]
    body_parts: list[str]
    license_spdx: str | None
    license_url: str | None
    citation_required: bool
    commercial_use_allowed: bool
    first_published_year: int | None


class CatalogTotals(BaseModel):
    collections: int
    subjects: int
    studies: int
    series: int
    instances: int


class CollectionListOut(BaseModel):
    generated_at: str
    version: str
    totals: CatalogTotals
    collections: list[CollectionSummary]


class SampleStudyOut(BaseModel):
    id: str
    study_description: str | None
    study_date: str | None
    modalities: list[str]


class CollectionDetailOut(CollectionSummary):
    landing_url: str
    citation_text: str | None = Field(
        description="Upstream academic citation copied verbatim at import time, if any.",
    )
    datacite: dict = Field(description="DataCite Metadata Schema 4 record (not a registered DOI).")
    sample_studies: list[SampleStudyOut]


def _summary(agg: catalog.CollectionAggregate) -> CollectionSummary:
    return CollectionSummary(
        slug=agg.slug,
        pid=agg.pid,
        collection=agg.collection,
        title=agg.title,
        subjects=agg.subjects,
        studies=agg.studies,
        series=agg.series,
        instances=agg.instances,
        modalities=agg.modalities,
        body_parts=agg.body_parts,
        license_spdx=agg.license_spdx,
        license_url=agg.license_url,
        citation_required=agg.citation_required,
        commercial_use_allowed=agg.commercial_use_allowed,
        first_published_year=agg.first_published_year,
    )


@router.get(
    "/collections",
    response_model=CollectionListOut,
    summary="List public OpenData collections",
)
@limiter.limit(SEARCH_LIMIT)
async def list_collections(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CollectionListOut:
    """Browse the public dataset commons, newest-published first.

    No auth required and no per-study or per-patient data is exposed —
    only aggregate counts and the attribution metadata (license,
    citation requirement) recorded at import time.
    """
    aggregates = await catalog.aggregate_collections(db)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    totals = CatalogTotals(
        collections=len(aggregates),
        subjects=sum(a.subjects for a in aggregates),
        studies=sum(a.studies for a in aggregates),
        series=sum(a.series for a in aggregates),
        instances=sum(a.instances for a in aggregates),
    )
    return CollectionListOut(
        generated_at=datetime.now(UTC).isoformat(),
        version=__version__,
        totals=totals,
        collections=[_summary(a) for a in aggregates],
    )


@router.get(
    "/collections/{slug}",
    response_model=CollectionDetailOut,
    summary="One public collection in full (with DataCite metadata)",
)
@limiter.limit(SEARCH_LIMIT)
async def get_collection(
    request: Request,
    response: Response,
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CollectionDetailOut:
    agg = await catalog.get_collection(db, slug)
    if agg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection not found")
    base_url = get_settings().public_frontend_url
    samples = await catalog.sample_studies(db, agg.collection)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    summary = _summary(agg)
    return CollectionDetailOut(
        **summary.model_dump(),
        landing_url=catalog.landing_url(base_url, agg),
        citation_text=agg.citation_text,
        datacite=catalog.build_datacite_metadata(agg, base_url=base_url),
        sample_studies=[
            SampleStudyOut(
                id=s.id,
                study_description=s.study_description,
                study_date=s.study_date,
                modalities=s.modalities,
            )
            for s in samples
        ],
    )


@router.get(
    "/collections/{slug}/citation",
    summary="Citation for a collection (text / bibtex / ris / datacite)",
)
@limiter.limit(SEARCH_LIMIT)
async def get_collection_citation(
    request: Request,
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    format: Literal["text", "bibtex", "ris", "datacite"] = "text",
) -> Response:
    """Return one collection's citation in the requested format.

    ``datacite`` returns the DataCite-4 metadata as JSON; the other
    formats return plain text with a sensible download filename so a
    reference manager can import them directly.
    """
    agg = await catalog.get_collection(db, slug)
    if agg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="collection not found")
    base_url = get_settings().public_frontend_url

    if format == "datacite":
        return JSONResponse(
            content=catalog.build_datacite_metadata(agg, base_url=base_url),
            headers={"Cache-Control": _CACHE_CONTROL},
        )

    if format == "bibtex":
        body = catalog.build_bibtex(agg, base_url=base_url)
        filename = f"{agg.slug}.bib"
    elif format == "ris":
        body = catalog.build_ris(agg, base_url=base_url)
        filename = f"{agg.slug}.ris"
    else:
        body = catalog.build_citation_text(agg, base_url=base_url)
        filename = f"{agg.slug}.txt"

    return PlainTextResponse(
        body,
        media_type=_CITATION_MEDIA_TYPES[format],
        headers={
            "Cache-Control": _CACHE_CONTROL,
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )
