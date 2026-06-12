"""Integration tests for ``services.versioning.commit_change`` and the
read-side helpers (``read_at_commit``, ``diff_commits``).

These hit a real PostgreSQL with the F12 schema applied. They build
their own fascicolo (Subject + User + Patient) and clean up via Subject
delete cascade so the tests are self-contained.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bvphoenix.config import get_settings
from bvphoenix.db.models import Patient
from bvphoenix.db.models.principals import Subject
from bvphoenix.services.canonical import canonicalize, payload_hash
from bvphoenix.services.versioning import (
    ActorContext,
    ConflictsUnresolved,
    EntityChange,
    MergeResolution,
    commit_change,
    decode_delta_bytes,
    detect_conflicts,
    diff_commits,
    encode_delta_bytes,
    ensure_main_seeded,
    fast_forward_merge,
    open_consultation_branch,
    pack_entity_objects,
    read_at_commit,
    read_object,
    resolve_branch_for_write,
    submit_consultation_proposal,
    three_way_merge,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


# ---------------------------------------------------------------------------
# Fixtures: a fresh patient owned by a fresh subject for each test.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fascicolo() -> AsyncIterator[tuple[AsyncSession, uuid.UUID, uuid.UUID]]:
    """Yield (db_session, subject_id, patient_id). Cleans up on teardown.

    A fresh engine+session is built per test (NullPool) so the loop-scoped
    pytest-asyncio runtime cannot end up holding a connection bound to a
    closed loop, which would surface as ``Event loop is closed`` errors.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db = factory()
    sid = uuid.uuid4()
    pid = uuid.uuid4()
    try:
        # Service mode: bypass RLS for fixture setup.
        await db.execute(text("SELECT set_config('app.current_subject_id', 'service', true)"))
        db.add(Subject(id=sid, kind="user", display_name=f"vcs-test-{sid}"))
        await db.flush()
        db.add(
            Patient(
                id=pid,
                managed_by_subject_id=sid,
                display_name="Test Patient F12",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCommitChange:
    @pytest.mark.asyncio
    async def test_initial_commit_creates_ref_and_objects(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        payload: dict[str, Any] = {
            "id": str(note_id),
            "patient_id": str(pid),
            "target_kind": "patient",
            "target_id": str(pid),
            "body": "First clinical note.",
            "author_subject_id": str(sid),
            "author_kind": "human",
        }

        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid, kind="human"),
            message="[clinical-notes] add note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=payload,
                )
            ],
        )
        await db.commit()

        # Hash check: object_hash for that change should equal the manual sha256.
        expected_obj_hash = hashlib.sha256(canonicalize(payload)).digest()
        actual_obj_hash = result.entity_object_hashes[("clinical_note", note_id)]
        assert actual_obj_hash == expected_obj_hash

        # entity_objects has the row.
        eo = (
            await db.execute(
                text(
                    "SELECT entity_kind, payload, payload_size, storage_kind "
                    "FROM entity_objects WHERE object_hash = :h"
                ),
                {"h": actual_obj_hash},
            )
        ).first()
        assert eo is not None
        assert eo[0] == "clinical_note"
        assert eo[1]["body"] == "First clinical note."
        assert eo[3] == "full"

        # commit row exists.
        commit_row = (
            await db.execute(
                text(
                    "SELECT patient_id, parent_hashes, author_kind, message "
                    "FROM commits WHERE commit_hash = :h"
                ),
                {"h": result.commit_hash},
            )
        ).first()
        assert commit_row is not None
        assert commit_row[0] == pid
        assert commit_row[1] == []
        assert commit_row[2] == "human"
        assert commit_row[3] == "[clinical-notes] add note"

        # ref points at the new commit.
        ref_row = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid},
            )
        ).first()
        assert ref_row is not None
        assert ref_row[0] == result.commit_hash

        # ref_log has the init entry.
        log_rows = (
            await db.execute(
                text(
                    "SELECT op_kind, from_commit, to_commit "
                    "FROM ref_log WHERE patient_id = :p AND ref_name = 'main'"
                ),
                {"p": pid},
            )
        ).all()
        assert len(log_rows) == 1
        assert log_rows[0][0] == "init"
        assert log_rows[0][1] is None
        assert log_rows[0][2] == result.commit_hash

        # manifest_entries has exactly two rows for this commit:
        # one for the clinical_note, one for the _tree_ blob is NOT present
        # (the tree lives in entity_objects; manifest_entries lists the
        # entities themselves).
        manifest_rows = (
            await db.execute(
                text(
                    "SELECT entity_kind, entity_id, object_hash "
                    "FROM manifest_entries WHERE commit_hash = :c"
                ),
                {"c": result.commit_hash},
            )
        ).all()
        assert len(manifest_rows) == 1
        kinds = {r[0] for r in manifest_rows}
        assert kinds == {"clinical_note"}

    @pytest.mark.asyncio
    async def test_subsequent_commit_is_child_of_previous(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        first = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="add note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload={"id": str(note_id), "body": "v1"},
                )
            ],
        )
        await db.commit()

        second = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="edit note",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload={"id": str(note_id), "body": "v2"},
                )
            ],
        )
        await db.commit()

        # Second commit's parent_hashes contains first.
        parents = (
            await db.execute(
                text("SELECT parent_hashes FROM commits WHERE commit_hash = :c"),
                {"c": second.commit_hash},
            )
        ).scalar_one()
        assert parents == [first.commit_hash]

        # Object hash differs for the new payload (different body).
        assert (
            second.entity_object_hashes[("clinical_note", note_id)]
            != first.entity_object_hashes[("clinical_note", note_id)]
        )

        # ref now points at second commit.
        head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid},
            )
        ).scalar_one()
        assert head == second.commit_hash

    @pytest.mark.asyncio
    async def test_unchanged_entity_dedup_in_entity_objects(self, fascicolo) -> None:
        """Two commits that don't touch entity X share the same object_hash for X."""
        db, sid, pid = fascicolo
        note_a = uuid.uuid4()
        note_b = uuid.uuid4()
        payload_a = {"id": str(note_a), "body": "alpha"}
        payload_b = {"id": str(note_b), "body": "beta"}

        # Commit 1: both notes
        c1 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="add both",
            changes=[
                EntityChange("clinical_note", note_a, payload_a),
                EntityChange("clinical_note", note_b, payload_b),
            ],
        )
        await db.commit()

        # Commit 2: edit only note A
        c2 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="edit a",
            changes=[
                EntityChange("clinical_note", note_a, {"id": str(note_a), "body": "alpha-v2"})
            ],
        )
        await db.commit()

        # The unchanged note B at commit 2 must point at the same object_hash
        # it had at commit 1 (no new entity_objects row for it).
        b_at_c2 = (
            await db.execute(
                text(
                    "SELECT object_hash FROM manifest_entries "
                    "WHERE commit_hash = :c AND entity_kind = 'clinical_note' "
                    "AND entity_id = :e"
                ),
                {"c": c2.commit_hash, "e": note_b},
            )
        ).scalar_one()
        b_at_c1 = c1.entity_object_hashes[("clinical_note", note_b)]
        assert b_at_c2 == b_at_c1

    @pytest.mark.asyncio
    async def test_delete_removes_entity_from_manifest(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="add",
            changes=[EntityChange("clinical_note", note_id, {"id": str(note_id), "body": "x"})],
        )
        await db.commit()

        c2 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="delete",
            changes=[EntityChange("clinical_note", note_id, payload=None)],
        )
        await db.commit()

        # No manifest_entry for that note at c2.
        rows = (
            await db.execute(
                text(
                    "SELECT 1 FROM manifest_entries "
                    "WHERE commit_hash = :c AND entity_kind = 'clinical_note' "
                    "AND entity_id = :e"
                ),
                {"c": c2.commit_hash, "e": note_id},
            )
        ).all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_agent_commit_carries_provenance(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        result = await commit_change(
            db,
            patient_id=pid,
            branch_ref="consultation/test-123",
            actor=ActorContext(
                subject_id=sid,
                kind="agent",
                model_id="claude-opus-4-7",
                provider="anthropic",
            ),
            message="[ai:autotag] inferred 3 tags",
            changes=[
                EntityChange(
                    "clinical_note",
                    note_id,
                    {"id": str(note_id), "body": "AI-generated"},
                )
            ],
        )
        await db.commit()

        row = (
            await db.execute(
                text(
                    "SELECT author_kind, model_id, provider, branch_at_creation "
                    "FROM commits WHERE commit_hash = :c"
                ),
                {"c": result.commit_hash},
            )
        ).first()
        assert row[0] == "agent"
        assert row[1] == "claude-opus-4-7"
        assert row[2] == "anthropic"
        assert row[3] == "consultation/test-123"


class TestRead:
    @pytest.mark.asyncio
    async def test_read_at_commit_returns_full_state(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        n1 = uuid.uuid4()
        n2 = uuid.uuid4()

        c = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="seed",
            changes=[
                EntityChange("clinical_note", n1, {"id": str(n1), "body": "one"}),
                EntityChange("clinical_note", n2, {"id": str(n2), "body": "two"}),
            ],
        )
        await db.commit()

        state = await read_at_commit(db, commit_hash=c.commit_hash)
        assert ("clinical_note", n1) in state
        assert ("clinical_note", n2) in state
        assert state[("clinical_note", n1)]["body"] == "one"
        assert state[("clinical_note", n2)]["body"] == "two"

    @pytest.mark.asyncio
    async def test_diff_between_two_commits(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        n1 = uuid.uuid4()
        n2 = uuid.uuid4()
        n3 = uuid.uuid4()

        c1 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="seed",
            changes=[
                EntityChange("clinical_note", n1, {"id": str(n1), "body": "a"}),
                EntityChange("clinical_note", n2, {"id": str(n2), "body": "b"}),
            ],
        )
        await db.commit()

        c2 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="evolve",
            changes=[
                # n1 modified
                EntityChange("clinical_note", n1, {"id": str(n1), "body": "a-v2"}),
                # n2 deleted
                EntityChange("clinical_note", n2, payload=None),
                # n3 added
                EntityChange("clinical_note", n3, {"id": str(n3), "body": "c"}),
            ],
        )
        await db.commit()

        diff = await diff_commits(db, a_hash=c1.commit_hash, b_hash=c2.commit_hash)
        # Reshape for assertions.
        by_id = {(kind, eid): change for (kind, eid, change, _, _) in diff}
        assert by_id[("clinical_note", n1)] == "modified"
        assert by_id[("clinical_note", n2)] == "removed"
        assert by_id[("clinical_note", n3)] == "added"

    @pytest.mark.asyncio
    async def test_read_object_returns_payload(self, fascicolo) -> None:
        db, sid, pid = fascicolo
        n = uuid.uuid4()
        payload = {"id": str(n), "body": "single"}

        c = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=sid),
            message="m",
            changes=[EntityChange("clinical_note", n, payload)],
        )
        await db.commit()

        h = c.entity_object_hashes[("clinical_note", n)]
        assert h is not None
        out = await read_object(db, h)
        assert out is not None
        assert out["body"] == "single"

    @pytest.mark.asyncio
    async def test_read_object_missing_returns_none(self, fascicolo) -> None:
        db, _sid, _pid = fascicolo
        fake_hash = payload_hash({"never": "stored"})
        out = await read_object(db, fake_hash)
        assert out is None


