"""F6.2: PATCH /api/studies/{id}/tier — unit integration tests.

Stubs ``get_db`` + ``require_user`` so we can hit the route handler
without a live Postgres. We still rely on the real route registration
to catch signature/contract drift.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bvphoenix.api import studies as studies_module

# The tier-patch endpoints live in studies.core after the 3.8.0 split.
# Each child module binds ``ensure_tier_consents``, ``revoke_tier_consent_for_study``
# and ``_enqueue_tier_reindex`` into its own globals via the
# ``from ._shared import *`` boilerplate, so the monkeypatch has to
# target that local namespace, not the package's __init__.
from bvphoenix.api.studies import core as studies_core_module
from bvphoenix.auth import require_user
from bvphoenix.db.session import get_db
from bvphoenix.main import app


class _FakeStudy:
    """Stand-in for a Study ORM row. Only the fields the handler reads
    / writes are populated."""

    def __init__(self, *, owner_subject_id: uuid.UUID, tier: str = "t1") -> None:
        self.id = uuid.uuid4()
        self.owner_subject_id = owner_subject_id
        self.contribution_tier = tier
        self.is_public = tier == "t4"


class _ScalarOne:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _StubSession:
    """Returns ``study`` on the Study lookup and forwards every other
    call to a no-op. ``commit_called`` lets the test assert the handler
    actually persisted."""

    def __init__(self, study: _FakeStudy | None) -> None:
        self._study = study
        self.commit_called = 0

    async def execute(self, _stmt: Any) -> _ScalarOne:
        return _ScalarOne(self._study)

    async def commit(self) -> None:
        self.commit_called += 1

    async def close(self) -> None:
        return None


_OWNER_SUBJECT_ID = uuid.uuid4()


@dataclass
class _StubUser:
    """Ducktypes :class:`User` — the PATCH handler only reads
    ``subject_id`` and ``is_admin``."""

    subject_id: uuid.UUID
    is_admin: bool = False


def _stub_user() -> _StubUser:
    return _StubUser(subject_id=_OWNER_SUBJECT_ID)


@pytest.fixture
def client_with_stubs(monkeypatch: pytest.MonkeyPatch):
    """Yields (client, study_ref) where mutating ``study_ref[0]``
    changes what the stub session returns for the next request."""
    study_ref: list[_FakeStudy | None] = [None]

    async def _override_get_db() -> AsyncIterator[_StubSession]:
        yield _StubSession(study_ref[0])

    async def _override_user() -> _StubUser:
        return _stub_user()

    # Disable the downstream side-effects so the test does not need a
    # live Redis and does not have to re-invent the SQLAlchemy query
    # shape that ensure_tier_consents actually sends.
    async def _noop_consents(*_: Any, **__: Any) -> list[Any]:
        return []

    async def _fake_enqueue(_study_id: uuid.UUID) -> bool:
        return True

    # Patch the child namespace (where the handler resolves the name)
    # AND the package one (for legacy callers that still reach in via
    # ``bvphoenix.api.studies.<name>``).
    for ns in (studies_core_module, studies_module):
        monkeypatch.setattr(ns, "ensure_tier_consents", _noop_consents)
        monkeypatch.setattr(ns, "revoke_tier_consent_for_study", _noop_consents)
        monkeypatch.setattr(ns, "_enqueue_tier_reindex", _fake_enqueue)

    # Save and restore existing overrides so we don't clobber whatever
    # another test module (test_transparency, test_a2a) has registered
    # at import time.
    prev_db = app.dependency_overrides.get(get_db)
    prev_user = app.dependency_overrides.get(require_user)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_user] = _override_user
    client = TestClient(app)
    try:
        yield client, study_ref
    finally:
        if prev_db is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = prev_db
        if prev_user is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = prev_user


def test_patch_tier_rejects_invalid_value(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study_ref[0] = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t1")
    response = client.patch(f"/api/studies/{study_ref[0].id}/tier", json={"tier": "t5"})
    assert response.status_code == 422


def test_patch_tier_404_when_not_owner(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study_ref[0] = _FakeStudy(owner_subject_id=uuid.uuid4(), tier="t1")
    response = client.patch(f"/api/studies/{study_ref[0].id}/tier", json={"tier": "t3"})
    # Non-owner sees 404 — we intentionally do not distinguish from a
    # missing study so the existence is not leaked.
    assert response.status_code == 404


def test_patch_tier_same_tier_is_noop(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study_ref[0] = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t2")
    response = client.patch(f"/api/studies/{study_ref[0].id}/tier", json={"tier": "t2"})
    assert response.status_code == 200
    body = response.json()
    assert body["old_tier"] == "t2"
    assert body["new_tier"] == "t2"
    assert body["reindex_enqueued"] is False


def test_patch_tier_t1_to_t3_triggers_reindex(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t1")
    study_ref[0] = study
    response = client.patch(f"/api/studies/{study.id}/tier", json={"tier": "t3"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["old_tier"] == "t1"
    assert body["new_tier"] == "t3"
    assert body["reindex_enqueued"] is True
    # T3 does not imply public.
    assert study.is_public is False
    assert study.contribution_tier == "t3"


def test_patch_tier_t1_to_t4_flags_public(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t1")
    study_ref[0] = study
    response = client.patch(f"/api/studies/{study.id}/tier", json={"tier": "t4"})
    assert response.status_code == 200
    assert study.is_public is True
    assert study.contribution_tier == "t4"


def test_patch_tier_t3_to_t1_downgrade_no_reindex(client_with_stubs) -> None:
    """Downgrade out of the commons: no reindex is kicked off and the
    study's is_public is forced to False."""
    client, study_ref = client_with_stubs
    study = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t3")
    study_ref[0] = study
    response = client.patch(f"/api/studies/{study.id}/tier", json={"tier": "t1"})
    assert response.status_code == 200
    body = response.json()
    assert body["old_tier"] == "t3"
    assert body["new_tier"] == "t1"
    assert body["reindex_enqueued"] is False
    assert study.contribution_tier == "t1"
    assert study.is_public is False


