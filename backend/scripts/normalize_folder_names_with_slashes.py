"""Sanitise folders whose ``name`` contains ``/``.

Some Folder rows landed with a *path* in the name field (e.g.
``'Profilazione molecolare n. 2024/33010/I + BM/6094-6095 — …'``)
instead of the proper parent/child hierarchy. The frontend builds
``?path=/A/B/C`` URLs by splitting on ``/`` and the backend resolves
them segment by segment, so a single folder whose name contains
slashes is invisible to navigation: the user clicks it, the URL has
4 segments, the DB has 1 row, ``_resolve_path`` 404s on segment 2.

This script splits each offending name into a real chain of nested
folders under the same ``parent_folder_id``, transfers every
``FolderItem`` from the corrupted folder to the leaf of the chain,
re-parents any direct subfolder of the corrupted folder to the
leaf, and finally deletes the corrupted row. Idempotent: if any
intermediate folder with the matching ``(name, parent)`` already
exists, it is reused.

``--dry-run`` previews the planned chains without writing.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.engine import make_sync_engine
from bvphoenix.db.models import Folder, FolderItem


def _find_or_create_segment(
    session: Session,
    *,
    name: str,
    parent_folder_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
    owner_subject_id: uuid.UUID,
    apply: bool,
) -> Folder | None:
    """Return the folder matching (name, parent_folder_id, patient_id),
    creating one if it does not exist. Returns None in dry-run when the
    folder would have to be created (so the caller can still walk the
    chain by carrying the planned name forward without flushing).
    """
    parent_clause = (
        Folder.parent_folder_id == parent_folder_id
        if parent_folder_id is not None
        else Folder.parent_folder_id.is_(None)
    )
    existing = (
        session.execute(
            select(Folder).where(
                Folder.patient_id == patient_id,
                Folder.name == name,
                parent_clause,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    if not apply:
        return None
    new_folder = Folder(
        name=name,
        owner_subject_id=owner_subject_id,
        parent_folder_id=parent_folder_id,
        patient_id=patient_id,
    )
    session.add(new_folder)
    session.flush()
    return new_folder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patient-id",
        type=uuid.UUID,
        help="Restrict the scan to one patient. Default: all patient-scoped folders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned chains without writing.",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = make_sync_engine(settings.database_url_sync)
    apply = not args.dry_run

    with Session(engine) as session:
        q = select(Folder).where(Folder.name.like("%/%"))
        if args.patient_id is not None:
            q = q.where(Folder.patient_id == args.patient_id)
        corrupted = session.execute(q).scalars().all()

        if not corrupted:
            print("no folders with '/' in name")
            return

        print(f"found {len(corrupted)} corrupted folder(s):")
        for f in corrupted:
            print(
                f"  - {f.id}  patient={f.patient_id}  parent={f.parent_folder_id}  name={f.name!r}"
            )

        for corrupted_folder in corrupted:
            segments = [s for s in corrupted_folder.name.split("/") if s]
            if not segments:
                print(f"  skip {corrupted_folder.id}: empty after split")
                continue

            print()
            print(f"=== {corrupted_folder.id} ===")
            print(f"original: {corrupted_folder.name!r}")
            print(f"segments: {segments}")

            # Walk the chain under the corrupted folder's existing
            # parent. Each step is find-or-create: idempotent.
            current_parent_id: uuid.UUID | None = corrupted_folder.parent_folder_id
            chain_ids: list[uuid.UUID | None] = []
            for seg in segments:
                node = _find_or_create_segment(
                    session,
                    name=seg,
                    parent_folder_id=current_parent_id,
                    patient_id=corrupted_folder.patient_id,
                    owner_subject_id=corrupted_folder.owner_subject_id,
                    apply=apply,
                )
                if node is None:
                    # dry-run + segment to be created: print and stop
                    # walking; downstream segments will also be new.
                    print(f"  would create folder name={seg!r} under parent={current_parent_id}")
                    chain_ids.append(None)
                    current_parent_id = None  # downstream are also new
                    continue
                action = "reused" if node.id != corrupted_folder.id else "<self>"
                print(f"  {action}: {node.id}  name={seg!r}  parent={current_parent_id}")
                chain_ids.append(node.id)
                current_parent_id = node.id

            # Move every FolderItem of the corrupted folder to the
            # leaf of the chain. In dry-run we just count what would
            # move.
            items = (
                session.execute(
                    select(FolderItem.resource_kind, FolderItem.resource_id).where(
                        FolderItem.folder_id == corrupted_folder.id
                    )
                )
            ).all()
            print(f"  items to relocate: {len(items)}")

            # Any folder that lists the corrupted row as parent must
            # be re-parented to the leaf so the subtree survives.
            children = (
                session.execute(
                    select(Folder.id).where(Folder.parent_folder_id == corrupted_folder.id)
                )
            ).all()
            print(f"  child folders to re-parent: {len(children)}")

            if not apply:
                print(f"  dry-run: would delete corrupted folder {corrupted_folder.id}")
                continue

            leaf_id = chain_ids[-1]
            assert leaf_id is not None  # we are in apply branch
            for kind, rid in items:
                # Skip if the leaf already has the same item (idempotent)
                exists = session.execute(
                    select(FolderItem).where(
                        FolderItem.folder_id == leaf_id,
                        FolderItem.resource_kind == kind,
                        FolderItem.resource_id == rid,
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        FolderItem(
                            folder_id=leaf_id,
                            resource_kind=kind,
                            resource_id=rid,
                        )
                    )
            for (child_id,) in children:
                session.execute(
                    update(Folder).where(Folder.id == child_id).values(parent_folder_id=leaf_id)
                )
            # Delete corrupted folder. ``folder_items`` cascade on
            # folder_id; old items are removed implicitly.
            session.delete(corrupted_folder)
            session.commit()
            print(f"  done. leaf={leaf_id}; corrupted folder deleted.")

        if not apply:
            print()
            print("dry-run: no writes performed.")


if __name__ == "__main__":
    main()
