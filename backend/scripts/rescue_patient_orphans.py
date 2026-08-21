"""Rescue: find every orphan Document / ImagingStudy of a patient
(no row in ``folder_items`` linking it to any folder) and link them
into a freshly-created ``_Recovery <timestamp>`` folder at the
patient root.

Used when a regression has left items unlinked from any folder so
the fascicolo Drive UI doesn't surface them. The script does not
delete or move existing FolderItem rows; it only inserts new ones
into the recovery folder. Idempotent re-runs create separate
recovery folders (timestamp suffix) — safe to re-run, the user can
review the outcome and merge or rename folders via the UI.

Use ``--dry-run`` first to inspect counts and a sample of the
affected resources before writing.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bvphoenix.config import get_settings
from bvphoenix.db.engine import make_sync_engine
from bvphoenix.db.models import Document, Folder, FolderItem, ImagingStudy, Patient


def _resolve_owner(patient: Patient) -> uuid.UUID:
    """The recovery folder is owned by whoever stewards the patient.
    Manager wins over self-user (a self-managed patient still has
    ``managed_by_subject_id`` set in practice; the fall-through
    handles legacy rows)."""
    owner = patient.managed_by_subject_id or patient.self_user_subject_id
    if owner is None:
        raise SystemExit(
            f"patient {patient.id} has neither managed_by_subject_id "
            f"nor self_user_subject_id; cannot determine recovery folder owner"
        )
    return owner


def _orphan_documents(session: Session, patient_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
    rows = session.execute(
        select(Document.id, Document.title, Document.kind_id).where(
            Document.patient_id == patient_id
        )
    ).all()
    if not rows:
        return []
    linked = {
        r[0]
        for r in session.execute(
            select(FolderItem.resource_id).where(
                FolderItem.resource_kind == "document",
                FolderItem.resource_id.in_([d[0] for d in rows]),
            )
        ).all()
    }
    return [(d[0], d[1], d[2]) for d in rows if d[0] not in linked]


def _orphan_studies(
    session: Session, patient_id: uuid.UUID
) -> list[tuple[uuid.UUID, str | None, str | None]]:
    rows = session.execute(
        select(
            ImagingStudy.id,
            ImagingStudy.study_description,
            ImagingStudy.study_instance_uid,
        ).where(ImagingStudy.patient_id == patient_id)
    ).all()
    if not rows:
        return []
    linked = {
        r[0]
        for r in session.execute(
            select(FolderItem.resource_id).where(
                FolderItem.resource_kind == "study",
                FolderItem.resource_id.in_([st[0] for st in rows]),
            )
        ).all()
    }
    return [(st[0], st[1], st[2]) for st in rows if st[0] not in linked]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patient-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--folder-name",
        default=None,
        help="Override the default '_Recovery <timestamp>' name.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the orphan inventory and the planned folder name without writing.",
    )
    parser.add_argument(
        "--include-studies",
        action="store_true",
        default=True,
        help="Include orphan ImagingStudy rows in the rescue (default: true).",
    )
    parser.add_argument(
        "--no-include-studies",
        dest="include_studies",
        action="store_false",
        help="Skip studies; rescue only Document rows.",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = make_sync_engine(settings.database_url_sync)
    with Session(engine) as session:
        patient = session.execute(
            select(Patient).where(Patient.id == args.patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise SystemExit(f"patient {args.patient_id} not found")
        owner = _resolve_owner(patient)

        orphan_docs = _orphan_documents(session, args.patient_id)
        orphan_studies = _orphan_studies(session, args.patient_id) if args.include_studies else []

        print(
            f"patient {args.patient_id}: "
            f"{len(orphan_docs)} orphan document(s), "
            f"{len(orphan_studies)} orphan study(s)"
        )
        if not orphan_docs and not orphan_studies:
            print("nothing to rescue")
            return

        sample_n = 50
        for doc_id, title, kind in orphan_docs[:sample_n]:
            print(f"  doc   {doc_id}  kind={kind:24s}  title={title!r}")
        if len(orphan_docs) > sample_n:
            print(f"  ...and {len(orphan_docs) - sample_n} more documents")
        for st_id, desc, uid in orphan_studies[:sample_n]:
            print(f"  study {st_id}  desc={desc!r}  uid={uid}")
        if len(orphan_studies) > sample_n:
            print(f"  ...and {len(orphan_studies) - sample_n} more studies")

        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S_UTC")
        folder_name = args.folder_name or f"_Recovery {ts}"

        if args.dry_run:
            print(
                f"\ndry-run: would create root folder {folder_name!r} on "
                f"patient {args.patient_id} (owner={owner}) and link "
                f"{len(orphan_docs)} doc(s) + {len(orphan_studies)} study(s)"
            )
            return

        recovery = Folder(
            name=folder_name,
            owner_subject_id=owner,
            parent_folder_id=None,
            patient_id=args.patient_id,
        )
        session.add(recovery)
        session.flush()

        for doc_id, _, _ in orphan_docs:
            session.add(
                FolderItem(
                    folder_id=recovery.id,
                    resource_kind="document",
                    resource_id=doc_id,
                )
            )
        for st_id, _, _ in orphan_studies:
            session.add(
                FolderItem(
                    folder_id=recovery.id,
                    resource_kind="study",
                    resource_id=st_id,
                )
            )
        session.commit()
        print(
            f"\ndone. created folder {recovery.id} {folder_name!r}; linked "
            f"{len(orphan_docs)} doc(s) + {len(orphan_studies)} study(s)"
        )


if __name__ == "__main__":
    main()
