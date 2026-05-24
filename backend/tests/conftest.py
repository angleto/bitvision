"""Shared pytest fixtures for integration tests against the dev Postgres.

These fixtures assume a running Postgres with the pgvector extension and
the app's migrations applied (`make up.infra && make db.migrate`). Tests
that need them create fresh UUIDs and clean up the rows they insert in
teardown. We deliberately do not stand up a separate test DB per test —
that would be slow and the isolation-by-UUID approach is sufficient for
this project's scale.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.auth.tokens import issue_agent_token
from bvphoenix.db.models import (
    AgentAssistant,
    AgentAssistantPatient,
    AgentToken,
    ClinicalEvent,
    Embedding,
    ImagingStudy,
    Patient,
    Series,
    User,
)
from bvphoenix.db.models.principals import Subject
from bvphoenix.db.session import SessionFactory
from bvphoenix.services.rate_limit import limiter as _app_limiter


@pytest.fixture(autouse=True)
def _disable_rate_limit() -> Iterator[None]:
    """Disable slowapi during tests.

    All TestClient requests come from 127.0.0.1, so the per-IP budgets
    configured in ``services/rate_limit.py`` would share state across
    the whole suite and start tripping once a file exercises more than
    a handful of requests against the same endpoint. The rate-limit
    *logic* is covered at unit level in ``test_rate_limit.py``
    (progressive lockout); tests that go through FastAPI routes care
    about functional behaviour, not throttling, so we toggle the
    limiter off for the duration of each test and restore it.
    """
    prev = _app_limiter.enabled
    _app_limiter.enabled = False
    try:
        yield
    finally:
        _app_limiter.enabled = prev


def _have_db() -> bool:
    """Cheap probe — true iff we can reach a Postgres host on a TCP
    socket within 200 ms.

    The earlier ``or True`` always passed; CI runners (no postgres
    sidecar) hit ``OSError [Errno 111]`` on every DB-touching test.
    Now: we resolve the host:port from BVP_DATABASE_URL /
    DATABASE_URL (or the dev default) and try to open a TCP socket.
    No Postgres handshake — purely "is something listening".
    """
    import socket
    from urllib.parse import urlparse

    url = (
        os.getenv("BVP_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql+asyncpg://bvphoenix:bvphoenix@localhost:5432/bvphoenix"
    )
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


skip_if_no_db = pytest.mark.skipif(not _have_db(), reason="no Postgres available")


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Yields a session bound to the dev Postgres. Rolls back at the end
    so tests never leave partial state, even if they forgot to clean up."""
    session = SessionFactory()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


