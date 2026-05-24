"""Pure-logic tests for the patient_id propagation walker used by
``scripts/repair_subfolder_patient_id.py``. The DB-side migration is
not exercised here (covered by manual dry-run on the deploy pod), but
the walker is the place where a wrong rule would silently rewrite or
miss orphan branches, so it deserves its own unit-level safety net.
"""

from __future__ import annotations

import uuid

from scripts.repair_subfolder_patient_id import _walk_patient_subtrees


def _u() -> uuid.UUID:
    return uuid.uuid4()


def test_propagates_to_null_descendants() -> None:
    pid = _u()
    root = _u()  # patient-scoped root
    mid = _u()  # NULL — created via the buggy POST /folders
    leaf = _u()  # NULL — descendant of the orphan branch
    rows = [
        (root, None, pid),
        (mid, root, None),
        (leaf, mid, None),
    ]
    to_set, conflicts = _walk_patient_subtrees(rows)
    assert to_set == {mid: pid, leaf: pid}
    assert conflicts == []


def test_does_not_touch_seed() -> None:
    pid = _u()
    root = _u()
    rows = [(root, None, pid)]
    to_set, conflicts = _walk_patient_subtrees(rows)
    assert to_set == {}
    assert conflicts == []


def test_conflict_is_reported_not_overwritten() -> None:
    pid_a = _u()
    pid_b = _u()
    root = _u()  # patient_id = pid_a
    rogue = _u()  # patient_id = pid_b — historical drift
    rows = [
        (root, None, pid_a),
        (rogue, root, pid_b),
    ]
    to_set, conflicts = _walk_patient_subtrees(rows)
    assert to_set == {}
    assert conflicts == [(rogue, pid_b, pid_a)]


def test_conflict_blocks_propagation_from_outer_seed() -> None:
    """A descendant past a conflicting node must NOT inherit
    ``patient_id`` from the outer seed (pid_a) — the conflict marks
    the sub-branch as claimed by a different patient. It MUST inherit
    from the inner seed (pid_b) since rogue is itself patient-scoped,
    so a NULL leaf under it correctly belongs to pid_b.
    """
    pid_a = _u()
    pid_b = _u()
    root = _u()  # pid_a — outer seed
    rogue = _u()  # pid_b — inner seed (different patient)
    leaf_under_rogue = _u()  # NULL — under rogue, must end up as pid_b
    rows = [
        (root, None, pid_a),
        (rogue, root, pid_b),
        (leaf_under_rogue, rogue, None),
    ]
    to_set, conflicts = _walk_patient_subtrees(rows)
    # The outer seed (pid_a) must not write past the conflict; the
    # inner seed (pid_b) is the rightful owner of the leaf.
    assert to_set == {leaf_under_rogue: pid_b}
    assert (rogue, pid_b, pid_a) in conflicts


def test_user_owned_subtree_left_alone() -> None:
    """Folders with no patient_id ancestor must not be touched. The
    walker is seeded only on patient-scoped folders, so a pure user
    workspace stays user-scoped.
    """
    a = _u()
    b = _u()
    rows = [
        (a, None, None),
        (b, a, None),
    ]
    to_set, conflicts = _walk_patient_subtrees(rows)
    assert to_set == {}
    assert conflicts == []


def test_sibling_subtrees_are_independent() -> None:
    pid_a = _u()
    pid_b = _u()
    root_a = _u()
    leaf_a = _u()
    root_b = _u()
    leaf_b = _u()
    rows = [
        (root_a, None, pid_a),
        (leaf_a, root_a, None),
        (root_b, None, pid_b),
        (leaf_b, root_b, None),
    ]
    to_set, _conflicts = _walk_patient_subtrees(rows)
    assert to_set == {leaf_a: pid_a, leaf_b: pid_b}
