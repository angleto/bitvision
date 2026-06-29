"""PHR-Bundle format conformance gate.

The PHR-Bundle (docs/phr-bundle.md) is the patient's portable, versioned
health-record container — the concrete form of "the patient owns the
data". This suite pins the published JSON Schema to the builder so the
open spec and the code can never drift silently:

* DB-free (runs in the PR gate): the schema is itself valid, its
  ``format`` / ``schema_version`` ``const`` declarations equal the
  backend constants, the checked-in example validates, and the two
  serializers that the legacy ``Report`` dead-symbol bug silently broke
  (``_serialize_report`` / ``_serialize_document``) produce
  schema-conformant entries from in-memory ORM rows.
* DB-backed (tagged-release gate): the real ``build_gdpr_bundle`` output
  validates against the schema end-to-end.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from bvphoenix.db.models import Consent, Document, ReportContent
from bvphoenix.services.gdpr_export import (
    GDPR_EXPORT_SCHEMA_VERSION,
    PHR_BUNDLE_FORMAT,
    _serialize_document,
    _serialize_report,
    build_gdpr_bundle,
)
from tests.conftest import skip_if_no_db

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "schemas"
_SCHEMA_PATH = _DOCS / "phr-bundle.v1.schema.json"
_EXAMPLE_PATH = _DOCS / "phr-bundle.v1.example.json"


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _item_validator(section: str) -> Draft202012Validator:
    """A validator for a single entry of one array section."""
    return Draft202012Validator(_schema()["properties"][section]["items"])


# --------------------------------------------------------------------------- #
# DB-free contract gates (run on every PR)
# --------------------------------------------------------------------------- #


def test_schema_is_itself_valid() -> None:
    Draft202012Validator.check_schema(_schema())


def test_format_and_version_consts_match_code() -> None:
    """The schema's pinned ``const`` values must equal the backend
    constants. If someone bumps GDPR_EXPORT_SCHEMA_VERSION without
    shipping a new schema (or vice versa), this fails."""
    props = _schema()["properties"]
    assert props["format"]["const"] == PHR_BUNDLE_FORMAT
    assert props["schema_version"]["const"] == GDPR_EXPORT_SCHEMA_VERSION


def test_published_example_validates() -> None:
    Draft202012Validator(_schema()).validate(json.loads(_EXAMPLE_PATH.read_text()))


def test_serialize_report_conforms() -> None:
    """The exact serialization the dead-symbol bug broke, exercised
    without a DB by projecting an in-memory ORM row."""
    r = ReportContent()
    r.id = uuid.uuid4()
    r.clinical_event_id = uuid.uuid4()
    r.authority_id = "canonical_synthesis"
    r.status = "draft"
    r.title = "CT chest summary"
    r.narrative_md = "No acute findings."
    r.author_kind = "agent"
    r.model_id = "claude-opus-4-8"
    r.provider = "anthropic"
    r.created_at = datetime.now(UTC)

    entry = _serialize_report(r)
    _item_validator("reports").validate(entry)
    assert entry["author_kind"] == "agent"  # AI provenance preserved


def test_serialize_document_conforms() -> None:
    d = Document()
    d.id = uuid.uuid4()
    d.patient_id = uuid.uuid4()
    d.kind_id = "referto"
    d.authority_id = "original"
    d.title = "Radiology report"
    d.text = "Esame TC del torace..."
    d.file_content_type = "application/pdf"
    d.content_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    d.document_date = None
    d.deleted_at = None
    d.created_at = datetime.now(UTC)

    entry = _serialize_document(d)
    _item_validator("patient_documents").validate(entry)
    assert entry["document_kind"] == "referto"


def test_serialize_document_lists_soft_deleted() -> None:
    """A soft-deleted-but-not-purged document is still held, so the
    honest export lists it with ``deleted_at`` set."""
    d = Document()
    d.id = uuid.uuid4()
    d.patient_id = uuid.uuid4()
    d.kind_id = "unclassified"
    d.authority_id = "original"
    d.title = "old scan"
    d.text = None
    d.file_content_type = None
    d.content_sha256 = None
    d.document_date = None
    d.deleted_at = datetime.now(UTC)
    d.created_at = datetime.now(UTC)

    entry = _serialize_document(d)
    _item_validator("patient_documents").validate(entry)
    assert entry["deleted_at"] is not None


# --------------------------------------------------------------------------- #
# DB-backed end-to-end gate (tagged release)
# --------------------------------------------------------------------------- #


@skip_if_no_db
@pytest.mark.asyncio
async def test_build_gdpr_bundle_conforms(db_session, make_user, make_study) -> None:
    """The real builder output validates against the published schema."""
    user = await make_user()
    await make_study(user, description="CT CHEST")
    db_session.add(Consent(user_subject_id=user.subject_id, kind="research_use"))
    await db_session.flush()

    bundle = await build_gdpr_bundle(db_session, user)

    # Envelope is self-identifying and the sections are present.
    assert bundle["format"] == PHR_BUNDLE_FORMAT
    assert bundle["schema_version"] == GDPR_EXPORT_SCHEMA_VERSION
    assert len(bundle["studies"]) == 1
    assert len(bundle["patients"]) == 1
    assert any(c["kind"] == "research_use" for c in bundle["consents"])

    Draft202012Validator(_schema()).validate(bundle)
