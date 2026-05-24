"""Integration tests for the merge3-based three-way text merge.

The unit under test is :func:`_attempt_text_auto_merge` and the
``submit_consultation_proposal`` integration that calls it. We verify
the round-trip: non-overlapping edits on a textual entity field are
auto-resolved (``merge_conflicts.resolution='auto_merge'`` with
``resolved_object_hash`` already set), overlapping edits stay manual,
and the merge endpoint can fast-track a proposal whose only conflict
was auto-resolved.
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
    _attempt_text_auto_merge,
    commit_change,
    open_consultation_branch,
    read_object,
    submit_consultation_proposal,
    three_way_merge,
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("BVP_DATABASE_URL") or os.getenv("DATABASE_URL")),
    reason="needs a Postgres with F12 migrations applied",
)


# ---------------------------------------------------------------------------
# Fixtures: copy of the standard fascicolo fixture from test_versioning_revert
# ---------------------------------------------------------------------------


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
        db.add(Subject(id=sid, kind="user", display_name=f"text-merge-{sid}"))
        await db.flush()
        db.add(
            Patient(
                id=pid,
                managed_by_subject_id=sid,
                display_name="Text merge patient",
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
# _attempt_text_auto_merge: direct unit-ish tests
# ---------------------------------------------------------------------------


class TestAutoMergeHelper:
    @pytest.mark.asyncio
    async def test_non_overlapping_edits_auto_merge(self, fascicolo) -> None:
        """Source edits paragraph 2, target edits paragraph 4 → merge3
        produces a clean union without conflict markers."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        base = "para1\npara2\npara3\npara4\npara5\n"
        source = "para1\nPARA2-source\npara3\npara4\npara5\n"
        target = "para1\npara2\npara3\nPARA4-target\npara5\n"

        # Build the three entity_objects via commit_change (cheap path
        # to obtain object_hashes).
        c0 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="base",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, base),
                )
            ],
        )
        c_src = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="source",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, source),
                )
            ],
        )
        c_tgt = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="target",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, target),
                )
            ],
        )
        await db.commit()

        base_h = c0.entity_object_hashes[("clinical_note", note_id)]
        src_h = c_src.entity_object_hashes[("clinical_note", note_id)]
        tgt_h = c_tgt.entity_object_hashes[("clinical_note", note_id)]

        merged_hash = await _attempt_text_auto_merge(
            db,
            base_hash=base_h,
            source_hash=src_h,
            target_hash=tgt_h,
            entity_kind="clinical_note",
        )
        assert merged_hash is not None
        merged = await read_object(db, merged_hash)
        assert merged is not None
        assert merged["body"] == ("para1\nPARA2-source\npara3\nPARA4-target\npara5\n")

    @pytest.mark.asyncio
    async def test_overlapping_edits_refused(self, fascicolo) -> None:
        """Source and target both edit the same line → marker conflict
        → helper returns None."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        base = "para1\nshared\npara3\n"
        source = "para1\nFROM_SOURCE\npara3\n"
        target = "para1\nFROM_TARGET\npara3\n"
        c0 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="base",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, base),
                )
            ],
        )
        c_src = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="source",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, source),
                )
            ],
        )
        c_tgt = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="target",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, target),
                )
            ],
        )
        await db.commit()

        merged_hash = await _attempt_text_auto_merge(
            db,
            base_hash=c0.entity_object_hashes[("clinical_note", note_id)],
            source_hash=c_src.entity_object_hashes[("clinical_note", note_id)],
            target_hash=c_tgt.entity_object_hashes[("clinical_note", note_id)],
            entity_kind="clinical_note",
        )
        assert merged_hash is None

    @pytest.mark.asyncio
    async def test_metadata_diff_refused(self, fascicolo) -> None:
        """Source flips ``pinned`` while target edits text → we refuse
        to auto-merge (we don't guess metadata)."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()

        def payload_with(body: str, pinned: bool) -> dict[str, Any]:
            p = _note_payload(note_id, pid, sid, body)
            p["pinned"] = pinned
            return p

        c0 = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="base",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=payload_with("para1\npara2\n", pinned=False),
                )
            ],
        )
        # Source: change pinned only
        c_src = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="source pin",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=payload_with("para1\npara2\n", pinned=True),
                )
            ],
        )
        # Target: change body only (non-overlapping vs source's text edits,
        # since source didn't edit text at all). But ``pinned`` differs.
        c_tgt = await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="target text",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=payload_with("para1\npara2-extended\n", pinned=False),
                )
            ],
        )
        await db.commit()

        merged_hash = await _attempt_text_auto_merge(
            db,
            base_hash=c0.entity_object_hashes[("clinical_note", note_id)],
            source_hash=c_src.entity_object_hashes[("clinical_note", note_id)],
            target_hash=c_tgt.entity_object_hashes[("clinical_note", note_id)],
            entity_kind="clinical_note",
        )
        assert merged_hash is None

    @pytest.mark.asyncio
    async def test_unsupported_kind_returns_none(self, fascicolo) -> None:
        """A kind not in TEXTUAL_FIELDS (e.g. tag) is not auto-merge-eligible."""
        db, _sid, _pid = fascicolo
        # We don't even need real hashes; bytes(32) suffices to fail the
        # kind check before any DB access.
        result = await _attempt_text_auto_merge(
            db,
            base_hash=b"\x00" * 32,
            source_hash=b"\x01" * 32,
            target_hash=b"\x02" * 32,
            entity_kind="tag",
        )
        assert result is None