class TestClinicalNotesEndpointDualWrite:
    """Verify that POST/PATCH/DELETE on the clinical_notes endpoint
    produce a consistent versioning history alongside the current
    table mutation, atomically in one transaction.
    """

    @pytest.mark.asyncio
    async def test_endpoint_post_creates_commit_and_current_row(self, fascicolo) -> None:
        # Inline the endpoint logic to test the helper without spinning
        # up FastAPI: it is the same dual-write the route performs.
        from bvphoenix.api.clinical_notes import _record_clinical_note_change
        from bvphoenix.db.models import ClinicalNote, User

        db, sid, pid = fascicolo
        # Build a User row matching the subject the fascicolo created.
        user = User(subject_id=sid, email=f"u-{sid}@example.com", is_admin=False)
        db.add(user)
        await db.flush()

        # Insert the note (current table)
        note = ClinicalNote(
            patient_id=pid,
            target_kind="patient",
            target_id=pid,
            author_subject_id=sid,
            body="Pilot test of dual-write",
            pinned=False,
            author_kind="human",
        )
        db.add(note)
        await db.flush()
        await db.refresh(note)

        # Versioning side
        class _FakeRequest:
            class state:
                agent_token = None

        # Mark user as the patient owner so resolve_branch_for_write
        # routes the write to main (no consultation_id supplied).
        (await db.execute(text("SELECT * FROM patients WHERE id = :p"), {"p": pid})).first()

        # Build a minimal Patient ORM-like proxy since our endpoint needs
        # the managed_by_subject_id field.
        class _PatientProxy:
            id = pid
            managed_by_subject_id = sid
            self_user_subject_id = None

        await _record_clinical_note_change(
            db,
            patient=_PatientProxy(),
            note=note,
            note_id=note.id,
            user=user,
            request=_FakeRequest(),
            message="[clinical-notes] add note on patient",
            consultation_id=None,
        )
        await db.commit()

        # Verify current row exists
        cur = (
            await db.execute(
                text("SELECT body, author_kind FROM clinical_notes WHERE id = :n"),
                {"n": note.id},
            )
        ).first()
        assert cur is not None
        assert cur[0] == "Pilot test of dual-write"
        assert cur[1] == "human"

        # Verify versioning side: ref main on patient points to a commit
        # whose manifest contains our clinical_note.
        ref_head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid},
            )
        ).scalar_one()
        manifest_row = (
            await db.execute(
                text(
                    "SELECT object_hash FROM manifest_entries "
                    "WHERE commit_hash = :c AND entity_kind = 'clinical_note' "
                    "AND entity_id = :e"
                ),
                {"c": ref_head, "e": note.id},
            )
        ).scalar_one()
        # The object payload contains the note's body verbatim.
        eo = (
            await db.execute(
                text("SELECT payload FROM entity_objects WHERE object_hash = :h"),
                {"h": manifest_row},
            )
        ).scalar_one()
        assert eo["body"] == "Pilot test of dual-write"
        assert eo["target_kind"] == "patient"

        # The commit author is the user, not service.
        commit_row = (
            await db.execute(
                text(
                    "SELECT author_subject_id, author_kind, message "
                    "FROM commits WHERE commit_hash = :c"
                ),
                {"c": ref_head},
            )
        ).first()
        assert commit_row[0] == sid
        assert commit_row[1] == "human"
        assert "[clinical-notes] add note" in commit_row[2]

        # Cleanup the user we inserted (cascade via subject FK is handled
        # by the fixture but the user row was added manually here).
        await db.execute(text("DELETE FROM users WHERE subject_id = :s"), {"s": sid})
        await db.commit()


