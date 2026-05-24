"""Sanity check that every model registers on Base.metadata.

Catches typos like "a model file exists but nothing imports it" before
they turn into a silent autogenerate that drops a table.
"""

from bvphoenix.db import models
from bvphoenix.db.base import Base

EXPECTED_TABLES = {
    "subjects",
    "users",
    "organizations",
    "groups",
    "memberships",
    "patients",
    "imaging_studies",
    "series",
    "instances",
    "derivatives",
    "email_verification_tokens",
}


def test_expected_tables_registered() -> None:
    missing = EXPECTED_TABLES - set(Base.metadata.tables.keys())
    assert not missing, f"missing tables on metadata: {missing}"


def test_study_has_owner_fk() -> None:
    study = Base.metadata.tables["imaging_studies"]
    fks = {fk.column.table.name for fk in study.foreign_keys}
    assert "subjects" in fks
    assert "patients" in fks


def test_series_cascades_from_study() -> None:
    series = Base.metadata.tables["series"]
    study_fk = next(fk for fk in series.foreign_keys if fk.column.table.name == "imaging_studies")
    assert study_fk.ondelete == "CASCADE"
