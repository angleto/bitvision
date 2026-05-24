"""Diagnostic: print every Folder + FolderItem related to a patient,
plus the resolved ``_folders_in_patient_tree`` set, to understand why
the tree endpoint may report 404 on a subfolder we just inserted."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy import create_engine

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    ImagingStudy,
    Patient,
    Subject,
    User,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patient-id", required=True, type=uuid.UUID)
    parser.add_argument("--owner-email", required=True)
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        patient = session.execute(
            select(Patient).where(Patient.id == args.patient_id)
        ).scalar_one_or_none()
        owner = session.execute(
            select(Subject)
            .join(User, User.subject_id == Subject.id)
            .where(User.email == args.owner_email)
        ).scalar_one_or_none()
        print(f"patient: {patient.id if patient else 'NOT FOUND'}")
        print(f"owner subject: {owner.id if owner else 'NOT FOUND'}")
        if not patient or not owner:
            return

        print("\n--- ImagingStudy patient_id ---")
        studies = session.execute(
            select(ImagingStudy.id, ImagingStudy.study_description, ImagingStudy.owner_subject_id)
            .where(ImagingStudy.patient_id == args.patient_id)
        ).all()
        print(f"  total: {len(studies)}")
        for sid, descr, oid in studies[:5]:
            print(f"    {sid} owner={oid} descr={descr}")

        print("\n--- Document patient_id ---")
        docs = session.execute(
            select(Document.id, Document.title, Document.kind_id)
            .where(Document.patient_id == args.patient_id)
        ).all()
        print(f"  total: {len(docs)}")
        for did, title, kind in docs[:10]:
            print(f"    {did} kind={kind} title={title!r}")

        print("\n--- Folder patient_id + owner_subject_id ---")
        folders = session.execute(
            select(Folder.id, Folder.name, Folder.parent_folder_id, Folder.owner_subject_id)
            .where(Folder.patient_id == args.patient_id)
        ).all()
        print(f"  total: {len(folders)}")
        for fid, name, parent, oid in folders:
            print(f"    {fid} parent={parent} owner={oid} name={name!r}")

        print("\n--- FolderItem joining Folder.patient_id ---")
        items = session.execute(
            select(
                FolderItem.folder_id,
                FolderItem.resource_kind,
                FolderItem.resource_id,
                Folder.name,
                Folder.owner_subject_id,
            )
            .join(Folder, Folder.id == FolderItem.folder_id)
            .where(Folder.patient_id == args.patient_id)
        ).all()
        print(f"  total: {len(items)}")
        for fid, kind, rid, fname, oid in items[:20]:
            print(f"    folder={fname!r} ({fid}) owner={oid} -> {kind} {rid}")


if __name__ == "__main__":
    main()