class TestConsultationAsFork:
    """End-to-end the F12.1 flow: open consultation -> writes go on its
    branch, not main; sign -> fast-forward merge to main; reject ->
    branch locked, proposal withdrawn.
    """

    @pytest.mark.asyncio
    async def test_open_branch_creates_ref_at_main_head(self, fascicolo) -> None:
        db, owner_sid, pid = fascicolo
        # Seed main with one commit so the consultation has a base.
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed main",
            changes=[
                EntityChange(
                    "patient",
                    pid,
                    {"id": str(pid), "schema_version": 1, "name": "owner-init"},
                )
            ],
        )
        await db.commit()

        main_head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid},
            )
        ).scalar_one()

        # Open a consultation as a fresh subject (the "consulted doctor").
        reader_sid = uuid.uuid4()
        db.add(Subject(id=reader_sid, kind="user", display_name="reader"))
        await db.flush()
        consultation_id = uuid.uuid4()

        branch = await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=consultation_id,
            actor=ActorContext(subject_id=reader_sid),
        )
        await db.commit()
        assert branch == f"consultation/{consultation_id}"

        ref = (
            await db.execute(
                text(
                    "SELECT commit_hash, owner_subject_id, visibility "
                    "FROM refs WHERE patient_id = :p AND ref_name = :r"
                ),
                {"p": pid, "r": branch},
            )
        ).first()
        assert ref is not None
        assert ref[0] == main_head  # branch starts at main HEAD
        assert ref[1] == reader_sid
        assert ref[2] == "private"

        # Cleanup
        await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": reader_sid})
        await db.commit()

    @pytest.mark.asyncio
    async def test_open_branch_seeds_main_lazily_if_missing(self, fascicolo) -> None:
        """A patient with no main yet should auto-seed it when first
        consultation opens. Otherwise non-owner readers couldn't ever
        start collaborating on a fresh patient."""
        db, owner_sid, pid = fascicolo
        consultation_id = uuid.uuid4()
        branch = await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=consultation_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()

        main_head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid},
            )
        ).scalar_one()
        cons_head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = :r"),
                {"p": pid, "r": branch},
            )
        ).scalar_one()
        assert main_head == cons_head  # both start at the seed commit

    @pytest.mark.asyncio
    async def test_resolve_branch_owner_uses_main(self, fascicolo) -> None:
        db, owner_sid, pid = fascicolo
        await ensure_main_seeded(db, patient_id=pid, actor=ActorContext(subject_id=owner_sid))
        await db.commit()

        branch = await resolve_branch_for_write(
            db,
            patient_id=pid,
            user_subject_id=owner_sid,
            consultation_id=None,
            is_owner=True,
        )
        assert branch == "main"

    @pytest.mark.asyncio
    async def test_resolve_branch_nonowner_no_consultation_403(self, fascicolo) -> None:
        db, _owner_sid, pid = fascicolo
        intruder = uuid.uuid4()
        with pytest.raises(PermissionError):
            await resolve_branch_for_write(
                db,
                patient_id=pid,
                user_subject_id=intruder,
                consultation_id=None,
                is_owner=False,
            )

    @pytest.mark.asyncio
    async def test_full_consultation_flow_signs_and_fastforwards(self, fascicolo) -> None:
        """The end-to-end happy path: doctor B opens a consultation on
        owner A's patient, writes a note, the consultation is signed,
        main fast-forwards, the note is now on main."""
        db, owner_sid, pid = fascicolo

        # Seed main as the owner so the patient has history.
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[
                EntityChange(
                    "patient",
                    pid,
                    {"id": str(pid), "schema_version": 1, "name": "owner"},
                )
            ],
        )
        await db.commit()

        # Doctor B (reader/consultant)
        reader_sid = uuid.uuid4()
        db.add(Subject(id=reader_sid, kind="user", display_name="reader"))
        await db.flush()
        await db.commit()

        # v3 dropped the Consultation table (folded into ReportContent):
        # the consultation's identity at the versioning layer is just the
        # ``consultation/<id>`` branch ref, so a fresh uuid is all the
        # service needs.
        consultation_id = uuid.uuid4()

        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=consultation_id,
            actor=ActorContext(subject_id=reader_sid),
        )
        await db.commit()

        # Reader writes a clinical_note on the consultation branch.
        note_id = uuid.uuid4()
        await commit_change(
            db,
            patient_id=pid,
            branch_ref=f"consultation/{consultation_id}",
            actor=ActorContext(subject_id=reader_sid),
            message="[clinical-notes] add note during consult",
            changes=[
                EntityChange(
                    "clinical_note",
                    note_id,
                    {
                        "id": str(note_id),
                        "patient_id": str(pid),
                        "target_kind": "patient",
                        "target_id": str(pid),
                        "body": "Lesione probabile follow-up 6m",
                        "pinned": False,
                        "author_subject_id": str(reader_sid),
                        "author_kind": "human",
                        "schema_version": 1,
                    },
                )
            ],
        )
        await db.commit()

        # Main has NOT yet seen the note: the manifest of main does not
        # contain that entity_id.
        main_head_before = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": pid},
            )
        ).scalar_one()
        on_main_before = await read_at_commit(
            db, commit_hash=main_head_before, entity_kind="clinical_note"
        )
        assert ("clinical_note", note_id) not in on_main_before

        # Submit + fast-forward merge (signing).
        proposal_id = await submit_consultation_proposal(
            db,
            patient_id=pid,
            consultation_id=consultation_id,
            proposer_subject_id=reader_sid,
            title="Consulto pneumologico",
        )
        await db.commit()

        # Owner signs the consultation -> approve fast-forward.
        new_main = await fast_forward_merge(
            db,
            proposal_id=proposal_id,
            reviewer_subject_id=owner_sid,
            review_notes="OK approvato",
        )
        await db.commit()

        # Main now contains the note.
        on_main_after = await read_at_commit(db, commit_hash=new_main, entity_kind="clinical_note")
        assert ("clinical_note", note_id) in on_main_after
        assert on_main_after[("clinical_note", note_id)]["body"] == "Lesione probabile follow-up 6m"

        # The proposal is in 'merged' status with the right merge_commit.
        prop = (
            await db.execute(
                text("SELECT status, merge_commit, review_decision FROM proposals WHERE id = :p"),
                {"p": proposal_id},
            )
        ).first()
        assert prop[0] == "merged"
        assert prop[1] == new_main
        assert prop[2] == "approve"

        # The reviewed branch is frozen: the merge locks the source ref so
        # no further commits can land on it (the v3+ form of the old
        # consultations.status='reviewed' write freeze; signing as
        # 'signed' is a separate physician-credential flow on the
        # canonical_synthesis ReportContent).
        src_locked = (
            await db.execute(
                text("SELECT is_locked FROM refs WHERE patient_id = :p AND ref_name = :r"),
                {"p": pid, "r": f"consultation/{consultation_id}"},
            )
        ).scalar_one()
        assert src_locked is True
        with pytest.raises(PermissionError, match="locked"):
            await resolve_branch_for_write(
                db,
                patient_id=pid,
                user_subject_id=reader_sid,
                consultation_id=consultation_id,
                is_owner=False,
            )

        # ref_log records the merge event.
        merge_log = (
            await db.execute(
                text(
                    "SELECT op_kind, from_commit, to_commit FROM ref_log "
                    "WHERE patient_id = :p AND ref_name = 'main' "
                    "AND op_kind = 'merge'"
                ),
                {"p": pid},
            )
        ).first()
        assert merge_log is not None
        assert merge_log[0] == "merge"
        assert merge_log[2] == new_main

        # Cleanup. Order matters: proposals.proposer_subject_id has
        # ON DELETE RESTRICT, so the proposal must go first; then the
        # subject.
        await db.execute(text("DELETE FROM proposals WHERE patient_id = :p"), {"p": pid})
        await db.execute(text("DELETE FROM subjects WHERE id = :s"), {"s": reader_sid})
        await db.commit()


