"""Pure unit tests for the dataset-catalog citation + slug logic.

No DB: these exercise ``slugify``, the title/commercial-use derivation,
the DOI extraction, and the four citation renderers (text / bibtex /
ris / datacite) against a hand-built ``CollectionAggregate``. The
DB-backed aggregation SQL is covered separately in
``tests/integration/test_catalog_db.py``.
"""

from __future__ import annotations

import pytest

from bvphoenix.services import dataset_catalog as catalog

_BASE = "https://bitvision.example"


def _agg(**overrides) -> catalog.CollectionAggregate:
    base = {
        "collection": "TCIA/QIN-BREAST",
        "subjects": 10,
        "studies": 20,
        "series": 40,
        "instances": 5000,
        "modalities": ["MR", "CT"],
        "body_parts": ["BREAST"],
        "license_spdx": "CC-BY-3.0",
        "license_url": "https://creativecommons.org/licenses/by/3.0/",
        "citation_text": (
            "Huang W, et al. Data from QIN-Breast. The Cancer Imaging Archive. "
            "doi:10.7937/K9/TCIA.2014.A2N1IXOX"
        ),
        "citation_required": True,
        "first_published_year": 2021,
    }
    base.update(overrides)
    return catalog.CollectionAggregate(**base)


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("TCIA/QIN-BREAST", "tcia-qin-breast"),
        ("TCIA/HCC-TACE-Seg", "tcia-hcc-tace-seg"),
        ("IDC/midrc-ricord-1c", "idc-midrc-ricord-1c"),
        ("  Weird__Name!! ", "weird-name"),
        ("///", "dataset"),
    ],
)
def test_slugify(handle: str, expected: str) -> None:
    assert catalog.slugify(handle) == expected


def test_title_moves_archive_prefix_to_parenthetical() -> None:
    assert _agg().title == "QIN-BREAST (TCIA)"
    assert catalog.CollectionAggregate(collection="STANDALONE").title == "STANDALONE"


def test_pid_is_stable_local_identifier() -> None:
    assert _agg().pid == "bitvision:dataset:tcia-qin-breast"


@pytest.mark.parametrize(
    ("spdx", "allowed"),
    [
        ("CC-BY-4.0", True),
        ("CC-BY-3.0", True),
        ("CC-BY-NC-4.0", False),
        ("CC-BY-NC-SA-4.0", False),
        (None, False),
        ("", False),
    ],
)
def test_commercial_use_allowed(spdx: str | None, allowed: bool) -> None:
    assert catalog.commercial_use_allowed(spdx) is allowed


def test_extract_doi_from_citation() -> None:
    assert catalog._extract_doi("... doi:10.7937/K9/TCIA.2014.A2N1IXOX") == (
        "10.7937/K9/TCIA.2014.A2N1IXOX"
    )
    assert catalog._extract_doi("ends in a period 10.1038/s41597-022-01560-7.") == (
        "10.1038/s41597-022-01560-7"
    )
    assert catalog._extract_doi("no identifier here") is None
    assert catalog._extract_doi(None) is None


def test_landing_url_is_frontend_dataset_route() -> None:
    assert catalog.landing_url(_BASE, _agg()) == f"{_BASE}/datasets/tcia-qin-breast"
    # Trailing slash on the base is normalised.
    assert catalog.landing_url(_BASE + "/", _agg()) == f"{_BASE}/datasets/tcia-qin-breast"


def test_datacite_metadata_is_well_formed() -> None:
    md = catalog.build_datacite_metadata(_agg(), base_url=_BASE)
    assert md["types"]["resourceTypeGeneral"] == "Dataset"
    assert md["publisher"] == "bitvision OpenData"
    assert md["publicationYear"] == "2021"
    assert md["identifiers"][0]["identifier"] == "bitvision:dataset:tcia-qin-breast"
    # License surfaces as an SPDX rights entry.
    assert md["rightsList"][0]["rightsIdentifier"] == "CC-BY-3.0"
    assert md["rightsList"][0]["rightsIdentifierScheme"] == "SPDX"
    # Upstream DOI is back-linked as a derivation.
    related = md["relatedIdentifiers"][0]
    assert related["relationType"] == "IsDerivedFrom"
    assert related["relatedIdentifier"] == "10.7937/K9/TCIA.2014.A2N1IXOX"
    # Modalities + body parts become subject keywords.
    subjects = {s["subject"] for s in md["subjects"]}
    assert {"MR", "CT", "BREAST"} <= subjects
    # The upstream human citation is preserved verbatim in descriptions.
    assert any("QIN-Breast" in d["description"] for d in md["descriptions"])


def test_datacite_without_license_or_doi_omits_those_blocks() -> None:
    md = catalog.build_datacite_metadata(
        _agg(license_spdx=None, license_url=None, citation_text=None),
        base_url=_BASE,
    )
    assert "rightsList" not in md
    assert "relatedIdentifiers" not in md
    # Still valid: required core fields present.
    assert md["creators"] and md["titles"] and md["publicationYear"]


def test_citation_text_leads_with_upstream_then_redistribution() -> None:
    text = catalog.build_citation_text(_agg(), base_url=_BASE)
    assert text.startswith("Huang W")
    assert "Accessed via bitvision OpenData" in text
    assert "bitvision:dataset:tcia-qin-breast" in text
    assert "CC-BY-3.0" in text


def test_citation_text_synthesises_when_no_upstream_citation() -> None:
    text = catalog.build_citation_text(_agg(citation_text=None), base_url=_BASE)
    assert "QIN-BREAST (TCIA)" in text
    assert "bitvision OpenData" in text


def test_bibtex_is_parseable_misc_entry() -> None:
    bib = catalog.build_bibtex(_agg(), base_url=_BASE)
    assert bib.startswith("@misc{bitvision-tcia-qin-breast,")
    assert "title        = {QIN-BREAST (TCIA)}" in bib
    assert "url          = {https://bitvision.example/datasets/tcia-qin-breast}" in bib
    assert bib.rstrip().endswith("}")
    # Balanced braces — a citation with stray braces must not break it.
    assert bib.count("{") == bib.count("}")


def test_ris_has_data_type_and_doi() -> None:
    ris = catalog.build_ris(_agg(), base_url=_BASE)
    assert ris.startswith("TY  - DATA")
    assert "PB  - bitvision OpenData" in ris
    assert "DO  - 10.7937/K9/TCIA.2014.A2N1IXOX" in ris
    assert ris.splitlines()[-1] == "ER  - "
