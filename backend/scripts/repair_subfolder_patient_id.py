"""Backfill: propagate ``patient_id`` from patient-scoped folders to
their descendant scaffolding.

Caused by a regression in ``api/folders.py:create_folder`` where
``patient_id`` was taken verbatim from the request body without
inheriting from the parent. UI / agent calls that omitted
``patient_id`` while passing ``parent_folder_id`` produced subfolders
with ``patient_id = NULL`` even when the parent (or any ancestor) was
patient-scoped. The orphan branch:

* falls outside the set returned by ``_folders_in_patient_tree`` (404
  on ``GET /api/patients/{id}/tree?path=…``),
* hides every Document / Study sitting underneath it from the Drive
  UI, even when the leaf folders themselves carry FolderItem rows.

The endpoint is now hardened (``patient_id`` propagates from parent;
mismatched bodies return 400; cross-patient moves are rejected). This
script repairs data already in the DB.

It walks every folder owned by ``--owner-subject-id`` (or all owners
with ``--all-owners``) and, for each patient-scoped folder, sets
``patient_id`` on every descendant whose current ``patient_id`` is
NULL. Descendants that already carry a different ``patient_id`` are
*never* overwritten — a mismatch is a sign of historical drift that
deserves human review and is reported.

Run with ``--dry-run`` first to inspect the planned writes.
"""

from __future__ import annotations

import argparse
import uuid
from collections import defaultdict

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.engine import make_sync_engine
from bvphoenix.db.models import Folder


def _walk_patient_subtrees(
    rows: list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]],
) -> tuple[dict[uuid.UUID, uuid.UUID], list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]]:
    """Compute the (folder_id -> expected patient_id) propagation map.

    Returns ``(to_set, conflicts)`` where ``to_set`` maps every folder
    that currently has ``patient_id IS NULL`` but lives inside a
    patient-scoped subtree to the patient_id it should carry, and
    ``conflicts`` lists ``(folder_id, current_patient_id, expected)``
    tuples for descendants whose current patient_id contradicts the
    one inherited from an ancestor (kept for human review, never
    auto-rewritten).
    """
    children_of: dict[uuid.UUID | None, list[uuid.UUID]] = defaultdict(list)
    patient_of: dict[uuid.UUID, uuid.UUID | None] = {}
    for fid, parent_id, pid in rows:
        children_of[parent_id].append(fid)
        patient_of[fid] = pid

    to_set: dict[uuid.UUID, uuid.UUID] = {}
    conflicts: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []

    # Seeds: every patient-scoped folder. Walk down each subtree
    # propagating the seed's patient_id to descendants that need it.
    for fid, _parent_id, pid in rows:
        if pid is None:
            continue
        seed_pid = pid
        stack = [fid]
        seen: set[uuid.UUID] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            cur_pid = patient_of.get(cur)
            if cur_pid is None and cur != fid:
                # Already None and not the seed itself: stage a fix.
                to_set[cur] = seed_pid
            elif cur_pid is not None and cur_pid != seed_pid and cur != fid:
                conflicts.append((cur, cur_pid, seed_pid))
                # Stop walking past a conflict — the subtree below is
                # claimed by another patient. Do not propagate the
                # seed's patient_id past this boundary.
                continue
            for child in children_of.get(cur, []):
                stack.append(child)
    return to_set, conflicts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner-subject-id",
        type=uuid.UUID,
        help="Restrict the scan to one owner. Default: all owners.",
    )
    parser.add_argument("--all-owners", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.owner_subject_id is None and not args.all_owners:
        parser.error("pass --owner-subject-id <uuid> or --all-owners")

    settings = get_settings()
    engine = make_sync_engine(settings.database_url_sync)
    with Session(engine) as session:
        q = select(Folder.id, Folder.parent_folder_id, Folder.patient_id)
        if args.owner_subject_id is not None:
            q = q.where(Folder.owner_subject_id == args.owner_subject_id)
        rows = session.execute(q).all()
        if not rows:
            print("no folders to scan")
            return

        # SQLAlchemy Row objects compare by tuple but we want plain
        # tuples for the helper.
        plain = [(r[0], r[1], r[2]) for r in rows]
        to_set, conflicts = _walk_patient_subtrees(plain)

        print(f"scanned {len(plain)} folder(s)")
        print(f"  · {len(to_set)} need patient_id propagation")
        print(f"  · {len(conflicts)} conflict(s) (will NOT be touched)")

        if conflicts:
            print("\nconflicts (manual review):")
            for fid, current, expected in conflicts[:50]:
                print(f"  - {fid}  current={current}  expected={expected}")
            if len(conflicts) > 50:
                print(f"  ...and {len(conflicts) - 50} more")

        if not to_set:
            return

        if args.dry_run:
            print("\ndry-run: would set patient_id on:")
            for fid, pid in list(to_set.items())[:50]:
                print(f"  - {fid}  patient_id <- {pid}")
            if len(to_set) > 50:
                print(f"  ...and {len(to_set) - 50} more")
            return

        # Group by target patient_id so each UPDATE rewrites a
        # cohesive set; keeps the audit log tidy if a trigger watches
        # the column.
        by_patient: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for fid, pid in to_set.items():
            by_patient[pid].append(fid)

        for pid, fids in by_patient.items():
            session.execute(
                update(Folder)
                .where(Folder.id.in_(fids), Folder.patient_id.is_(None))
                .values(patient_id=pid)
            )
            print(f"  set patient_id={pid} on {len(fids)} folder(s)")
        session.commit()
        print(f"done. {len(to_set)} folder(s) propagated.")


if __name__ == "__main__":
    main()