class TestThreeWayMerge:
    """F12.3 conflict detection + three-way merge engine.

    Setup pattern: open consultation branch from main, BOTH owner and
    consultant make changes that diverge, then run detect/merge.
    """

    @pytest.mark.asyncio
    async def test_no_conflict_when_disjoint_changes(self, fascicolo) -> None:
        """Owner edits entity A on main while consultation edits entity B
        on its branch. The two sides touch disjoint entities, so the
        merge has zero conflicts and proceeds automatically."""
        db, owner_sid, pid = fascicolo
        # Seed main with two notes
        n_a = uuid.uuid4()
        n_b = uuid.uuid4()
        c_seed = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[
                EntityChange("clinical_note", n_a, {"id": str(n_a), "body": "A0"}),
                EntityChange("clinical_note", n_b, {"id": str(n_b), "body": "B0"}),
            ],
        )
        await db.commit()

        # Open a consultation branch from main
        cons_id = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()
        cons_branch = f"consultation/{cons_id}"

        # Owner edits A on main; consultant edits B on consultation branch.
        c_main = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="A1",
            changes=[EntityChange("clinical_note", n_a, {"id": str(n_a), "body": "A1"})],
        )
        await db.commit()
        c_cons = await commit_change(
            db,
            patient_id=pid,
            branch_ref=cons_branch,
            actor=ActorContext(subject_id=owner_sid),
            message="B1",
            changes=[EntityChange("clinical_note", n_b, {"id": str(n_b), "body": "B1"})],
        )
        await db.commit()

        conflicts = await detect_conflicts(
            db,
            base_commit=c_seed.commit_hash,
            source_commit=c_cons.commit_hash,
            target_commit=c_main.commit_hash,
        )
        assert conflicts == []

        # Three-way merge proceeds without resolutions.
        merge_hash = await three_way_merge(
            db,
            base_commit=c_seed.commit_hash,
            source_commit=c_cons.commit_hash,
            target_commit=c_main.commit_hash,
            target_ref_name="main",
            patient_id=pid,
            actor=ActorContext(subject_id=owner_sid),
            message="merge disjoint",
        )
        await db.commit()

        # Result: main has both edits.
        state = await read_at_commit(db, commit_hash=merge_hash, entity_kind="clinical_note")
        assert state[("clinical_note", n_a)]["body"] == "A1"
        assert state[("clinical_note", n_b)]["body"] == "B1"

    @pytest.mark.asyncio
    async def test_edit_edit_conflict_requires_resolution(self, fascicolo) -> None:
        """Both sides edit the same field with different values → conflict."""
        db, owner_sid, pid = fascicolo
        n = uuid.uuid4()
        base = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "v0"})],
        )
        await db.commit()

        cons_id = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()
        cons_branch = f"consultation/{cons_id}"

        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="owner edit",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "owner"})],
        )
        await db.commit()
        source = await commit_change(
            db,
            patient_id=pid,
            branch_ref=cons_branch,
            actor=ActorContext(subject_id=owner_sid),
            message="consult edit",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "consult"})],
        )
        await db.commit()

        conflicts = await detect_conflicts(
            db,
            base_commit=base.commit_hash,
            source_commit=source.commit_hash,
            target_commit=target.commit_hash,
        )
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c.entity_kind == "clinical_note"
        assert c.entity_id == n
        assert c.conflict_kind == "edit_edit"

        # Without resolutions, three_way_merge raises.
        with pytest.raises(ConflictsUnresolved) as exc_info:
            await three_way_merge(
                db,
                base_commit=base.commit_hash,
                source_commit=source.commit_hash,
                target_commit=target.commit_hash,
                target_ref_name="main",
                patient_id=pid,
                actor=ActorContext(subject_id=owner_sid),
                message="must fail",
            )
        assert len(exc_info.value.conflicts) == 1

    @pytest.mark.asyncio
    async def test_take_source_resolution(self, fascicolo) -> None:
        """Take_source: the merged manifest pins the source's hash."""
        db, owner_sid, pid = fascicolo
        n = uuid.uuid4()
        base = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "v0"})],
        )
        await db.commit()

        cons_id = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()
        cons_branch = f"consultation/{cons_id}"

        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="t",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "owner"})],
        )
        await db.commit()
        source = await commit_change(
            db,
            patient_id=pid,
            branch_ref=cons_branch,
            actor=ActorContext(subject_id=owner_sid),
            message="s",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "consult"})],
        )
        await db.commit()
        source_hash_for_n = source.entity_object_hashes[("clinical_note", n)]

        merge_hash = await three_way_merge(
            db,
            base_commit=base.commit_hash,
            source_commit=source.commit_hash,
            target_commit=target.commit_hash,
            target_ref_name="main",
            patient_id=pid,
            actor=ActorContext(subject_id=owner_sid),
            message="take source",
            resolutions=[
                MergeResolution(
                    entity_kind="clinical_note",
                    entity_id=n,
                    kind="take_source",
                    resolved_object_hash=source_hash_for_n,
                )
            ],
        )
        await db.commit()
        state = await read_at_commit(db, commit_hash=merge_hash, entity_kind="clinical_note")
        assert state[("clinical_note", n)]["body"] == "consult"

    @pytest.mark.asyncio
    async def test_take_target_resolution(self, fascicolo) -> None:
        db, owner_sid, pid = fascicolo
        n = uuid.uuid4()
        base = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "v0"})],
        )
        await db.commit()
        cons_id = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="t",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "owner"})],
        )
        await db.commit()
        source = await commit_change(
            db,
            patient_id=pid,
            branch_ref=f"consultation/{cons_id}",
            actor=ActorContext(subject_id=owner_sid),
            message="s",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "consult"})],
        )
        await db.commit()
        target_hash = target.entity_object_hashes[("clinical_note", n)]

        merge_hash = await three_way_merge(
            db,
            base_commit=base.commit_hash,
            source_commit=source.commit_hash,
            target_commit=target.commit_hash,
            target_ref_name="main",
            patient_id=pid,
            actor=ActorContext(subject_id=owner_sid),
            message="take target",
            resolutions=[
                MergeResolution(
                    entity_kind="clinical_note",
                    entity_id=n,
                    kind="take_target",
                    resolved_object_hash=target_hash,
                )
            ],
        )
        await db.commit()
        state = await read_at_commit(db, commit_hash=merge_hash, entity_kind="clinical_note")
        assert state[("clinical_note", n)]["body"] == "owner"

    @pytest.mark.asyncio
    async def test_manual_resolution_uses_new_object(self, fascicolo) -> None:
        """Manual: caller pre-computes a fresh entity_object and supplies
        its hash; the merge pins that. Useful for clinician-mediated
        conflict resolution where neither side is right."""
        db, owner_sid, pid = fascicolo
        from bvphoenix.services.versioning import _ensure_entity_object

        n = uuid.uuid4()
        base = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "v0"})],
        )
        await db.commit()
        cons_id = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="t",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "owner"})],
        )
        await db.commit()
        source = await commit_change(
            db,
            patient_id=pid,
            branch_ref=f"consultation/{cons_id}",
            actor=ActorContext(subject_id=owner_sid),
            message="s",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "consult"})],
        )
        await db.commit()

        merged_payload = {"id": str(n), "body": "owner+consult merged"}
        manual_hash = await _ensure_entity_object(
            db, entity_kind="clinical_note", schema_version=1, payload=merged_payload
        )
        await db.commit()

        merge_hash = await three_way_merge(
            db,
            base_commit=base.commit_hash,
            source_commit=source.commit_hash,
            target_commit=target.commit_hash,
            target_ref_name="main",
            patient_id=pid,
            actor=ActorContext(subject_id=owner_sid),
            message="manual",
            resolutions=[
                MergeResolution(
                    entity_kind="clinical_note",
                    entity_id=n,
                    kind="manual",
                    resolved_object_hash=manual_hash,
                )
            ],
        )
        await db.commit()
        state = await read_at_commit(db, commit_hash=merge_hash, entity_kind="clinical_note")
        assert state[("clinical_note", n)]["body"] == "owner+consult merged"

    @pytest.mark.asyncio
    async def test_merge_commit_has_two_parents(self, fascicolo) -> None:
        db, owner_sid, pid = fascicolo
        n = uuid.uuid4()
        base = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="seed",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "v0"})],
        )
        await db.commit()
        cons_id = uuid.uuid4()
        await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=cons_id,
            actor=ActorContext(subject_id=owner_sid),
        )
        await db.commit()
        target = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="t",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "ot"})],
        )
        await db.commit()
        source = await commit_change(
            db,
            patient_id=pid,
            branch_ref=f"consultation/{cons_id}",
            actor=ActorContext(subject_id=owner_sid),
            message="s",
            changes=[EntityChange("clinical_note", n, {"id": str(n), "body": "os"})],
        )
        await db.commit()
        target_hash = target.entity_object_hashes[("clinical_note", n)]
        merge_hash = await three_way_merge(
            db,
            base_commit=base.commit_hash,
            source_commit=source.commit_hash,
            target_commit=target.commit_hash,
            target_ref_name="main",
            patient_id=pid,
            actor=ActorContext(subject_id=owner_sid),
            message="m",
            resolutions=[
                MergeResolution(
                    entity_kind="clinical_note",
                    entity_id=n,
                    kind="take_target",
                    resolved_object_hash=target_hash,
                )
            ],
        )
        await db.commit()
        parents = (
            await db.execute(
                text("SELECT parent_hashes FROM commits WHERE commit_hash = :c"),
                {"c": merge_hash},
            )
        ).scalar_one()
        # Two parents, target first by convention.
        assert len(parents) == 2
        assert parents[0] == target.commit_hash
        assert parents[1] == source.commit_hash


