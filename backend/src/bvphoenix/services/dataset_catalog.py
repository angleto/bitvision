"""Public dataset catalog — the citable, browsable commons surface.

This is the *read* side of the OpenData library.
``services.public_dataset`` ingests curated, already-de-identified,
CC-licensed DICOM collections (TCIA / IDC / OsiriX) and stamps each
``ImagingStudy`` with its upstream provenance (``source_collection``,
``license_spdx``, ``license_url``, ``citation_text``). This module
aggregates those rows back into TCIA/IDC-style *collections* and turns
the per-study provenance into a browsable, citable catalog.

Design contract:

* **Membership is purely public.** A collection is the set of studies
  with ``is_public = TRUE`` and a non-null ``source_collection``. The
  predicate is identical to ``visible_studies_filter(user=None)``
  AND-ed with "has provenance", so the catalog can never surface a row
  an anonymous visitor could not already read. The aggregation hard-codes
  ``is_public`` and never consults the caller — there is no code path
  here that could leak a private study even if called with an admin
  session.
* **Storage isolation.** Only aggregate counts and the provenance the
  importer recorded for attribution (license, citation) leave this
  module. No S3 keys, no patient identity, no per-instance data.
* **No DOI minting.** The persistent identifier is a stable *local* PID
  (``bitvision:dataset:<slug>``) plus a resolvable landing URL. A real
  DataCite DOI is a fast-follow that needs an external account; the
  ``datacite`` block here is faithful DataCite-4 *metadata* the catalog
  can hand to a minting step later without reshaping.

The slug is derived deterministically from ``source_collection`` and is
not stored: there are a handful of collections, so a slug lookup just
re-derives every collection's slug and matches. That keeps the schema
untouched (no migration) and the slug always consistent with the source
handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ImagingStudy, Series

# Cap the per-collection facet lists so a pathological collection cannot
# balloon the payload. Modalities are few by nature; body parts can be
# noisier, so we keep the top-N by frequency.
_MAX_BODY_PARTS = 24
_SAMPLE_STUDIES = 8


def slugify(value: str) -> str:
    """Stable, URL-safe slug for a ``source_collection`` handle.

    ``"TCIA/HCC-TACE-Seg"`` -> ``"tcia-hcc-tace-seg"``. Lowercased,
    every run of non-alphanumeric characters collapsed to a single
    hyphen, trimmed. Deterministic and dependency-free; the same handle
    always yields the same slug, which is what lets us avoid persisting
    it.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "dataset"


def _humanize_title(collection: str) -> str:
    """Human title from a ``source_collection`` handle.

    ``"TCIA/HCC-TACE-Seg"`` -> ``"HCC-TACE-Seg (TCIA)"``. The archive
    prefix (the part before the first ``/``) is moved into a trailing
    parenthetical so the dataset name leads. Handles with no prefix are
    returned verbatim.
    """
    archive, _, name = collection.partition("/")
    if name:
        return f"{name} ({archive})"
    return collection


def commercial_use_allowed(license_spdx: str | None) -> bool:
    """Whether the SPDX license permits commercial reuse.

    Conservative: an unknown / missing license is treated as *not*
    cleared for commercial use, and any NonCommercial CC variant
    (``...-NC...``) is excluded. This only labels the catalog entry; the
    authoritative gate stays the per-grant / per-study license flags.
    """
    if not license_spdx:
        return False
    return "-NC" not in license_spdx.upper()


@dataclass
class CollectionAggregate:
    """Everything the catalog knows about one public collection."""

    collection: str
    subjects: int = 0
    studies: int = 0
    series: int = 0
    instances: int = 0
    modalities: list[str] = field(default_factory=list)
    body_parts: list[str] = field(default_factory=list)
    license_spdx: str | None = None
    license_url: str | None = None
    citation_text: str | None = None
    citation_required: bool = False
    first_published_year: int | None = None

    @property
    def slug(self) -> str:
        return slugify(self.collection)

    @property
    def title(self) -> str:
        return _humanize_title(self.collection)

    @property
    def pid(self) -> str:
        """Stable local persistent identifier (DOI is a fast-follow)."""
        return f"bitvision:dataset:{self.slug}"

    @property
    def commercial_use_allowed(self) -> bool:
        return commercial_use_allowed(self.license_spdx)