def rand_vec(seed: int) -> list[float]:
    """Deterministic L2-normalized 512-dim float32 vector."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype(np.float32)
    n = float(np.linalg.norm(v))
    return (v / n).tolist() if n > 0 else v.tolist()


@pytest_asyncio.fixture
async def make_user(db_session: AsyncSession):
    """Factory: creates a Subject + User row and returns the User.
    Records created ids for teardown."""
    created: list[uuid.UUID] = []

    async def _make(email: str | None = None, is_admin: bool = False) -> User:
        sid = uuid.uuid4()
        email = email or f"test-{sid}@example.com"
        db_session.add(Subject(id=sid, kind="user", display_name=email))
        await db_session.flush()
        user = User(
            subject_id=sid,
            email=email,
            password_hash=None,
            is_admin=is_admin,
        )
        db_session.add(user)
        await db_session.flush()
        created.append(sid)
        return user

    yield _make

    # Teardown — cascade from subjects removes User, and any studies owned
    # by the subject are removed by FK cascade/restrict rules where applicable.
    for sid in created:
        await db_session.execute(Subject.__table__.delete().where(Subject.id == sid))
    await db_session.commit()


@pytest_asyncio.fixture
async def make_study(db_session: AsyncSession):
    """Factory: creates an ImagingStudy + parent ClinicalEvent + one Series.

    v3: ``ImagingStudy`` is the imaging projection of a ``ClinicalEvent``
    (1:1). The fixture creates both atomically so the FK from
    ``imaging_studies.clinical_event_id`` is satisfied. The patient_id
    used for the parent ClinicalEvent is taken from the optional
    ``patient`` argument (one is minted if not supplied).
    """
    created_events: list[uuid.UUID] = []
    created_studies: list[uuid.UUID] = []

    async def _make(
        owner: User,
        *,
        patient: Patient | None = None,
        description: str = "test study",
        modality: str = "CT",
        body_part: str = "CHEST",
        study_date=None,
        is_public: bool = False,
        series_description: str | None = None,
    ) -> tuple[ImagingStudy, Series]:
        if patient is None:
            patient = Patient(
                id=uuid.uuid4(),
                managed_by_subject_id=owner.subject_id,
                display_name="Test Patient",
            )
            db_session.add(patient)
            await db_session.flush()

        # Parent ClinicalEvent — kind='imaging_study' so the imaging
        # child slot is the natural projection.
        event = ClinicalEvent(
            id=uuid.uuid4(),
            patient_id=patient.id,
            kind="imaging_study",
            title=description or "test study",
            event_date=study_date,
            body_part=body_part,
        )
        db_session.add(event)
        await db_session.flush()
        created_events.append(event.id)

        study_uid = f"1.2.840.{uuid.uuid4().int}"[:64]
        study = ImagingStudy(
            id=uuid.uuid4(),
            patient_id=patient.id,
            clinical_event_id=event.id,
            study_instance_uid=study_uid,
            owner_subject_id=owner.subject_id,
            study_description=description,
            study_date=study_date,
            modalities=[modality],
            is_public=is_public,
        )
        db_session.add(study)
        await db_session.flush()

        series_uid = f"1.2.840.{uuid.uuid4().int}"[:64]
        series = Series(
            id=uuid.uuid4(),
            study_id=study.id,
            series_instance_uid=series_uid,
            modality=modality,
            body_part_examined=body_part,
            series_description=series_description or f"{modality} {body_part}",
        )
        db_session.add(series)
        await db_session.flush()
        await db_session.commit()
        created_studies.append(study.id)
        return study, series

    yield _make

    # Cleanup: dropping the ImagingStudy CASCADEs to Series; dropping
    # the ClinicalEvent CASCADEs to ImagingStudy. We only need to
    # delete the events; the rest follows.
    for sid in created_studies:
        await db_session.execute(ImagingStudy.__table__.delete().where(ImagingStudy.id == sid))
    for eid in created_events:
        await db_session.execute(ClinicalEvent.__table__.delete().where(ClinicalEvent.id == eid))
    await db_session.commit()


@pytest_asyncio.fixture
async def agent_token(db_session: AsyncSession, make_user):
    """Factory: creates an :class:`AgentAssistant` + :class:`AgentToken`
    bound to a freshly minted owner user.

    Returns ``(jwt_raw, token_row, assistant_row, owner_user)``. The
    JWT carries the agent's scopes and points at the assistant. Use it
    in HTTP tests via ``Authorization: Bearer <jwt_raw>``.

    Cleanup: cascades from the owner user's Subject (Subject ON DELETE
    CASCADE removes AgentAssistant which removes AgentToken).
    """
    created: list[tuple[uuid.UUID, uuid.UUID]] = []  # (assistant_id, token_id)

    async def _make(
        *,
        owner: User | None = None,
        scopes: list[str] | None = None,
        patient_ids: list[uuid.UUID] | None = None,
        ttl_seconds: int = 3600,
        label: str = "test-assistant",
    ) -> tuple[str, AgentToken, AgentAssistant, User]:
        if owner is None:
            owner = await make_user()
        scopes = scopes or ["patient:read"]
        assistant = AgentAssistant(
            id=uuid.uuid4(),
            owner_subject_id=owner.subject_id,
            label=label,
            permissions=scopes,
        )
        db_session.add(assistant)
        await db_session.flush()

        for pid in patient_ids or []:
            db_session.add(
                AgentAssistantPatient(
                    assistant_id=assistant.id,
                    patient_id=pid,
                    granted_by_subject_id=owner.subject_id,
                )
            )

        token_id = uuid.uuid4()
        jwt_raw, token_hash = issue_agent_token(
            agent_token_id=token_id,
            owner_subject_id=owner.subject_id,
            scope=scopes,
            ttl_seconds=ttl_seconds,
        )
        token = AgentToken(
            id=token_id,
            assistant_id=assistant.id,
            token_hash=token_hash,
            token_tail=jwt_raw[-8:],
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        db_session.add(token)
        await db_session.flush()
        await db_session.commit()
        created.append((assistant.id, token.id))
        return jwt_raw, token, assistant, owner

    yield _make

    for assistant_id, _ in created:
        await db_session.execute(
            AgentAssistant.__table__.delete().where(AgentAssistant.id == assistant_id)
        )
    await db_session.commit()


@pytest.fixture
def idempotency_replay():
    """Helper: build a header dict that triggers idempotent semantics.

    Usage::

        headers = idempotency_replay()
        client.patch(url, json={...}, headers=headers)
        client.patch(url, json={...}, headers=headers)  # cache hit

    The returned function memoizes a single key per call site so the
    second invocation in the same test reuses it. Pass ``new=True`` to
    mint a fresh key.
    """
    keys: dict[str, str] = {}

    def _make(slot: str = "default", *, new: bool = False) -> dict[str, str]:
        if new or slot not in keys:
            keys[slot] = str(uuid.uuid4())
        return {"Idempotency-Key": keys[slot]}

    return _make


@pytest.fixture
def mock_etag_clock():
    """Helper to produce predictable ETag-like opaque tokens in tests
    that exercise optimistic concurrency without hitting the DAG.

    Returns a callable: each call increments an internal counter and
    yields ``f'"etag-{n}"'``. Tests can compare the value or pass it
    in via the ``If-Match`` header.
    """
    counter = {"n": 0}

    def _next() -> str:
        counter["n"] += 1
        return f'"etag-{counter["n"]}"'

    return _next


@pytest_asyncio.fixture
async def make_embedding(db_session: AsyncSession):
    """Factory: inserts an Embedding row for a series."""

    async def _make(
        series: Series, *, vector: list[float] | None = None, model_id: str = "biomedclip-v1"
    ) -> Embedding:
        emb = Embedding(
            id=uuid.uuid4(),
            target_kind="series",
            target_id=series.id,
            model_id=model_id,
            vector=vector if vector is not None else rand_vec(int(series.id.int % (2**31))),
        )
        db_session.add(emb)
        await db_session.flush()
        await db_session.commit()
        return emb

    yield _make
    # Embeddings cascade away when the series is deleted via the study teardown.
