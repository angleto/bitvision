"""Backfill: link patient Documents that are not in any folder to a
target folder of choice.

Caused by a regression in ``services/bulk_ingest.py`` where stage 5
(folder linking) only created ``folder_items`` rows for the
``studies_created`` of a bulk upload, not for ``documents_created``.
Any PDF / image / text uploaded via the InlineFascicoloUploader to a
specific folder ended up in the patient's namespace but with no
``FolderItem`` link, so the fascicolo Drive UI showed it in the root
instead of the chosen folder. The bulk-upload service was patched, but
already-uploaded documents stay orphan because the dedup pass on
re-upload (``content_sha256``) skips them before stage 5 runs.

The script finds every Document on ``--patient-id`` that has no row in
``folder_items`` (``resource_kind='document'``, ``resource_id=doc.id``)
and inserts one pointing into the chosen folder. Idempotent: a doc
already linked to *any* folder is skipped.

ISO bundles (``kind_id='imaging_study_bundle'``) are excluded by
default because the ISO path already creates the FolderItem row in the
worker; if an ISO bundle is genuinely orphan, use
``repair_iso_folder_items.py`` instead, which also validates the
matching subfolder name. Pass ``--all-kinds`` to override.

Run with ``--dry-run`` first to inspect the planned writes.
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import Document, Folder, FolderItem


def _resolve_folder(
    session: Session,
    patient_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    folder_name: str | None,
) -> Folder:
    if folder_id is not None:
        folder = session.execute(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.patient_id == patient_id,
            )
        ).scalar_one_or_none()
        if folder is None:
            raise SystemExit(f"folder {folder_id} not found on patient {patient_id}")
        return folder

    assert folder_name is not None
    rows = (
        session.execute(
            select(Folder).where(
                Folder.patient_id == patient_id,
                Folder.name == folder_name,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        # Print every folder on the patient so the operator can pick
        # the right name/id without a separate roundtrip. The name is
        # a fragile identifier (rename, encoding drift, em-dash vs
        # hyphen) — show the UUID alongside so a follow-up call can
        # use ``--folder-id`` instead.
        all_rows = (
            session.execute(select(Folder.id, Folder.name).where(Folder.patient_id == patient_id))
        ).all()
        print(f"no folder named {folder_name!r} on patient {patient_id}", flush=True)
        if all_rows:
            print(f"folders on patient {patient_id}:", flush=True)
            for fid, name in sorted(all_rows, key=lambda r: r[1]):
                print(f"  {fid}  {name!r}", flush=True)
        else:
            print(f"  (no patient-scoped folder exists for {patient_id})", flush=True)
        raise SystemExit(1)
    if len(rows) > 1:
        ids = ", ".join(str(r.id) for r in rows)
        raise SystemExit(
            f"multiple folders named {folder_name!r} on patient "
            f"{patient_id}: {ids}. Use --folder-id to disambiguate."
        )
    return rows[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patient-id", required=True, type=uuid.UUID)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--folder-id", type=uuid.UUID)
    group.add_argument("--folder-name", type=str)
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help=(
            "Also include imaging_study_bundle (ISO) documents. "
            "By default they are skipped because the ISO ingest path "
            "uses repair_iso_folder_items.py for its own backfill."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be linked without writing.",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        folder = _resolve_folder(session, args.patient_id, args.folder_id, args.folder_name)
        print(f"target folder: {folder.id} (name={folder.name!r}, patient={folder.patient_id})")

        # All Documents on this patient.
        q = select(Document.id, Document.title, Document.kind_id).where(
            Document.patient_id == args.patient_id,
        )
        if not args.all_kinds:
            q = q.where(Document.kind_id != "imaging_study_bundle")
        docs = session.execute(q).all()
        if not docs:
            print(f"no documents on patient {args.patient_id}")
            return

        # Documents already linked to ANY folder — those are not orphan.
        linked = {
            row[0]
            for row in session.execute(
                select(FolderItem.resource_id).where(
                    FolderItem.resource_kind == "document",
                    FolderItem.resource_id.in_([d[0] for d in docs]),
                )
            ).all()
        }

        orphans = [(doc_id, title, kind) for doc_id, title, kind in docs if doc_id not in linked]
        if not orphans:
            print(
                f"all {len(docs)} document(s) on patient "
                f"{args.patient_id} are already in some folder"
            )
            return

        print(f"found {len(orphans)} orphan document(s):")
        for doc_id, title, kind in orphans:
            print(f"  - {doc_id}  kind={kind:24s}  title={title!r}")

        if args.dry_run:
            print(f"dry-run: would link {len(orphans)} doc(s) to folder {folder.id}")
            return

        for doc_id, _, _ in orphans:
            session.add(
                FolderItem(
                    folder_id=folder.id,
                    resource_kind="document",
                    resource_id=doc_id,
                )
            )
        session.commit()
        print(f"done. {len(orphans)} FolderItem row(s) inserted into folder {folder.id}")


if __name__ == "__main__":
    main()
