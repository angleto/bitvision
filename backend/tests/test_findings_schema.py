"""Pure-unit tests for the Finding schema + vocab-seed consistency.

No database: these guard the static contracts of P2 (the vocab seed in
migration 0020 stays consistent with the model constants, and the API
schemas validate as intended). DB-backed CRUD / structured-query tests
run in CI against a migrated Postgres.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from pydantic import ValidationError

from bvphoenix.api.findings import FindingCreateIn, GeometryRefIn
from bvphoenix.db.models.findings import (
    FINDING_STATUSES,
    FINDING_TYPE_CATEGORIES,
    LATERALITIES,
)


def _load_migration():
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0020_findings_entity.py"
    )
    spec = importlib.util.spec_from_file_location("m0020", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_down_revision_chains_to_marker_tracking() -> None:
    mig = _load_migration()
    assert mig.down_revision == "0019_marker_tracking"
    assert mig.revision == "0020_findings_entity"


def test_seed_finding_types_categories_valid_and_unique() -> None:
    mig = _load_migration()
    keys = [k for (k, _d, _c) in mig._FINDING_TYPES]
    cats = {c for (_k, _d, c) in mig._FINDING_TYPES}
    assert len(keys) == len(set(keys)), "duplicate finding_type seed keys"
    unknown = cats - set(FINDING_TYPE_CATEGORIES)
    assert not unknown, f"seed uses categories absent from the model CHECK: {unknown}"
    # The escape-hatch type must exist (finding_type_id is NOT NULL).
    assert "other" in keys


def test_seed_anatomy_and_morphology_keys_unique() -> None:
    mig = _load_migration()
    a_keys = [k for (k, _d, _lat) in mig._ANATOMY_SITES]
    m_keys = [k for (k, _d) in mig._MORPHOLOGY_TERMS]
    assert len(a_keys) == len(set(a_keys)), "duplicate anatomy_site seed keys"
    assert len(m_keys) == len(set(m_keys)), "duplicate morphology seed keys"
    assert {"spiculated", "lobulated"} <= set(m_keys)


def test_geometry_ref_requires_a_target() -> None:
    with pytest.raises(ValidationError):
        GeometryRefIn(role="bbox")  # neither marker_id nor segmentation_id
    # one target is enough
    GeometryRefIn(role="bbox", marker_id=uuid.uuid4())


def test_finding_create_defaults_and_confidence_bounds() -> None:
    f = FindingCreateIn(study_id=uuid.uuid4(), type="nodule")
    assert f.status == "candidate"
    assert f.morphology == []
    assert f.longest_diameter_mm is None
    # confidence is a 0..1 probability
    with pytest.raises(ValidationError):
        FindingCreateIn(study_id=uuid.uuid4(), type="nodule", confidence=1.5)


def test_model_constants_match_api_literals() -> None:
    assert set(FINDING_STATUSES) == {"candidate", "confirmed", "retracted"}
    assert set(LATERALITIES) == {"left", "right", "bilateral", "midline"}