@dataclass
class SampleStudy:
    id: str
    study_description: str | None
    study_date: str | None
    modalities: list[str]


# --- aggregation ----------------------------------------------------------
#
# The public-collection predicate, factored so every query shares the
# exact same membership rule. ``collection`` optionally narrows to one
# source handle (the detail / citation path) without changing semantics.


def _public_collection_studies(collection: str | None = None):
    stmt = select(ImagingStudy).where(
        ImagingStudy.is_public.is_(True),
        ImagingStudy.source_collection.is_not(None),
    )
    if collection is not None:
        stmt = stmt.where(ImagingStudy.source_collection == collection)
    return stmt.subquery()


async def aggregate_collections(
    db: AsyncSession, *, collection: str | None = None
) -> list[CollectionAggregate]:
    """Aggregate public studies into collections, newest-published first.

    Pass ``collection`` to compute a single collection (the detail path);
    omit it for the full catalog. Runs a handful of indexed grouped
    queries over the public subset — cheap for the commons-sized corpus.
    """
    pub = _public_collection_studies(collection)

    # Studies / subjects / publication year, grouped by collection.
    study_rows = (
        await db.execute(
            select(
                pub.c.source_collection,
                func.count(pub.c.id),
                func.count(func.distinct(pub.c.patient_id)),
                func.min(func.extract("year", pub.c.created_at)),
            ).group_by(pub.c.source_collection)
        )
    ).all()
    if not study_rows:
        return []

    aggregates: dict[str, CollectionAggregate] = {}
    for coll, studies, subjects, first_year in study_rows:
        aggregates[coll] = CollectionAggregate(
            collection=coll,
            studies=int(studies),
            subjects=int(subjects),
            first_published_year=int(first_year) if first_year is not None else None,
        )

    # License + citation, grouped with every distinct provenance tuple so
    # a collection whose rows disagree (shouldn't happen — the importer
    # writes it uniformly) resolves to the variant covering the most
    # studies rather than an arbitrary pick.
    lic_rows = (
        await db.execute(
            select(
                pub.c.source_collection,
                pub.c.license_spdx,
                pub.c.license_url,
                pub.c.citation_text,
                pub.c.citation_required,
                func.count(pub.c.id),
            ).group_by(
                pub.c.source_collection,
                pub.c.license_spdx,
                pub.c.license_url,
                pub.c.citation_text,
                pub.c.citation_required,
            )
        )
    ).all()
    dominant: dict[str, int] = {}
    for coll, spdx, url, citation, required, n in lic_rows:
        agg = aggregates.get(coll)
        if agg is None:
            continue
        n = int(n)
        if n <= dominant.get(coll, -1):
            continue
        dominant[coll] = n
        agg.license_spdx = spdx
        agg.license_url = url
        agg.citation_text = citation
        agg.citation_required = bool(required)

    # Series + instance counts, joined to the public studies.
    series_rows = (
        await db.execute(
            select(
                pub.c.source_collection,
                func.count(Series.id),
                func.coalesce(func.sum(Series.received_instance_count), 0),
            )
            .select_from(Series)
            .join(pub, Series.study_id == pub.c.id)
            .group_by(pub.c.source_collection)
        )
    ).all()
    for coll, series, instances in series_rows:
        agg = aggregates.get(coll)
        if agg is not None:
            agg.series = int(series)
            agg.instances = int(instances)

    # Modalities (study-level text[]), ordered by frequency.
    modality_col = func.unnest(pub.c.modalities).label("m")
    modality_rows = (
        await db.execute(
            select(pub.c.source_collection, modality_col, func.count())
            .group_by(pub.c.source_collection, modality_col)
            .order_by(func.count().desc())
        )
    ).all()
    for coll, modality, _ in modality_rows:
        agg = aggregates.get(coll)
        if agg is not None and modality and modality not in agg.modalities:
            agg.modalities.append(modality)

    # Body parts (series-level), top-N by frequency per collection.
    body_rows = (
        await db.execute(
            select(
                pub.c.source_collection,
                cast(Series.body_part_examined, String),
                func.count(),
            )
            .select_from(Series)
            .join(pub, Series.study_id == pub.c.id)
            .where(Series.body_part_examined.is_not(None))
            .group_by(pub.c.source_collection, Series.body_part_examined)
            .order_by(func.count().desc())
        )
    ).all()
    for coll, body_part, _ in body_rows:
        agg = aggregates.get(coll)
        if agg is None or not body_part:
            continue
        if len(agg.body_parts) < _MAX_BODY_PARTS and body_part not in agg.body_parts:
            agg.body_parts.append(body_part)

    # Newest-published first, then by size, then name for a stable order.
    return sorted(
        aggregates.values(),
        key=lambda a: (-(a.first_published_year or 0), -a.studies, a.collection),
    )


