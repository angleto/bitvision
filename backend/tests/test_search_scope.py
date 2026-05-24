"""Search scope filter tests.

Covers ``services.permissions.apply_scope_filter`` (the helper that
narrows a visibility-filtered query to 'public', 'mine', or leaves it
untouched on 'all'/None) and the ``is_opendata`` flag computed on
``StudyOut`` at serialisation.

Pure-Python unit tests: no DB required. We assemble a Select with
SQLAlchemy and inspect the WHERE clause after the helper runs to make
sure the additional restriction is in place.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from bvphoenix.api._schemas import StudyOut
from bvphoenix.db.models import ImagingStudy
from bvphoenix.services.permissions import apply_scope_filter, platform_owner_subject_id


def _fake_user(subject_id: uuid.UUID | None = None) -> object:
    """Lightweight User stand-in — apply_scope_filter only reads
    .subject_id, so a SimpleNamespace is enough and avoids the DB."""
    return SimpleNamespace(subject_id=subject_id or uuid.uuid4())


def test_scope_none_is_noop() -> None:
    base = select(ImagingStudy)
    out = apply_scope_filter(base, None, _fake_user())
    # compile to a string so we can search for the new clause without
    # depending on SQLAlchemy internal AST equality.
    assert str(out.compile()) == str(base.compile())


def test_scope_all_is_noop() -> None:
    base = select(ImagingStudy)
    out = apply_scope_filter(base, "all", _fake_user())
    assert str(out.compile()) == str(base.compile())


def test_scope_public_adds_is_public_or_platform_owner() -> None:
    base = select(ImagingStudy)
    out = apply_scope_filter(base, "public", _fake_user())
    sql = str(out.compile())
    assert "is_public" in sql
    # platform_owner_subject_id() value should appear bound somewhere.
    assert "owner_subject_id" in sql


def test_scope_mine_restricts_to_user() -> None:
    sid = uuid.uuid4()
    base = select(ImagingStudy)
    out = apply_scope_filter(base, "mine", _fake_user(subject_id=sid))
    sql = str(out.compile(compile_kwargs={"literal_binds": True}))
    # SQLAlchemy renders UUID literals as hex without dashes; check both
    # forms so a future Postgres dialect tweak does not break the test.
    assert sid.hex in sql or str(sid) in sql
    assert "owner_subject_id" in sql


def test_scope_mine_anonymous_matches_nothing() -> None:
    base = select(ImagingStudy)
    out = apply_scope_filter(base, "mine", None)
    sql = str(out.compile())
    # Anonymous on 'mine' returns a clause that matches no row — the
    # exact form (``id IS NULL``) is implementation detail; what matters
    # is the resulting filter is not empty.
    assert "imaging_studies.id IS NULL" in sql.replace("\n", " ")


def test_scope_unknown_value_is_noop() -> None:
    base = select(ImagingStudy)
    out = apply_scope_filter(base, "weird", _fake_user())
    assert str(out.compile()) == str(base.compile())


def test_studyout_is_opendata_true_for_platform_owner() -> None:
    fake = SimpleNamespace(
        id=uuid.uuid4(),
        study_instance_uid="1.2.3",
        owner_subject_id=platform_owner_subject_id(),
        patient_id=uuid.uuid4(),
        contribution_tier="t4",
        is_public=True,
        is_listed_for_sale=False,
        ingestion_complete=True,
        study_description="public test",
        study_date=None,
        modalities=["CT"],
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        source_collection="TCIA/LIDC-IDRI",
        license_spdx="CC-BY-3.0",
        license_url="https://creativecommons.org/licenses/by/3.0/",
        citation_required=True,
        citation_text="cite me",
    )
    out = StudyOut.model_validate(fake)
    assert out.is_opendata is True


def test_studyout_is_opendata_false_for_user_owned() -> None:
    fake = SimpleNamespace(
        id=uuid.uuid4(),
        study_instance_uid="1.2.4",
        owner_subject_id=uuid.uuid4(),  # NOT platform owner
        patient_id=uuid.uuid4(),
        contribution_tier="t1",
        is_public=False,
        is_listed_for_sale=False,
        ingestion_complete=True,
        study_description="private upload",
        study_date=None,
        modalities=["MR"],
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        source_collection=None,
        license_spdx=None,
        license_url=None,
        citation_required=False,
        citation_text=None,
    )
    out = StudyOut.model_validate(fake)
    assert out.is_opendata is False
