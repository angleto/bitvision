"""Backfill ``content_sha256`` on pre-0038 clinical_event_attachments
and auto-reconcile them against the patient Drive.

Attachment rows created before migration 0038 have ``content_sha256 =
NULL`` (the column did not exist). This script streams each such blob
from S3, computes its SHA-256, stores it, then runs the same reconcile
pass the upload endpoint now runs: if a byte-identical document already
lives in the patient's Drive, a system-authored
``clinical_event_documents`` link is created so the event points at the
curated document.

Idempotent and resumable: rows that already carry a hash are skipped,
and the per-attachment commit means a re-run only fills the remaining
gaps. ``--dry-run`` prints the plan without writing. ``--patient-id``
scopes the sweep to one patient.

Run from the backend package, e.g.::

    uv run python scripts/backfill_event_attachment_hashes.py --dry-run
    uv run python scripts/backfill_event_attachment_hashes.py --patient-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import uuid

from sqlalchemy import select

from bvphoenix.config import get_settings
from bvphoenix.db.models import ClinicalEvent, ClinicalEventAttachment
from bvphoenix.db.session import SessionFactory
from bvphoenix.services.clinical_event_documents import reconcile_attachment
from bvphoenix.storage import get_s3_storage


async def _run(patient_id: uuid.UUID | None, dry_run: bool) -> None:
    settings = get_settings()
    storage = get_s3_storage()
    hashed = 0
    linked = 0
    skipped = 0
    async with SessionFactory() as session:
        q = select(ClinicalEventAttachment).where(
            ClinicalEventAttachment.deleted_at.is_(None),
            ClinicalEventAttachment.content_sha256.is_(None),
        )
        if patient_id is not None:
            q = q.where(ClinicalEventAttachment.patient_id == patient_id)
        atts = (await session.execute(q)).scalars().all()
        print(f"{len(atts)} attachment(s) without content_sha256", flush=True)

        for att in atts:
            try:
                data = await asyncio.to_thread(
                    storage.get_object_bytes,
                    bucket=settings.s3_bucket_raw,
                    key=att.storage_key,
                )
            except Exception as exc:
                skipped += 1
                print(f"  SKIP {att.id}: cannot fetch bytes ({exc})", flush=True)
                continue

            sha = hashlib.sha256(data).hexdigest()
            print(f"  {att.id} {att.filename!r} -> {sha[:12]}…", flush=True)
            if dry_run:
                continue

            att.content_sha256 = sha
            hashed += 1
            ev = (
                await session.execute(select(ClinicalEvent).where(ClinicalEvent.id == att.event_id))
            ).scalar_one_or_none()
            if ev is not None:
                link = await reconcile_attachment(session, event=ev, attachment=att)
                if link is not None:
                    linked += 1
                    print(f"    linked -> document {link.document_id}", flush=True)
            # Commit per row so the sweep is resumable on interruption.
            await session.commit()

    print(
        f"done: hashed={hashed} linked={linked} skipped={skipped} (dry_run={dry_run})",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patient-id", type=uuid.UUID, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned hashes/links without writing.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.patient_id, args.dry_run))


if __name__ == "__main__":
    main()
