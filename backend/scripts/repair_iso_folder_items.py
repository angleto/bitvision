"""Idempotent fix-up: for every ``imaging_study_bundle`` Document on a
patient, ensure a ``folder_items`` row exists pointing into the
matching per-CD subfolder. Re-runs of ``import_iso_bundle.py`` left
some FolderItem rows missing (the long S3 upload between flush() and
commit() interacts badly with the managed-PG idle transaction
timeout), and without those rows ``_folders_in_patient_tree`` cannot
reach the folder so navigating to it returns 404 → the frontend
shows the "Drive UI in rollout" banner."""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import Document, Folder, FolderItem


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patient-id", required=True, type=uuid.UUID)
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        # All ISO-bundle Documents on this patient.
        docs = session.execute(
            select(Document.id, Document.title)
            .where(
                Document.patient_id == args.patient_id,
                Document.kind_id == "imaging_study_bundle",
            )
        ).all()
        if not docs:
            print(f"no imaging_study_bundle docs on patient {args.patient_id}")
            return

        repaired = 0
        for doc_id, title in docs:
            # Title format: "DVD originale — <safe_name>" — strip the
            # prefix and the .iso extension to recover the iso stem
            # used as the folder's name.
            stem = title
            for prefix in ("DVD originale — ", "DVD originale - "):
                if stem.startswith(prefix):
                    stem = stem[len(prefix):]
                    break
            if stem.lower().endswith(".iso"):
                stem = stem[: -len(".iso")]

            folder_row = session.execute(
                select(Folder.id).where(
                    Folder.patient_id == args.patient_id,
                    Folder.parent_folder_id.is_(None),
                    Folder.name == stem,
                )
            ).first()
            if folder_row is None:
                print(f"  doc {doc_id} (stem={stem!r}): no matching folder, skip")
                continue
            folder_id = folder_row[0]

            existing = session.execute(
                select(FolderItem).where(
                    FolderItem.folder_id == folder_id,
                    FolderItem.resource_kind == "document",
                    FolderItem.resource_id == doc_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  doc {doc_id} -> folder {folder_id}: already linked, skip")
                continue

            session.add(
                FolderItem(
                    folder_id=folder_id,
                    resource_kind="document",
                    resource_id=doc_id,
                )
            )
            session.commit()
            repaired += 1
            print(f"  doc {doc_id} -> folder {folder_id}: linked")

        print(f"done. {repaired} FolderItem row(s) inserted.")


if __name__ == "__main__":
    main()
