"""Catalog API wiring — routing, schemas, citation dispatch, 404, caching.

The DB is stubbed and the aggregation is replaced with deterministic
fakes (the SQL itself is covered in tests/integration/test_catalog_db.py),
so this exercises the FastAPI router, the Pydantic payloads, the
citation-format content negotiation, the not-found path, and the public
``Cache-Control`` header without a live Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.db.session import get_db
from bvphoenix.main import app
from bvphoenix.services import dataset_catalog as catalog


class _StubSession:
    async def execute(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
        raise AssertionError("DB should be bypassed in these tests")

    async def close(self) -> None:
        return None


async def _override_get_db() -> AsyncIterator[_StubSession]:
    yield _StubSession()


def _agg(collection: str, **overrides) -> catalog.CollectionAggregate:
    base = {
        "collection": collection,
        "subjects": 3,
        "studies": 7,
        "series": 14,
        "instances": 900,
        "modalities": ["CT", "SEG"],
        "body_parts": ["LIVER"],
        "license_spdx": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "citation_text": "Some A, et al. doi:10.7937/TCIA.ABCD-1234",
        "citation_required": True,
        "first_published_year": 2026,
    }
    base.update(overrides)
    return catalog.CollectionAggregate(**base)


@pytest.fixture(autouse=True)
def _stub_db() -> Iterator[None]:
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _stub_aggregation(monkeypatch: pytest.MonkeyPatch) -> None:
    aggs = [
        _agg("TCIA/HCC-TACE-Seg", studies=210, subjects=105, instances=50000),
        _agg("TCIA/QIN-BREAST", studies=20, subjects=10, instances=5000),
    ]

    async def fake_aggregate(_db: Any, *, collection: str | None = None):
        if collection is None:
            return aggs
        return [a for a in aggs if a.collection == collection]

    async def fake_get(_db: Any, slug: str):
        return next((a for a in aggs if a.slug == slug), None)

    async def fake_samples(_db: Any, collection: str):
        return [
            catalog.SampleStudy(
                id="00000000-0000-0000-0000-000000000001",
                study_description="CT LIVER",
                study_date="2006-07-08",
                modalities=["CT"],
            )
        ]

    monkeypatch.setattr(catalog, "aggregate_collections", fake_aggregate)
    monkeypatch.setattr(catalog, "get_collection", fake_get)
    monkeypatch.setattr(catalog, "sample_studies", fake_samples)


client = TestClient(app)


def test_list_collections_shape_and_totals() -> None:
    r = client.get("/api/catalog/collections")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["collections"] == 2
    assert body["totals"]["studies"] == 230
    assert body["totals"]["subjects"] == 115
    assert body["totals"]["instances"] == 55000
    slugs = [c["slug"] for c in body["collections"]]
    assert slugs == ["tcia-hcc-tace-seg", "tcia-qin-breast"]
    first = body["collections"][0]
    assert first["pid"] == "bitvision:dataset:tcia-hcc-tace-seg"
    assert first["commercial_use_allowed"] is True
    assert first["license_spdx"] == "CC-BY-4.0"


def test_list_collections_sets_public_cache_control() -> None:
    r = client.get("/api/catalog/collections")
    assert r.headers["Cache-Control"] == "public, max-age=300"


def test_list_is_anonymous() -> None:
    # No Authorization header — the commons is public by design.
    assert client.get("/api/catalog/collections").status_code == 200


def test_collection_detail_includes_datacite_and_samples() -> None:
    r = client.get("/api/catalog/collections/tcia-qin-breast")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "tcia-qin-breast"
    assert body["landing_url"].endswith("/datasets/tcia-qin-breast")
    assert body["datacite"]["types"]["resourceTypeGeneral"] == "Dataset"
    assert body["datacite"]["publisher"] == "bitvision OpenData"
    assert body["citation_text"].startswith("Some A")
    assert len(body["sample_studies"]) == 1
    assert body["sample_studies"][0]["study_description"] == "CT LIVER"


def test_collection_detail_unknown_slug_is_404() -> None:
    r = client.get("/api/catalog/collections/does-not-exist")
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    ("fmt", "content_type", "needle"),
    [
        ("text", "text/plain", "Accessed via bitvision OpenData"),
        ("bibtex", "application/x-bibtex", "@misc{bitvision-tcia-qin-breast,"),
        ("ris", "application/x-research-info-systems", "TY  - DATA"),
    ],
)
def test_citation_text_formats(fmt: str, content_type: str, needle: str) -> None:
    r = client.get(f"/api/catalog/collections/tcia-qin-breast/citation?format={fmt}")
    assert r.status_code == 200, r.text
    assert content_type in r.headers["content-type"]
    assert needle in r.text
    assert "filename=" in r.headers.get("content-disposition", "")


def test_citation_datacite_returns_json() -> None:
    r = client.get("/api/catalog/collections/tcia-qin-breast/citation?format=datacite")
    assert r.status_code == 200, r.text
    assert "application/json" in r.headers["content-type"]
    body = r.json()
    assert body["identifiers"][0]["identifier"] == "bitvision:dataset:tcia-qin-breast"
    assert body["relatedIdentifiers"][0]["relatedIdentifier"] == "10.7937/TCIA.ABCD-1234"


def test_citation_unknown_slug_is_404() -> None:
    r = client.get("/api/catalog/collections/nope/citation?format=text")
    assert r.status_code == 404, r.text


def test_citation_rejects_unknown_format() -> None:
    # Literal[...] query type → 422 from FastAPI validation.
    r = client.get("/api/catalog/collections/tcia-qin-breast/citation?format=xml")
    assert r.status_code == 422, r.text