class TestPublishToOpenData:
    """F12.4 publish flow: clone-and-scrub a private fascicolo into a
    new PLATFORM_OWNER-owned OpenData fascicolo. Verify regex redaction,
    demographics scrub, redaction_events audit, and the source patient
    remaining intact."""

    @pytest.mark.asyncio
    async def test_publish_clones_and_scrubs(self, fascicolo) -> None:
        from datetime import date

        from bvphoenix.db.models import ClinicalNote, Patient
        from bvphoenix.services.permissions import platform_owner_subject_id
        from bvphoenix.services.publish import publish_patient_to_opendata

        db, owner_sid, pid = fascicolo

        # Enrich the source patient with PHI-laden demographics +
        # one clinical note containing CF / phone / email / address.
        await db.execute(
            text(
                "UPDATE patients SET display_name='Mario Bianchi', "
                "  birth_date='1970-05-12', "
                # v3: the codice fiscale lives in the external_identifiers
                # JSONB array, not in a tax_id column.
                '  external_identifiers=\'[{"system": '
                '"urn:oid:2.16.840.1.113883.2.9.4.3.2", '
                '"value": "BNCMRA70E12H501Z", "type": "fiscal-code"}]\'::jsonb, '
                "  phone='+39 333 1234567', email='mario@example.com', "
                "  address='Via Roma 12, 00100 Roma', "
                "  notes='Paziente seguito da MMG Dr. Verdi' "
                "WHERE id = :p"
            ),
            {"p": pid},
        )
        note_id = uuid.uuid4()
        db.add(
            ClinicalNote(
                id=note_id,
                patient_id=pid,
                target_kind="patient",
                target_id=pid,
                author_subject_id=owner_sid,
                body=(
                    "Il paziente Mario Bianchi (CF BNCMRA70E12H501Z) "
                    "e' stato visto il 12/03/2025. Telefono +39 333 1234567 "
                    "ed email mario@example.com. Residente in Via Roma 12."
                ),
                pinned=False,
                author_kind="human",
            )
        )
        await db.flush()
        await db.commit()

        source_patient = (await db.execute(select(Patient).where(Patient.id == pid))).scalar_one()

        # Run the publish helper directly.
        from bvphoenix.services.versioning import ActorContext as _Actor

        result = await publish_patient_to_opendata(
            db,
            source_patient=source_patient,
            actor=_Actor(subject_id=owner_sid, kind="human"),
            pseudonym="OpenData Test 001",
        )
        await db.commit()

        # 1. New patient exists, owned by PLATFORM_OWNER, demographics scrubbed.
        new_pid = result.public_patient_id
        new_patient = (
            await db.execute(
                text(
                    "SELECT display_name, managed_by_subject_id, birth_date, "
                    "  external_identifiers, phone, email, address "
                    "FROM patients WHERE id = :p"
                ),
                {"p": new_pid},
            )
        ).first()
        assert new_patient[0] == "OpenData Test 001"
        assert new_patient[1] == platform_owner_subject_id()
        assert new_patient[2] == date(1970, 1, 1)  # year-only
        assert new_patient[3] == []  # identifiers (CF, MRN, ...) never cloned
        assert new_patient[4] is None  # phone stripped
        assert new_patient[5] is None  # email stripped
        assert new_patient[6] is None  # address stripped

        # 2. Cloned clinical_note has redacted body.
        cloned = (
            await db.execute(
                text(
                    "SELECT id, body, author_subject_id FROM clinical_notes WHERE patient_id = :p"
                ),
                {"p": new_pid},
            )
        ).first()
        assert cloned is not None
        cloned_id, cloned_body, cloned_author = cloned
        assert "[CF]" in cloned_body
        assert "[TEL]" in cloned_body
        assert "[EMAIL]" in cloned_body
        assert "[DATE]" in cloned_body
        assert "[ADDR]" in cloned_body
        # The original CF / email / phone / address must NOT appear in the
        # cloned body.
        assert "BNCMRA70E12H501Z" not in cloned_body
        assert "mario@example.com" not in cloned_body
        assert "+39 333 1234567" not in cloned_body
        assert "Via Roma 12" not in cloned_body
        # Author rewritten to PLATFORM_OWNER for full anonymity.
        assert cloned_author == platform_owner_subject_id()

        # 3. redaction_events populated with one row per redaction.
        events = (
            await db.execute(
                text(
                    "SELECT redaction_kind FROM redaction_events "
                    "WHERE target_kind = 'clinical_note' AND target_id = :n"
                ),
                {"n": cloned_id},
            )
        ).all()
        kinds = sorted(e[0] for e in events)
        assert "regex_codice_fiscale" in kinds
        assert "regex_email" in kinds
        assert "regex_phone" in kinds
        assert "regex_date_precise" in kinds
        assert "regex_address" in kinds

        # 4. Original (private) patient is untouched.
        original = (
            await db.execute(
                text(
                    "SELECT display_name, external_identifiers, phone FROM patients WHERE id = :p"
                ),
                {"p": pid},
            )
        ).first()
        assert original[0] == "Mario Bianchi"
        assert original[1][0]["value"] == "BNCMRA70E12H501Z"
        assert original[2] == "+39 333 1234567"

        # 5. Public main has the seed commit + cloned note.
        public_main_head = (
            await db.execute(
                text("SELECT commit_hash FROM refs WHERE patient_id = :p AND ref_name = 'main'"),
                {"p": new_pid},
            )
        ).scalar_one()
        assert public_main_head == result.public_main_commit
        manifest = (
            await db.execute(
                text("SELECT entity_kind, entity_id FROM manifest_entries WHERE commit_hash = :c"),
                {"c": public_main_head},
            )
        ).all()
        kinds_in_manifest = {row[0] for row in manifest}
        assert "patient" in kinds_in_manifest
        assert "clinical_note" in kinds_in_manifest

        # Cleanup. PLATFORM_OWNER cannot be deleted; only the new patient
        # row + its cascade.
        await db.execute(text("DELETE FROM clinical_notes WHERE patient_id = :p"), {"p": pid})
        await db.execute(text("DELETE FROM patients WHERE id = :p"), {"p": new_pid})
        await db.commit()


