"""Shared full-text-search expression helpers (dual-config).

The corpus is Italian-first but laced with English radiology acronyms
and DICOM code-strings, so a *single* text-search config loses recall
either way: ``italian`` stems away exact tokens like "T2 FLAIR", while
``simple`` never matches "polmoni" against "polmone". Every FTS site
therefore uses a **dual** config — the Italian-stemmed lexemes OR'd with
the raw ``simple`` tokens — on both the document and the query side, so
the two always speak the same two languages.

For studies/series the dual *vector* is materialised as a generated,
GIN-indexed column (``ImagingStudy.study_description_tsv`` /
``Series.series_description_tsv``) so the index is usable. For the
patient-scoped document / report_content sites (small per-patient row
sets, no dedicated index) :func:`dual_tsvector` builds the identical
expression inline. Queries always go through :func:`dual_tsquery`.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

__all__ = [
    "FTS_FALLBACK_CONFIG",
    "FTS_PRIMARY_CONFIG",
    "dual_tsquery",
    "dual_tsvector",
]

# Primary: stemmed + stopworded for the dominant prose language.
FTS_PRIMARY_CONFIG = "italian"
# Fallback: no stemming/stopwords, so acronyms and code-strings survive.
FTS_FALLBACK_CONFIG = "simple"


def dual_tsquery(q: str) -> ColumnElement:
    """``plainto_tsquery('italian', q) || plainto_tsquery('simple', q)``.

    The ``||`` is the tsquery OR operator: a row matches if either the
    stemmed-Italian or the raw-token reading of the query hits.
    """
    return func.plainto_tsquery(FTS_PRIMARY_CONFIG, q).op("||")(
        func.plainto_tsquery(FTS_FALLBACK_CONFIG, q)
    )


def dual_tsvector(text_col: ColumnElement | str) -> ColumnElement:
    """Inline dual-config tsvector for a column lacking a generated one.

    Matches the generated-column expression on studies/series so the two
    sides of a search rank consistently. NULL-safe via ``coalesce``.
    """
    return func.to_tsvector(FTS_PRIMARY_CONFIG, func.coalesce(text_col, "")).op("||")(
        func.to_tsvector(FTS_FALLBACK_CONFIG, func.coalesce(text_col, ""))
    )