# --- DELETE /studies/{id}/training-consent --------------------------------


def test_delete_training_consent_downgrades_t3_to_t2(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t3")
    study_ref[0] = study
    response = client.delete(f"/api/studies/{study.id}/training-consent")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["old_tier"] == "t3"
    assert body["new_tier"] == "t2"
    # The stub monkeypatches revoke_tier_consent_for_study to return [].
    assert body["consent_rows_updated"] == 0
    assert study.contribution_tier == "t2"
    assert study.is_public is False


def test_delete_training_consent_t4_also_downgrades_to_t2(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t4")
    # T4 starts as is_public=True via _FakeStudy.__init__; the revoke
    # endpoint should both change the tier and clear the public flag.
    study_ref[0] = study
    response = client.delete(f"/api/studies/{study.id}/training-consent")
    assert response.status_code == 200
    body = response.json()
    assert body["new_tier"] == "t2"
    assert study.contribution_tier == "t2"
    assert study.is_public is False


def test_delete_training_consent_noop_when_already_private(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study = _FakeStudy(owner_subject_id=_OWNER_SUBJECT_ID, tier="t1")
    study_ref[0] = study
    response = client.delete(f"/api/studies/{study.id}/training-consent")
    assert response.status_code == 200
    body = response.json()
    assert body["old_tier"] == "t1"
    assert body["new_tier"] == "t1"
    assert body["consent_rows_updated"] == 0
    # Tier unchanged.
    assert study.contribution_tier == "t1"


def test_delete_training_consent_404_for_non_owner(client_with_stubs) -> None:
    client, study_ref = client_with_stubs
    study_ref[0] = _FakeStudy(owner_subject_id=uuid.uuid4(), tier="t3")
    response = client.delete(f"/api/studies/{study_ref[0].id}/training-consent")
    assert response.status_code == 404