async def get_collection(db: AsyncSession, slug: str) -> CollectionAggregate | None:
    """Resolve one collection by slug, or ``None`` if no public match.

    Re-derives every collection's slug and matches — there are a handful
    of collections, so this is a single grouped query plus an in-Python
    lookup, and it keeps the slug authoritative without a stored column.
    """
    handles = (
        (
            await db.execute(
                select(ImagingStudy.source_collection)
                .where(
                    ImagingStudy.is_public.is_(True),
                    ImagingStudy.source_collection.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    match = next((h for h in handles if h and slugify(h) == slug), None)
    if match is None:
        return None
    aggregates = await aggregate_collections(db, collection=match)
    return aggregates[0] if aggregates else None


async def sample_studies(db: AsyncSession, collection: str) -> list[SampleStudy]:
    """A few representative studies for a collection's landing preview."""
    rows = (
        await db.execute(
            select(
                ImagingStudy.id,
                ImagingStudy.study_description,
                ImagingStudy.study_date,
                ImagingStudy.modalities,
            )
            .where(
                ImagingStudy.is_public.is_(True),
                ImagingStudy.source_collection == collection,
            )
            .order_by(ImagingStudy.study_date.desc().nullslast())
            .limit(_SAMPLE_STUDIES)
        )
    ).all()
    return [
        SampleStudy(
            id=str(sid),
            study_description=desc,
            study_date=sdate.isoformat() if sdate else None,
            modalities=list(mods or []),
        )
        for sid, desc, sdate, mods in rows
    ]


# --- citation -------------------------------------------------------------
#
# Three human-readable formats plus a DataCite-4 metadata block. The
# upstream ``citation_text`` is already a formatted academic citation
# (the importer copies it verbatim from the source archive), so we lead
# with it and add the redistribution provenance rather than re-deriving
# author lists we cannot reliably parse.

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


def _extract_doi(citation_text: str | None) -> str | None:
    if not citation_text:
        return None
    m = _DOI_RE.search(citation_text)
    if not m:
        return None
    # Trim trailing punctuation a sentence-final DOI commonly carries.
    return m.group(0).rstrip(".,);")


def landing_url(base_url: str, agg: CollectionAggregate) -> str:
    return f"{base_url.rstrip('/')}/datasets/{agg.slug}"


def _abstract(agg: CollectionAggregate) -> str:
    return (
        f"{agg.title}: a public DICOM imaging collection of "
        f"{agg.studies} studies ({agg.instances} images) across "
        f"{agg.subjects} subjects, redistributed via bitvision OpenData."
    )


def build_datacite_metadata(agg: CollectionAggregate, *, base_url: str) -> dict:
    """DataCite Metadata Schema 4 record (not a registered DOI).

    bitvision is the *publisher* of this redistribution mirror; the
    upstream source stays attributed through the human citation
    (``descriptions``) and, when the citation carries a DOI, a
    ``relatedIdentifiers`` ``IsDerivedFrom`` back-link.
    """
    pid = agg.pid
    year = agg.first_published_year or datetime.now(UTC).year
    rights: dict[str, str] = {}
    if agg.license_spdx:
        rights["rights"] = agg.license_spdx
        rights["rightsIdentifier"] = agg.license_spdx
        rights["rightsIdentifierScheme"] = "SPDX"
    if agg.license_url:
        rights["rightsUri"] = agg.license_url

    descriptions: list[dict] = [{"description": _abstract(agg), "descriptionType": "Abstract"}]
    if agg.citation_text:
        descriptions.append({"description": agg.citation_text, "descriptionType": "Other"})

    related: list[dict] = []
    source_doi = _extract_doi(agg.citation_text)
    if source_doi:
        related.append(
            {
                "relatedIdentifier": source_doi,
                "relatedIdentifierType": "DOI",
                "relationType": "IsDerivedFrom",
            }
        )

    subjects = [{"subject": m} for m in agg.modalities] + [{"subject": b} for b in agg.body_parts]

    record: dict = {
        "schemaVersion": "http://datacite.org/schema/kernel-4",
        "types": {
            "resourceTypeGeneral": "Dataset",
            "resourceType": "DICOM imaging collection",
        },
        "identifiers": [{"identifier": pid, "identifierType": "bitvision-PID"}],
        "creators": [{"name": agg.title, "nameType": "Organizational"}],
        "titles": [{"title": f"{agg.title} — bitvision OpenData mirror"}],
        "publisher": "bitvision OpenData",
        "publicationYear": str(year),
        "subjects": subjects,
        "sizes": [f"{agg.studies} studies", f"{agg.instances} images"],
        "formats": ["application/dicom"],
        "url": landing_url(base_url, agg),
        "descriptions": descriptions,
    }
    if rights:
        record["rightsList"] = [rights]
    if related:
        record["relatedIdentifiers"] = related
    return record


def build_citation_text(agg: CollectionAggregate, *, base_url: str) -> str:
    """Plain-text citation: upstream credit + redistribution provenance."""
    url = landing_url(base_url, agg)
    parts: list[str] = []
    if agg.citation_text:
        parts.append(agg.citation_text.rstrip("."))
    else:
        year = agg.first_published_year or datetime.now(UTC).year
        parts.append(f"{agg.title} [DICOM imaging collection]. bitvision OpenData, {year}")
    redistribution = f"Accessed via bitvision OpenData, {url} ({agg.pid})"
    if agg.license_spdx:
        redistribution += f". Licensed under {agg.license_spdx}"
    parts.append(redistribution)
    return ". ".join(parts) + "."


def build_bibtex(agg: CollectionAggregate, *, base_url: str) -> str:
    year = agg.first_published_year or datetime.now(UTC).year
    key = f"bitvision-{agg.slug}"
    lines = [
        f"@misc{{{key},",
        f"  title        = {{{agg.title}}},",
        "  howpublished = {bitvision OpenData},",
        f"  year         = {{{year}}},",
        f"  url          = {{{landing_url(base_url, agg)}}},",
    ]
    if agg.license_spdx:
        lines.append(f"  license      = {{{agg.license_spdx}}},")
    if agg.citation_text:
        # Brace-escape so a citation with special characters stays valid.
        note = agg.citation_text.replace("{", "(").replace("}", ")")
        lines.append(f"  note         = {{Source: {note}}},")
    lines.append("}")
    return "\n".join(lines)


def build_ris(agg: CollectionAggregate, *, base_url: str) -> str:
    year = agg.first_published_year or datetime.now(UTC).year
    lines = [
        "TY  - DATA",
        f"TI  - {agg.title}",
        "PB  - bitvision OpenData",
        f"PY  - {year}",
        f"UR  - {landing_url(base_url, agg)}",
    ]
    if agg.license_spdx:
        lines.append(f"C1  - License: {agg.license_spdx}")
    if agg.citation_text:
        lines.append(f"N1  - Source: {agg.citation_text}")
    source_doi = _extract_doi(agg.citation_text)
    if source_doi:
        lines.append(f"DO  - {source_doi}")
    lines.append("ER  - ")
    return "\n".join(lines)


CITATION_FORMATS = ("text", "bibtex", "ris", "datacite")
