"""Pure unit tests for ``services.document_catalog_validation``.

The helpers gate the document-catalog write path: pre-validation
against the active vocabulary so a phantom kind id (the FE-only
``imaging_report`` / ``discharge_letter`` regression) gets rejected
with a structured 422 instead of bubbling up as an
``IntegrityError`` → 500. These tests cover the pure helpers without
a DB; the endpoint integration is exercised by the patient-API suite.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from bvphoenix.services.document_catalog_validation import (
    CatalogActiveIds,
    translate_catalog_fk_violation,
    validate_kind_id,
)


def _catalog(*kinds: str) -> CatalogActiveIds:
    return CatalogActiveIds(
        kinds=frozenset(kinds),
        provenances=frozenset(["digital_native_pdf", "manual_entry"]),
        authorities=frozenset(["original"]),
    )


def test_validate_kind_id_accepts_active() -> None:
    catalog = _catalog("radiology_report", "lab_result")
    assert validate_kind_id("radiology_report", catalog) is None


def test_validate_kind_id_rejects_phantom() -> None:
    catalog = _catalog("radiology_report", "lab_result")
    err = validate_kind_id("imaging_report", catalog)
    assert err is not None
    assert "imaging_report" in err
    assert "/api/document-catalog" in err


def test_validate_kind_id_none_is_noop() -> None:
    """``None`` means the field was not supplied; no validation needed."""
    catalog = _catalog("radiology_report")
    assert validate_kind_id(None, catalog) is None


def test_validate_kind_id_blank_is_noop() -> None:
    """Empty / whitespace strings are caught by the input-shape check
    in the API layer (400 invalid_kind_id with a different slug); the
    catalog membership helper short-circuits."""
    catalog = _catalog("radiology_report")
    assert validate_kind_id("", catalog) is None
    assert validate_kind_id("   ", catalog) is None


def _make_integrity_error(detail: str) -> IntegrityError:
    """Build an IntegrityError whose ``orig`` stringifies to ``detail``.

    Real instances carry a psycopg / asyncpg error object; for the
    pattern matcher we only care about ``str(exc.orig)``.
    """

    class _FakeOrig:
        def __init__(self, msg: str) -> None:
            self._msg = msg

        def __str__(self) -> str:
            return self._msg

    return IntegrityError("stmt", {}, _FakeOrig(detail))


def test_translate_kind_fk_violation() -> None:
    exc = _make_integrity_error(
        'insert or update on table "documents" violates foreign key constraint '
        '"documents_kind_id_fkey"\n'
        'DETAIL:  Key (kind_id)=(imaging_report) is not present in table "document_kinds".'
    )
    out = translate_catalog_fk_violation(exc)
    assert out is not None
    assert out.status_code == 422
    assert isinstance(out.detail, dict)
    assert out.detail["field"] == "kind_id"
    assert out.detail["catalog_table"] == "document_kinds"


def test_translate_provenance_fk_violation() -> None:
    exc = _make_integrity_error(
        'violates foreign key constraint "documents_provenance_id_fkey"\n'
        "DETAIL:  Key (provenance_id)=(unknown_source) is not present in table "
        '"document_provenances".'
    )
    out = translate_catalog_fk_violation(exc)
    assert out is not None
    assert isinstance(out.detail, dict)
    assert out.detail["field"] == "provenance_id"


def test_translate_authority_fk_violation() -> None:
    exc = _make_integrity_error(
        'violates foreign key constraint "documents_authority_id_fkey"\n'
        "DETAIL:  Key (authority_id)=(bogus) is not present in table "
        '"document_authorities".'
    )
    out = translate_catalog_fk_violation(exc)
    assert out is not None
    assert isinstance(out.detail, dict)
    assert out.detail["field"] == "authority_id"


def test_translate_unrelated_integrity_error_returns_none() -> None:
    """A unique-constraint or NOT NULL violation MUST bubble up so the
    caller's existing error mapping (or the global 500 handler) takes
    over — the helper only intercepts catalog FKs."""
    exc = _make_integrity_error(
        'duplicate key value violates unique constraint "patients_external_id_key"\n'
        "DETAIL:  Key (external_id)=(123) already exists."
    )
    assert translate_catalog_fk_violation(exc) is None
