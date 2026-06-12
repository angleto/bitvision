"""F12.8 cold-tier integration tests.

Verify the round-trip: a ``storage_kind='full'`` row tier-down to S3,
then ``read_object`` / ``read_at_commit`` resolve it transparently and
return the same canonical payload. Also test the worker selection
filters (size, age, tombstone, idempotency).

Skip rule mirrors the rest of the versioning tests: needs a real
Postgres with F12 + 0048 migrations applied AND a reachable MinIO /
S3 endpoint at the env-configured URL (defaults are fine for the dev
docker-compose).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bvphoenix.config import get_settings
from bvphoenix.db.models import Patient
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.versioning import (
    ActorContext,
    EntityChange,
    commit_change,
    read_at_commit,
    read_object,
    tier_down_entity_objects,
)
from bvphoenix.storage import get_s3_storage

from .conftest import skip_if_no_s3

pytestmark = [
    pytest.mark.skipif(
        not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
        reason="needs a Postgres with F12 + 0048 migrations applied",
    ),
    # The round-trip writes real objects: a missing MinIO must skip,
    # not surface as botocore ConnectionRefusedError mid-test.
    skip_if_no_s3,
]


@pytest_asyncio.fixture
async def fascicolo() -> AsyncIterator[tuple[AsyncSession, uuid.UUID, uuid.UUID]]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    try:
        await db.execute(text("SELECT set_config('app.current_subject_id', 'service', true)"))
        db.add(Subject(id=sid, kind="user", display_name=f"s3-tier-{sid}"))
        await db.flush()
        db.add(
            Patient(
                id=pid,
                managed_by_subject_id=sid,
                display_name="S3 tier patient",
            )
        )
        await db.commit()
        yield db, sid, pid
    finally:
        try:
            await db.rollback()
            await db.execute(text("SELECT set_config('app.current_subject_id', 'service', true)"))
            await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": pid})
            await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": sid})
            await db.commit()
        finally:
            await db.close()
            await engine.dispose()


def _actor(sid: uuid.UUID) -> ActorContext:
    return ActorContext(subject_id=sid, kind="human")


def _large_note(
    note_id: uuid.UUID,
    pid: uuid.UUID,
    sid: uuid.UUID,
    body: str,
) -> dict[str, Any]:
    return {
        "id": str(note_id),
        "patient_id": str(pid),
        "target_kind": "patient",
        "target_id": str(pid),
        "body": body,
        "author_subject_id": str(sid),
        "author_kind": "human",
    }


async def _backdate_object(db: AsyncSession, object_hash: bytes, days_old: int) -> None:
    """Push an entity_object's ``created_at`` back so the worker's age
    filter sees it as cold. Bypasses RLS via the service-subject set
    in the fixture."""
    await db.execute(
        text(
            "UPDATE entity_objects SET created_at = "
            "  now() - make_interval(days => :d) "
            "WHERE object_hash = :h"
        ),
        {"d": days_old, "h": object_hash},
    )


def _delete_s3_object(bucket: str, key: str) -> None:
    """Best-effort cleanup of a tier-down test artifact. Tests are
    independent on hash, so leftovers don't affect correctness, but
    we delete to keep the dev MinIO bucket tidy."""
    with contextlib.suppress(Exception):
        get_s3_storage().delete_object(bucket=bucket, key=key)


# ---------------------------------------------------------------------------
# Round-trip: tier-down then read
# ---------------------------------------------------------------------------


class TestS3RoundTrip:
    @pytest.mark.asyncio
    async def test_tier_down_then_read_object_returns_same_payload(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        # Body large enough to clear the default 16 KiB threshold.
        body = "Clinical context. " * 1500
        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="big note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_large_note(note_id, pid, sid, body),
                )
            ],
        )
        await db.commit()

        obj_hash = result.entity_object_hashes[("clinical_note", note_id)]
        assert obj_hash is not None
        # Make the row look 400-day old so the worker accepts it.
        await _backdate_object(db, obj_hash, days_old=400)
        await db.commit()

        moved = await tier_down_entity_objects(
            db,
            min_payload_bytes=8 * 1024,
            age_days=365,
            batch_limit=10,
        )
        await db.commit()
        assert moved >= 1

        # Verify the row is now in the s3 tier with NULL payload.
        row = (
            await db.execute(
                text(
                    "SELECT storage_kind, payload, s3_bucket, s3_key "
                    "FROM entity_objects WHERE object_hash = :h"
                ),
                {"h": obj_hash},
            )
        ).first()
        assert row is not None
        storage_kind, payload, s3_bucket, s3_key = row
        assert storage_kind == "s3"
        assert payload is None
        assert s3_bucket is not None
        assert s3_key.startswith("entity_objects/")

        # read_object must transparently fetch from S3.
        resolved = await read_object(db, obj_hash)
        assert resolved is not None
        assert resolved["body"] == body
        assert resolved["id"] == str(note_id)

        # read_at_commit must also resolve through S3.
        state = await read_at_commit(db, commit_hash=result.commit_hash)
        assert state[("clinical_note", note_id)]["body"] == body

        _delete_s3_object(s3_bucket, s3_key)

    @pytest.mark.asyncio
    async def test_tier_down_skips_recent_rows(self, fascicolo) -> None:
        """Recent rows must NOT be tiered down even if they are large."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        body = "X" * 30_000
        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="recent big note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_large_note(note_id, pid, sid, body),
                )
            ],
        )
        await db.commit()

        obj_hash = result.entity_object_hashes[("clinical_note", note_id)]
        # No backdating: row is recent.
        moved = await tier_down_entity_objects(
            db,
            min_payload_bytes=8 * 1024,
            age_days=365,
            batch_limit=10,
        )
        await db.commit()
        assert moved == 0

        storage_kind = (
            await db.execute(
                text("SELECT storage_kind FROM entity_objects WHERE object_hash = :h"),
                {"h": obj_hash},
            )
        ).scalar_one()
        assert storage_kind == "full"

    @pytest.mark.asyncio
    async def test_tier_down_skips_small_payloads(self, fascicolo) -> None:
        """Below-threshold rows stay inline even if they are old."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="tiny note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_large_note(note_id, pid, sid, "tiny"),
                )
            ],
        )
        await db.commit()

        obj_hash = result.entity_object_hashes[("clinical_note", note_id)]
        await _backdate_object(db, obj_hash, days_old=400)
        await db.commit()

        moved = await tier_down_entity_objects(
            db,
            min_payload_bytes=16 * 1024,
            age_days=365,
            batch_limit=10,
        )
        await db.commit()
        assert moved == 0

    @pytest.mark.asyncio
    async def test_tier_down_idempotent(self, fascicolo) -> None:
        """A second call must not re-upload a row that is already in
        the s3 tier."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        body = "Y" * 20_000
        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="idempotent",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_large_note(note_id, pid, sid, body),
                )
            ],
        )
        await db.commit()
        obj_hash = result.entity_object_hashes[("clinical_note", note_id)]
        await _backdate_object(db, obj_hash, days_old=400)
        await db.commit()

        first = await tier_down_entity_objects(
            db, min_payload_bytes=8 * 1024, age_days=365, batch_limit=10
        )
        await db.commit()
        second = await tier_down_entity_objects(
            db, min_payload_bytes=8 * 1024, age_days=365, batch_limit=10
        )
        await db.commit()
        assert first >= 1
        assert second == 0

        # Cleanup S3 artifact.
        row = (
            await db.execute(
                text("SELECT s3_bucket, s3_key FROM entity_objects WHERE object_hash = :h"),
                {"h": obj_hash},
            )
        ).first()
        if row is not None and row[0]:
            _delete_s3_object(row[0], row[1])

    @pytest.mark.asyncio
    async def test_tombstoned_rows_skipped(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        body = "Z" * 20_000
        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="will be tombstoned",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_large_note(note_id, pid, sid, body),
                )
            ],
        )
        await db.commit()
        obj_hash = result.entity_object_hashes[("clinical_note", note_id)]
        # Backdate AND tombstone. Mirror the production erasure path:
        # payload becomes the empty object (the storage invariant for
        # 'full' requires payload IS NOT NULL), is_tombstoned flips,
        # the s3 / delta indirection columns are cleared.
        await _backdate_object(db, obj_hash, days_old=400)
        await db.execute(
            text(
                "UPDATE entity_objects SET is_tombstoned = true, "
                "  tombstoned_at = now(), "
                "  tombstoned_reason = 'gdpr-test', "
                "  payload = '{}'::jsonb, "
                "  delta_bytes = NULL, "
                "  delta_parent_hash = NULL, "
                "  s3_bucket = NULL, "
                "  s3_key = NULL, "
                "  s3_etag = NULL, "
                "  storage_kind = 'full' "
                "WHERE object_hash = :h"
            ),
            {"h": obj_hash},
        )
        await db.commit()

        moved = await tier_down_entity_objects(
            db, min_payload_bytes=8 * 1024, age_days=365, batch_limit=10
        )
        await db.commit()
        assert moved == 0

        # Tombstoned reads still work, no S3 round trip needed.
        resolved = await read_object(db, obj_hash)
        assert resolved == {"_tombstoned": True}
