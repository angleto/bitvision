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
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from httpx import AsyncClient


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


_HAVE_DB = _have_db()

# CI's ``backend-db-test`` job arms ``BVP_REQUIRE_DB=1``: there a
# Postgres service IS up, so a probe miss (a slow runner, IPv6-first
# resolution, a remapped port) must not turn the whole DB-backed gate
# into a silent all-skipped green. With the flag set the tests RUN and
# fail loudly on the connection instead of skipping; ``test_db_gate.py``
# turns that into one readable failure. Without the flag (every local
# run) the behaviour is unchanged: no Postgres, no DB tests.
REQUIRE_DB = os.getenv("BVP_REQUIRE_DB") == "1"

skip_if_no_db = pytest.mark.skipif(not _HAVE_DB and not REQUIRE_DB, reason="no Postgres available")


def _have_s3() -> bool:
    """Cheap probe for the S3/MinIO endpoint, twin of :func:`_have_db`.

    True iff something is listening on the configured
    ``BVP_S3_ENDPOINT_URL`` (default ``http://localhost:9000``, the dev
    docker-compose MinIO). Local runs typically have Postgres but no
    MinIO; the S3 round-trip tests skip instead of failing with
    ``ConnectionRefusedError`` deep inside botocore.
    """
    import socket
    from urllib.parse import urlparse

    url = os.getenv("BVP_S3_ENDPOINT_URL") or "http://localhost:9000"
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


skip_if_no_s3 = pytest.mark.skipif(not _have_s3(), reason="no S3/MinIO endpoint available")


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


@asynccontextmanager
async def client_as(session: AsyncSession, user: User | None) -> AsyncIterator[AsyncClient]:
    """Async client bound to the live FastAPI app, authenticated as ``user``.

    ``test_collab_fixes.py`` grew a private ``_client_as`` for the same
    job but never restores ``app.dependency_overrides``, so whichever
    file ran first kept the app pinned to its own session for the rest
    of the process. This version is a context manager and restores the
    previous overrides on exit, which makes it safe to use from several
    files in one run.

    ``session`` is the test's own :class:`AsyncSession`, so a handler's
    ``await db.commit()`` is visible to the assertions afterwards
    without a second connection (and without racing the fixture's
    rollback). Passing ``user=None`` yields an anonymous client whose
    ``require_user`` raises 401.
    """
    from httpx import ASGITransport, AsyncClient

    from bvphoenix.auth import optional_user, require_user
    from bvphoenix.db.session import get_db
    from bvphoenix.main import app

    async def _db() -> AsyncIterator[AsyncSession]:
        yield session

    async def _usr() -> User:
        if user is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="not authenticated")
        return user

    if user is not None:
        # ``commit`` / ``rollback`` on the shared session expire every
        # loaded attribute. The app reads ``user.is_admin`` from handler
        # code, which runs on the event loop and NOT inside
        # ``greenlet_spawn``, so a lazy reload there raises
        # MissingGreenlet. Refreshing here (awaited, so the greenlet
        # exists) hands the app a fully loaded instance.
        await session.refresh(user)

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _usr
    app.dependency_overrides[optional_user] = _usr
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@asynccontextmanager
async def client_as_bearer(session: AsyncSession, bearer: str) -> AsyncIterator[AsyncClient]:
    """Async client that goes through the REAL auth chain.

    :func:`client_as` overrides ``require_user`` / ``optional_user``,
    which keeps ordinary tests short but bypasses everything that
    populates ``request.state.is_agent`` — so a test built on it can
    never tell a human write from an agent one, and an endpoint that
    stamped ``author_kind='human'`` on every row would look correct.

    This variant overrides only ``get_db`` and presents ``bearer`` in
    the ``Authorization`` header, so ``bvphoenix.auth.deps`` resolves
    the credential for real. Pass the plaintext per-assistant
    ``client_secret`` (the modern MCP path) to obtain an agent context
    carrying ``agent_assistant_id``; pass a user JWT for a human one.

    Like :func:`client_as` it restores the previous overrides on exit,
    so several files can use it in the same run.
    """
    from httpx import ASGITransport, AsyncClient

    from bvphoenix.db.session import get_db
    from bvphoenix.main import app

    async def _db() -> AsyncIterator[AsyncSession]:
        yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {bearer}"},
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@asynccontextmanager
async def public_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Anonymous client: ``optional_user`` resolves to ``None``.

    Distinct from ``client_as(session, None)``, which makes
    ``optional_user`` RAISE 401 — the right shape for asserting that a
    ``require_user`` endpoint refuses anonymous callers, and the wrong
    one for the public endpoints (``/api/shared/{token}/info`` and
    friends) that are supposed to answer without a session.
    """
    from httpx import ASGITransport, AsyncClient

    from bvphoenix.auth import optional_user
    from bvphoenix.db.session import get_db
    from bvphoenix.main import app

    async def _db() -> AsyncIterator[AsyncSession]:
        yield session

    async def _anon() -> None:
        return None

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[optional_user] = _anon
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