class TestHistoryServiceContract:
    """F12.5 history API surface: cover the service-level helpers used
    by the read endpoints. The endpoints themselves are thin wrappers
    that already inherit auth coverage from existing patient-API tests."""

    @pytest.mark.asyncio
    async def test_history_walks_parent_chain(self, fascicolo) -> None:
        """A linear chain of 3 commits walks back via parent_hashes[0]."""
        db, owner_sid, pid = fascicolo
        n = uuid.uuid4()
        commits: list[bytes] = []
        for i in range(3):
            r = await commit_change(
                db,
                patient_id=pid,
                branch_ref="main",
                actor=ActorContext(subject_id=owner_sid),
                message=f"v{i}",
                changes=[EntityChange("clinical_note", n, {"id": str(n), "body": f"v{i}"})],
            )
            await db.commit()
            commits.append(r.commit_hash)
        # Walk: at any commit, parent[0] should be the previous one.
        for i, c in enumerate(commits):
            row = (
                await db.execute(
                    text("SELECT parent_hashes FROM commits WHERE commit_hash = :c"),
                    {"c": c},
                )
            ).scalar_one()
            if i == 0:
                assert row == []
            else:
                assert row[0] == commits[i - 1]

    @pytest.mark.asyncio
    async def test_diff_distinguishes_added_removed_modified(self, fascicolo) -> None:
        """diff_commits already covered upstream; here we sanity-check
        that the same shape feeds the API endpoint output cleanly."""
        db, owner_sid, pid = fascicolo
        n1 = uuid.uuid4()
        n2 = uuid.uuid4()
        c_a = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="a",
            changes=[
                EntityChange("clinical_note", n1, {"id": str(n1), "body": "1"}),
                EntityChange("clinical_note", n2, {"id": str(n2), "body": "2"}),
            ],
        )
        await db.commit()
        c_b = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=ActorContext(subject_id=owner_sid),
            message="b",
            changes=[
                EntityChange("clinical_note", n1, {"id": str(n1), "body": "1bis"}),
                EntityChange("clinical_note", n2, payload=None),
            ],
        )
        await db.commit()
        diff = await diff_commits(db, a_hash=c_a.commit_hash, b_hash=c_b.commit_hash)
        by_id = {(k, e): change for (k, e, change, _, _) in diff}
        assert by_id[("clinical_note", n1)] == "modified"
        assert by_id[("clinical_note", n2)] == "removed"


