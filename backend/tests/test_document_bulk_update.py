"""Pure unit tests for ``services.document_bulk_update``.

These exercise the diff helper and the input shape; the DB-backed
acceptance tests live alongside the patient API integration suite.
"""

from __future__ import annotations

import uuid
from datetime import date

from bvphoenix.services.document_bulk_update import (
    BulkUpdateItem,
    _diff_doc,
    _validate_kind,
)


class _Doc:
    """Minimal stub of :class:`PatientDocument` for the diff helper."""

    def __init__(
        self,
        *,
        title: str = "old",
        document_type: str = "lab_result",
        document_date: date | None = None,
        text: str | None = None,
    ) -> None:
        self.title = title
        self.document_type = document_type
        self.document_date = document_date
        self.text = text


def _doc_uuid() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_diff_only_supplied_fields() -> None:
    doc = _Doc(title="old", text="old text")
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        title="new",
        fields_set=frozenset({"title"}),
    )
    diff = _diff_doc(doc, item)
    assert diff == {"title": {"before": "old", "after": "new"}}


def test_diff_skips_unchanged_field() -> None:
    doc = _Doc(title="same")
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        title="same",
        fields_set=frozenset({"title"}),
    )
    assert _diff_doc(doc, item) == {}


def test_diff_normalises_empty_text_to_none() -> None:
    doc = _Doc(text="something")
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        text="",
        fields_set=frozenset({"text"}),
    )
    diff = _diff_doc(doc, item)
    assert diff == {"text": {"before": "something", "after": None}}


def test_diff_handles_document_date_iso() -> None:
    doc = _Doc(document_date=date(2024, 1, 12))
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        document_date=date(2024, 2, 1),
        fields_set=frozenset({"document_date"}),
    )
    diff = _diff_doc(doc, item)
    assert diff == {"document_date": {"before": "2024-01-12", "after": "2024-02-01"}}


def test_validate_kind_rejects_empty() -> None:
    """v3: validation of unknown ids is delegated to the FK on
    documents.kind_id (catalog-driven). The application-side check
    only catches obvious shape errors — empty / whitespace strings.
    Unknown ids surface as a 422 from the DB layer instead of a
    pre-flight validation error."""
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        document_type="   ",  # whitespace-only
        fields_set=frozenset({"document_type"}),
    )
    err = _validate_kind(item)
    assert err is not None
    assert "empty" in err.lower()


def test_validate_kind_accepts_allowed() -> None:
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        document_type="lab_result",
        fields_set=frozenset({"document_type"}),
    )
    assert _validate_kind(item) is None


def test_validate_kind_skips_when_not_supplied() -> None:
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        title="new",
        fields_set=frozenset({"title"}),
    )
    assert _validate_kind(item) is None


def test_validate_kind_rejects_phantom_when_catalog_supplied() -> None:
    """When ``apply_bulk_update`` passes the active-catalog snapshot,
    ``_validate_kind`` rejects ids absent from the seeded
    ``document_kinds`` rows. This is the pre-flight check that turns
    the original 500 into a structured 422 before the FK on flush.
    """
    from bvphoenix.services.document_catalog_validation import CatalogActiveIds

    catalog = CatalogActiveIds(
        kinds=frozenset({"radiology_report", "lab_result"}),
        provenances=frozenset({"manual_entry"}),
        authorities=frozenset({"original"}),
    )
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        document_type="imaging_report",  # FE-only phantom that fails the FK
        fields_set=frozenset({"document_type"}),
    )
    err = _validate_kind(item, catalog)
    assert err is not None
    assert "imaging_report" in err


def test_validate_kind_collapses_kind_id_over_document_type() -> None:
    from bvphoenix.services.document_catalog_validation import CatalogActiveIds

    catalog = CatalogActiveIds(
        kinds=frozenset({"radiology_report"}),
        provenances=frozenset(),
        authorities=frozenset(),
    )
    # ``kind_id`` wins on collision; the alias ``document_type`` value is
    # ignored when ``kind_id`` is also supplied.
    item = BulkUpdateItem(
        document_id=_doc_uuid(),
        document_type="lab_result",
        kind_id="radiology_report",
        fields_set=frozenset({"document_type", "kind_id"}),
    )
    assert _validate_kind(item, catalog) is None