# ---------------------------------------------------------------------------
# End-to-end: submit_consultation_proposal → three_way_merge through merge_conflicts
# ---------------------------------------------------------------------------


class TestSubmitProposalAutoMerge:
    @pytest.mark.asyncio
    async def test_proposal_auto_merges_non_overlapping_edits(self, fascicolo) -> None:
        """Two divergent branches edit non-overlapping paragraphs of the
        same clinical_note. Submitting the consultation proposal
        pre-resolves the conflict; conflict_count is 0 and the
        merge_conflicts row carries resolution='auto_merge'."""
        db, sid, pid = fascicolo
        note_id = uuid.uuid4()
        base_text = "p1\np2\np3\np4\np5\n"

        # Seed main with the base body.
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
                    payload=_note_payload(note_id, pid, sid, base_text),
                )
            ],
        )
        # Open a consultation branch by writing a no-op-text commit on it.
        consultation_id = uuid.uuid4()
        # Pre-create the consultations row that submit_consultation_proposal
        # wires the proposal back to. We only need the columns that the
        # proposal/merge code reads.
        await db.execute(
            text(
                "INSERT INTO consultations "
                "(id, patient_id, author_subject_id, author_kind, "
                " status, title, created_at, updated_at) "
                "VALUES (:id, :pid, :sid, 'human', 'submitted', "
                "  'test consultation', now(), now())"
            ),
            {"id": consultation_id, "pid": pid, "sid": sid},
        )
        # Materialise the consultation branch at main's current head so
        # there is a proper LCA between source and target later on.
        consult_ref = await open_consultation_branch(
            db,
            patient_id=pid,
            consultation_id=consultation_id,
            actor=_actor(sid),
        )
        # Source edits paragraph 2, lands on consultation branch.
        await commit_change(
            db,
            patient_id=pid,
            branch_ref=consult_ref,
            actor=_actor(sid),
            message="source edit",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "p1\nP2-source\np3\np4\np5\n"),
                )
            ],
        )
        # Target edits paragraph 4, lands on main.
        await commit_change(
            db,
            patient_id=pid,
            branch_ref="main",
            actor=_actor(sid),
            message="target edit",
            changes=[
                EntityChange(
                    entity_kind="clinical_note",
                    entity_id=note_id,
                    payload=_note_payload(note_id, pid, sid, "p1\np2\np3\nP4-target\np5\n"),
                )
            ],
        )
        await db.commit()

        proposal_id = await submit_consultation_proposal(
            db,
            patient_id=pid,
            consultation_id=consultation_id,
            proposer_subject_id=sid,
            title="merge me",
        )
        await db.commit()

        # conflict_count should be 0 (auto-merged).
        cc = (
            await db.execute(
                text("SELECT conflict_count FROM proposals WHERE id = :p"),
                {"p": proposal_id},
            )
        ).scalar_one()
        assert cc == 0

        # merge_conflicts row exists and is auto-resolved.
        rows = (
            await db.execute(
                text(
                    "SELECT resolution, resolved_object_hash, conflict_kind "
                    "FROM merge_conflicts WHERE proposal_id = :p"
                ),
                {"p": proposal_id},
            )
        ).all()
        assert len(rows) == 1
        resolution, resolved_hash, conflict_kind = rows[0]
        assert resolution == "auto_merge"
        assert resolved_hash is not None
        assert conflict_kind == "edit_edit"

        # The auto-merged payload contains both edits.
        merged = await read_object(db, resolved_hash)
        assert merged is not None
        assert merged["body"] == "p1\nP2-source\np3\nP4-target\np5\n"

        # Three-way merge with the auto_merge resolution should apply
        # the merged hash without complaint. Use the existing service
        # path: build MergeResolution from the merge_conflicts row.
        from bvphoenix.services.versioning import MergeResolution

        proposal_row = (
            await db.execute(
                text(
                    "SELECT base_commit, source_head_commit, "
                    "  target_head_commit, target_ref_name "
                    "FROM proposals WHERE id = :p"
                ),
                {"p": proposal_id},
            )
        ).first()
        assert proposal_row is not None
        base_c, src_c, tgt_c, tgt_ref = proposal_row

        resolution_rows = (
            await db.execute(
                text(
                    "SELECT entity_kind, entity_id, resolution, "
                    "  resolved_object_hash "
                    "FROM merge_conflicts WHERE proposal_id = :p"
                ),
                {"p": proposal_id},
            )
        ).all()
        resolutions = [
            MergeResolution(
                entity_kind=r[0],
                entity_id=r[1],
                kind=r[2],
                resolved_object_hash=r[3],
            )
            for r in resolution_rows
        ]
        merge_hash = await three_way_merge(
            db,
            base_commit=base_c,
            source_commit=src_c,
            target_commit=tgt_c,
            target_ref_name=tgt_ref,
            patient_id=pid,
            actor=_actor(sid),
            message="merge proposal",
            resolutions=resolutions,
        )
        await db.commit()

        # The merge commit's manifest should pin the auto-merged hash
        # for our note.
        post_state = (
            await db.execute(
                text(
                    "SELECT object_hash FROM manifest_entries "
                    "WHERE commit_hash = :c AND entity_kind = 'clinical_note' "
                    "  AND entity_id = :e"
                ),
                {"c": merge_hash, "e": note_id},
            )
        ).scalar_one()
        assert post_state == resolved_hash
