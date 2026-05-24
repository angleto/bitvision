"""One-shot importer: copy a folder of .iso files into a patient's
fascicolo as ``imaging_study_bundle`` Documents, mirroring the worker
path in ``bvworkers.tasks.ingest_bulk._persist_iso_archives`` but
runnable from an operator's laptop with the target environment's env
vars exported (object-storage endpoint/region/buckets + DB URL).

Usage::

    export BVP_S3_ENDPOINT_URL=https://s3.example.com \\
           BVP_S3_REGION=<region> BVP_S3_BUCKET_RAW=<raw-bucket> \\
           BVP_S3_BUCKET_DERIVATIVES=<derivatives-bucket>
    uv run python scripts/import_iso_bundle.py \\
        --iso-dir /path/to/iso/to_import \\
        --patient-id 00000000-0000-0000-0000-000000000000 \\
        --owner-email operator@example.com
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    Patient,
    Subject,
    User,
)
from bvphoenix.storage import get_s3_storage


def _ensure_subfolder(
    session: Session,
    *,
    parent_folder_id: uuid.UUID | None,
    patient_id: uuid.UUID,
    owner_subject_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    existing = session.execute(
        select(Folder).where(
            Folder.name == name,
            Folder.parent_folder_id == parent_folder_id,
            Folder.patient_id == patient_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id
    folder = Folder(
        name=name,
        parent_folder_id=parent_folder_id,
        patient_id=patient_id,
        owner_subject_id=owner_subject_id,
    )
    session.add(folder)
    session.flush()
    return folder.id


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso-dir", required=True, type=Path)
    parser.add_argument("--patient-id", required=True, type=uuid.UUID)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument(
        "--no-wrap-in-folder",
        action="store_true",
        help="Drop the bundle directly in the fascicolo root.",
    )
    args = parser.parse_args()

    settings = get_settings()
    storage = get_s3_storage()
    storage.ensure_bucket(settings.s3_bucket_raw)

    engine = create_engine(settings.database_url_sync, future=True)
    with Session(engine) as session:
        patient = session.execute(
            select(Patient).where(Patient.id == args.patient_id)
        ).scalar_one_or_none()
        if patient is None:
            print(f"patient {args.patient_id} not found", file=sys.stderr)
            return 2
        owner = session.execute(
            select(Subject)
            .join(User, User.subject_id == Subject.id)
            .where(User.email == args.owner_email)
        ).scalar_one_or_none()
        if owner is None:
            print(f"user {args.owner_email} not found", file=sys.stderr)
            return 2

        iso_paths = sorted(args.iso_dir.glob("*.iso"))
        if not iso_paths:
            print(f"no .iso files in {args.iso_dir}", file=sys.stderr)
            return 1

        for iso_path in iso_paths:
            size = iso_path.stat().st_size
            doc_id = uuid.uuid4()
            safe_name = iso_path.name.replace("/", "_")
            print(f"[{safe_name}] {size / 1024**2:.1f} MiB → S3 + DB ...")

            target_folder_id: uuid.UUID | None = None
            if not args.no_wrap_in_folder:
                stem = iso_path.stem  # filename without final .iso
                target_folder_id = _ensure_subfolder(
                    session,
                    parent_folder_id=None,
                    patient_id=args.patient_id,
                    owner_subject_id=owner.id,
                    name=stem,
                )

            dst_key = f"patients/{args.patient_id}/iso/{doc_id}_{safe_name}"
            storage.upload_file(iso_path, bucket=settings.s3_bucket_raw, key=dst_key)
            sha256 = _sha256_of(iso_path)

            doc = Document(
                id=doc_id,
                patient_id=args.patient_id,
                uploaded_by_subject_id=owner.id,
                kind_id="imaging_study_bundle",
                provenance_id="dicom_dvd_iso",
                authority_id="original",
                title=f"DVD originale — {safe_name}",
                file_s3_key=dst_key,
                file_content_type="application/x-iso9660-image",
                document_date=datetime.now(UTC).date(),
                content_sha256=sha256,
                original_blob_hash=sha256,
            )
            session.add(doc)
            session.flush()
            if target_folder_id is not None:
                session.add(
                    FolderItem(
                        folder_id=target_folder_id,
                        resource_kind="document",
                        resource_id=doc_id,
                    )
                )
            session.commit()
            print(f"  Document {doc_id} -> folder {target_folder_id}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