class TestPackWorker:
    """F12.6 pack worker: convert long chains of full payloads into
    delta-encoded form, mirror git's loose-vs-pack lifecycle.

    The roundtrip property is the safety net: reading any object after
    packing must return the exact same payload as before packing.
    """

    @pytest.mark.asyncio
    async def test_delta_roundtrip_property(self) -> None:
        """encode → decode = identity."""
        from bvphoenix.services.canonical import canonicalize

        parent = canonicalize({"id": "x", "body": "A" * 500 + " v1"})
        current = canonicalize({"id": "x", "body": "A" * 500 + " v2"})
        delta = encode_delta_bytes(parent, current)
        assert len(delta) < len(current) // 2
        assert decode_delta_bytes(parent, delta) == current

    @pytest.mark.asyncio
    async def test_pack_chain_replaces_redundant_versions(self, fascicolo) -> None:
        """Create 11 versions of the same clinical_note with small edits;
        pack converts intermediate ones to delta; read still returns
        the original payload."""
        db, owner_sid, pid = fascicolo
        n = uuid.uuid4()
        canonical_payloads: list[dict] = []
        for i in range(11):
            payload = {"id": str(n), "body": "A" * 300 + f" v{i}"}
            canonical_payloads.append(payload)
            await commit_change(
                db,
                patient_id=pid,
                branch_ref="main",
                actor=ActorContext(subject_id=owner_sid),
                message=f"v{i}",
                changes=[EntityChange("clinical_note", n, payload)],
            )
            await db.commit()

        # Before pack: every object is full.
        full_count_before = (
            await db.execute(
                text(
                    "SELECT count(*) FROM entity_objects "
                    "WHERE storage_kind = 'full' AND entity_kind = 'clinical_note'"
                )
            )
        ).scalar_one()

        converted = await pack_entity_objects(
            db,
            entity_kind="clinical_note",
            entity_id=n,
            snapshot_every=10,
            delta_threshold=0.5,
        )
        await db.commit()

        # At least some rows were packed.
        assert converted >= 1

        # Read every prior version: payloads must match what we wrote.
        rows = (
            await db.execute(
                text(
                    "SELECT object_hash, storage_kind FROM entity_objects "
                    "WHERE entity_kind = 'clinical_note' "
                    "ORDER BY created_at"
                )
            )
        ).all()
        # We wrote 11 distinct payloads, so we expect 11 rows for the
        # clinical_note entity_kind in this fascicolo.
        [
            r
            for r in rows
            if (await read_object(db, r[0])) is not None
            and (await read_object(db, r[0])).get("id") == str(n)
        ]
        # The above evaluation is fragile because read_object is async
        # in a comprehension. Re-do explicitly:
        kept = []
        for h, sk in rows:
            payload = await read_object(db, h)
            if payload and payload.get("id") == str(n):
                kept.append((h, sk, payload))

        # Sort by body version suffix (v0, v1, ...) numerically.
        def _ver(body: str) -> int:
            return int(body.rsplit("v", 1)[-1])

        kept_sorted = sorted(kept, key=lambda t: _ver(t[2]["body"]))
        assert len(kept_sorted) == 11
        for i, (_, _, p) in enumerate(kept_sorted):
            assert p["body"].endswith(f"v{i}")

        # Confirm the storage shape: at least one row went from 'full'
        # to 'delta'.
        full_count_after = (
            await db.execute(
                text(
                    "SELECT count(*) FROM entity_objects "
                    "WHERE storage_kind = 'full' AND entity_kind = 'clinical_note'"
                )
            )
        ).scalar_one()
        assert full_count_after < full_count_before
