"""Integration tests for revert + restore_entity service helpers.

Mirrors the fixture pattern of ``test_versioning.py``: a fresh
``Subject`` + ``Patient`` per test, with cleanup via cascade. Skipped
when no Postgres is available (env var ``BVP_DATABASE_URL``).
"""

from __future__ import annotations

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
    RevertConflict,
    commit_change,
    read_at_commit,
    restore_entity_at_commit,
    revert_commit,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


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
        db.add(Subject(id=sid, kind="user", display_name=f"revert-test-{sid}"))
        await db.flush()
        db.add(
            Patient(
                id=pid,
                managed_by_subject_id=sid,
                display_name="Revert test patient",
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


def _note_payload(
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


def _actor(sid: uuid.UUID) -> ActorContext:
    return ActorContext(subject_id=sid, kind="human")


# ---------------------------------------------------------------------------
# revert_commit
# ---------------------------------------------------------------------------


class TestRevertCommit:
    @pytest.mark.asyncio
    async def test_revert_of_add_removes_entity(self, fascicolo) -> None:
        """Revert a commit that *added* an entity → entity disappears
        from the new manifest."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        # Initial commit on main: seed an unrelated entity so main is
        # not empty when we add the note.
        seed_id = uuid.uuid4()
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=seed_id,
                    payload=_note_payload(seed_id, pid, sid, "seed body"),
                )
            ],
        )

        # Add the note we will revert.
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="add note A",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "note A body"),
                )
            ],
        )
        await db.commit()

        # Revert the add.
        result = await revert_commit(
            db,
            patient_id=pid,
            commit_to_revert=target.commit_hash,
            branch_ref="main",
            actor=_actor(sid),
            message="Revert: add note A",
        )
        await db.commit()

        # Note A should be gone from the new commit's manifest.
        state = await read_at_commit(db, commit_hash=result.commit_hash)
        assert ("clinical_note", note_id) not in state
        # The seed entity is preserved (we only inverted target's effect).
        assert ("clinical_note", seed_id) in state

        # ref_log records this as op_kind='revert'.
        op = (
            await db.execute(
                text("SELECT op_kind FROM ref_log WHERE to_commit = :c AND patient_id = :p"),
                {"c": result.commit_hash, "p": pid},
            )
        ).scalar_one()
        assert op == "revert"

    @pytest.mark.asyncio
    async def test_revert_of_modify_restores_parent_payload(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        # c1: add note with body "v1"
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="add note v1",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v1"),
                )
            ],
        )
        # c2: modify to "v2"
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="modify note to v2",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v2"),
                )
            ],
        )
        await db.commit()

        # Revert c2 → note body should go back to "v1".
        result = await revert_commit(
            db,
            patient_id=pid,
            commit_to_revert=target.commit_hash,
            branch_ref="main",
            actor=_actor(sid),
            message="Revert: modify",
        )
        await db.commit()

        state = await read_at_commit(db, commit_hash=result.commit_hash)
        payload = state[("clinical_note", note_id)]
        assert payload["body"] == "v1"

    @pytest.mark.asyncio
    async def test_revert_of_delete_re_adds_entity(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="add note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "alive"),
                )
            ],
        )
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="delete note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=None,
                )
            ],
        )
        await db.commit()

        result = await revert_commit(
            db,
            patient_id=pid,
            commit_to_revert=target.commit_hash,
            branch_ref="main",
            actor=_actor(sid),
            message="Revert: delete",
        )
        await db.commit()

        state = await read_at_commit(db, commit_hash=result.commit_hash)
        assert ("clinical_note", note_id) in state
        assert state[("clinical_note", note_id)]["body"] == "alive"

    @pytest.mark.asyncio
    async def test_revert_blocked_by_divergent_head(self, fascicolo) -> None:
        """A commit between target and HEAD modifies the same entity →
        revert refuses with RevertConflict."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="add v1",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v1"),
                )
            ],
        )
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="modify to v2",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v2"),
                )
            ],
        )
        # Further modify the same entity AFTER target → divergence.
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="modify to v3",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v3"),
                )
            ],
        )
        await db.commit()

        with pytest.raises(RevertConflict) as exc:
            await revert_commit(
                db,
                patient_id=pid,
                commit_to_revert=target.commit_hash,
                branch_ref="main",
                actor=_actor(sid),
                message="Revert: v2",
            )
        assert len(exc.value.conflicts) == 1
        c = exc.value.conflicts[0]
        assert c.entity_kind == "clinical_note"
        assert c.entity_id == note_id

    @pytest.mark.asyncio
    async def test_revert_of_revert_restores_target(self, fascicolo) -> None:
        """Reverting a revert undoes the undo: the entity ends up at the
        same payload it had at the original target."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "seed"),
                )
            ],
        )
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="modify",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "modified"),
                )
            ],
        )
        r1 = await revert_commit(
            db,
            patient_id=pid,
            commit_to_revert=target.commit_hash,
            branch_ref="main",
            actor=_actor(sid),
            message="Revert: modify",
        )
        # State after r1: note body is "seed".
        state = await read_at_commit(db, commit_hash=r1.commit_hash)
        assert state[("clinical_note", note_id)]["body"] == "seed"

        # Revert the revert → note body should return to "modified".
        r2 = await revert_commit(
            db,
            patient_id=pid,
            commit_to_revert=r1.commit_hash,
            branch_ref="main",
            actor=_actor(sid),
            message="Revert: Revert: modify",
        )
        await db.commit()
        state = await read_at_commit(db, commit_hash=r2.commit_hash)
        assert state[("clinical_note", note_id)]["body"] == "modified"
        assert r1.commit_hash != r2.commit_hash

    @pytest.mark.asyncio
    async def test_revert_root_commit_rejected(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        first = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="root",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "root"),
                )
            ],
        )
        await db.commit()

        with pytest.raises(ValueError, match="root commit"):
            await revert_commit(
                db,
                patient_id=pid,
                commit_to_revert=first.commit_hash,
                branch_ref="main",
                actor=_actor(sid),
                message="should fail",
            )


# ---------------------------------------------------------------------------
# restore_entity_at_commit
# ---------------------------------------------------------------------------


class TestRestoreEntity:
    @pytest.mark.asyncio
    async def test_restore_entity_to_older_state(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        # c1: v1
        c1 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="v1",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v1"),
                )
            ],
        )
        # c2: v2
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="v2",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v2"),
                )
            ],
        )
        await db.commit()

        # Restore the note to its state at c1.
        result = await restore_entity_at_commit(
            db,
            patient_id=pid,
            source_commit=c1.commit_hash,
            entity_kind="clinical_note",
            entity_id=note_id,
            branch_ref="main",
            actor=_actor(sid),
            message="Restore note to v1",
        )
        await db.commit()

        state = await read_at_commit(db, commit_hash=result.commit_hash)
        assert state[("clinical_note", note_id)]["body"] == "v1"

        # ref_log records op_kind='revert' (restore-entity is recorded
        # as a revert in the audit ledger).
        op = (
            await db.execute(
                text("SELECT op_kind FROM ref_log WHERE to_commit = :c"),
                {"c": result.commit_hash},
            )
        ).scalar_one()
        assert op == "revert"

    @pytest.mark.asyncio
    async def test_restore_entity_absent_at_source_means_delete(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        seed = uuid.uuid4()

        # Seed-only commit; the note doesn't exist yet.
        c_seed = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="seed",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=seed,
                    payload=_note_payload(seed, pid, sid, "seed"),
                )
            ],
        )
        # Then add the note we will later "restore" to its (absent) state.
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="add note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "added later"),
                )
            ],
        )
        await db.commit()

        # Restore the note to its state at c_seed (where it didn't exist)
        # → the new commit drops it from the manifest.
        result = await restore_entity_at_commit(
            db,
            patient_id=pid,
            source_commit=c_seed.commit_hash,
            entity_kind="clinical_note",
            entity_id=note_id,
            branch_ref="main",
            actor=_actor(sid),
            message="Restore (delete) note",
        )
        await db.commit()

        state = await read_at_commit(db, commit_hash=result.commit_hash)
        assert ("clinical_note", note_id) not in state
        # The seed survives.
        assert ("clinical_note", seed) in state

    @pytest.mark.asyncio
    async def test_restore_entity_noop_raises(self, fascicolo) -> None:
        """Restoring an entity to its current state on the head is a
        no-op and should raise to keep the audit trail honest."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        c1 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="v1",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "v1"),
                )
            ],
        )
        await db.commit()
        # head == c1, asking to restore note_id to c1's state is a no-op.
        with pytest.raises(ValueError, match="already at the requested"):
            await restore_entity_at_commit(
                db,
                patient_id=pid,
                source_commit=c1.commit_hash,
                entity_kind="clinical_note",
                entity_id=note_id,
                branch_ref="main",
                actor=_actor(sid),
                message="should be no-op",
            )
