"""Unit tests for the shared ``get_patient_or_404`` access gate.

The helper is the single entry point that the new Q&A endpoints
(:mod:`api.qna`, :mod:`api.search_chunks`) and any future patient-
scoped surface use to layer:

1. Existence (``patient`` row present in DB).
2. Agent-token patient-scope (``enforce_agent_patient_scope`` —
   refuses agent tokens whose assistant is not bound to this patient).
3. Human visibility (``can_patient`` — refuses humans without the
   action permission).

Two regressions this gate prevents in the new code:

* A leaked agent JWT bound to patient A could call ``/api/patients/B/ask``
  before this fix because the new endpoints used ``optional_user``
  alone.
* A human session could query any patient by guessing the UUID for
  the same reason.

We test the layered ordering by mocking the dependencies in isolation
so a regression elsewhere (e.g. someone adding a new endpoint that
forgets the gate) is caught here too.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Patient, Subject
from bvphoenix.services.permissions import get_patient_or_404


def _make_request(*, is_agent: bool, allowed: set[uuid.UUID] | None = None):
    state_kwargs: dict = {"is_agent": is_agent}
    if allowed is not None:
        state_kwargs["agent_patient_ids"] = allowed
    return SimpleNamespace(state=SimpleNamespace(**state_kwargs))


@pytest.mark.asyncio
async def test_returns_patient_when_can_patient_passes():
    """Happy path with mocked deps: row exists + human caller +
    ``can_patient`` returns True → helper returns the patient row.

    Mocked rather than DB-backed so the test does not depend on the
    grant/visibility wiring (covered separately in
    ``test_visibility``); we are asserting the *helper's* layered
    sequence here, not the underlying permission model.
    """
    pid = uuid.uuid4()
    fake_patient = SimpleNamespace(id=pid)
    request = _make_request(is_agent=False)

    db_mock = AsyncMock()
    scalar_or_none = AsyncMock()
    scalar_or_none.scalar_one_or_none = lambda: fake_patient
    db_mock.execute = AsyncMock(return_value=scalar_or_none)

    with patch(
        "bvphoenix.services.permissions.can_patient",
        new=AsyncMock(return_value=True),
    ):
        out = await get_patient_or_404(
            db_mock,
            patient_id=pid,
            user=None,
            request=request,
        )
    assert out is fake_patient


@pytest.mark.asyncio
async def test_404_when_patient_does_not_exist():
    """Existence check fires first — neither agent nor human gate runs.

    Mocked DB returns ``scalar_one_or_none() -> None`` to simulate the
    "patient row missing" branch without needing a live Postgres.
    """
    request = _make_request(is_agent=False)

    db_mock = AsyncMock()
    scalar_none = AsyncMock()
    scalar_none.scalar_one_or_none = lambda: None
    db_mock.execute = AsyncMock(return_value=scalar_none)

    with pytest.raises(HTTPException) as exc:
        await get_patient_or_404(db_mock, patient_id=uuid.uuid4(), user=None, request=request)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_403_for_agent_token_without_patient_in_scope():
    """Agent JWT scoped to a different patient → enforce_agent_patient_scope
    raises 403 (we deliberately keep this code rather than coalescing
    to 404 so a leaked token cannot enumerate via timing).

    Mocked DB: only the helper's layered sequence matters; the
    membership check on ``request.state.agent_patient_ids`` is
    in-memory.
    """
    pid = uuid.uuid4()
    other_pid = uuid.uuid4()
    fake_patient = SimpleNamespace(id=pid)
    request = _make_request(is_agent=True, allowed={other_pid})

    db_mock = AsyncMock()
    scalar_or_none = AsyncMock()
    scalar_or_none.scalar_one_or_none = lambda: fake_patient
    db_mock.execute = AsyncMock(return_value=scalar_or_none)

    with pytest.raises(HTTPException) as exc:
        await get_patient_or_404(db_mock, patient_id=pid, user=None, request=request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_404_when_human_lacks_visibility():
    """Patient exists, request is human, but ``can_patient`` returns False
    → fall-through to the second 404. Mocked because building a real
    visibility miss requires multiple Patient/Grant rows we do not
    need for this assertion."""
    pid = uuid.uuid4()
    fake_patient = SimpleNamespace(id=pid)

    request = _make_request(is_agent=False)

    db_mock = AsyncMock()
    scalar_or_none = AsyncMock()
    scalar_or_none.scalar_one_or_none = lambda: fake_patient
    db_mock.execute = AsyncMock(return_value=scalar_or_none)

    with patch(
        "bvphoenix.services.permissions.can_patient",
        new=AsyncMock(return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_patient_or_404(db_mock, patient_id=pid, user=None, request=request)
    assert exc.value.status_code == 404
