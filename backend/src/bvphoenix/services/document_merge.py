"""Merge two or more patient documents into a primary (Sprint 3, ADR 0017).

Use case: the agent identifies that documents D1, D2 are duplicates of
P1 (e.g. same scan uploaded three times by mistake). The merge moves
the binary attachments of D1, D2 under P1 (per ADR 0017 — file
ownership transfer, no reference counting) and soft-deletes the
duplicates.

Constraints:

* All documents (primary + duplicates) must belong to the same patient.
* Duplicates must currently be live (no tombstones).
* The primary must not be in the duplicate set.

The function returns a summary that the API caller forwards to the
audit log so the forensic trail records every file transfer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    Document,
    DocumentFile,
)

_DEFAULT_PURGE_AFTER_DAYS = 30


class DocumentMergeError(ValueError):
    """Common base for merge precondition failures."""


@dataclass(slots=True)
class FileTransferRecord:
    file_id: uuid.UUID
    from_document_id: uuid.UUID
    to_document_id: uuid.UUID


@dataclass(slots=True)
class MergeResult:
    primary_id: uuid.UUID
    duplicate_ids: list[uuid.UUID]
    files_transferred: list[FileTransferRecord] = field(default_factory=list)
    files_orphaned: list[uuid.UUID] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "primary_id": str(self.primary_id),
            "duplicate_ids": [str(d) for d in self.duplicate_ids],
            "files_transferred": [
                {
                    "file_id": str(t.file_id),
                    "from_document_id": str(t.from_document_id),
                    "to_document_id": str(t.to_document_id),
                }
                for t in self.files_transferred
            ],
            "files_orphaned": [str(f) for f in self.files_orphaned],
        }


async def merge_documents(
    db: AsyncSession,
    *,
    primary: Document,
    duplicate_ids: list[uuid.UUID],
    preserve_files_as_attachments: bool,
    reason: str | None,
    actor_subject_id: uuid.UUID | None,
) -> MergeResult:
    """Run the merge pipeline.

    The caller owns the surrounding HTTP / permission gates and the
    final ``db.commit()``. This function flushes but does not commit.
    """
    if not duplicate_ids:
        raise DocumentMergeError("duplicate_ids cannot be empty")
    if primary.id in duplicate_ids:
        raise DocumentMergeError("primary document cannot be in the duplicate set")
    if primary.deleted_at is not None:
        raise DocumentMergeError("primary document is already soft-deleted")

    duplicates = (
        (
            await db.execute(
                select(Document).where(
                    Document.id.in_(duplicate_ids),
                    Document.patient_id == primary.patient_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(duplicates) != len(set(duplicate_ids)):
        raise DocumentMergeError(
            "one or more duplicates are missing or belong to a different patient"
        )
    if any(d.deleted_at is not None for d in duplicates):
        raise DocumentMergeError("duplicates must be live; soft-deleted documents cannot be merged")

    files_transferred: list[FileTransferRecord] = []
    files_orphaned: list[uuid.UUID] = []

    for d in duplicates:
        files = (
            (await db.execute(select(DocumentFile).where(DocumentFile.document_id == d.id)))
            .scalars()
            .all()
        )
        if preserve_files_as_attachments:
            # Bump every file's document_id to the primary; keep the
            # original ``sequence`` semantics by appending after the
            # current max.
            current_max = (
                await db.execute(
                    select(DocumentFile.sequence)
                    .where(DocumentFile.document_id == primary.id)
                    .order_by(DocumentFile.sequence.desc())
                    .limit(1)
                )
            ).scalar()
            offset = (current_max or 0) + 1
            for i, f in enumerate(files):
                files_transferred.append(
                    FileTransferRecord(
                        file_id=f.id,
                        from_document_id=d.id,
                        to_document_id=primary.id,
                    )
                )
                await db.execute(
                    update(DocumentFile)
                    .where(DocumentFile.id == f.id)
                    .values(document_id=primary.id, sequence=offset + i)
                )
        else:
            # Caller opted to drop the duplicates' files: leave them
            # parented to the duplicate and let the purge worker reap
            # them after the retention window.
            files_orphaned.extend(f.id for f in files)

        # Soft-delete the duplicate.
        now = datetime.now(UTC)
        d.deleted_at = now
        d.purge_after = now + timedelta(days=_DEFAULT_PURGE_AFTER_DAYS)
        d.delete_reason = reason or f"merged into {primary.id}"

    await db.flush()

    return MergeResult(
        primary_id=primary.id,
        duplicate_ids=list(duplicate_ids),
        files_transferred=files_transferred,
        files_orphaned=files_orphaned,
    )


__all__ = [
    "DocumentMergeError",
    "FileTransferRecord",
    "MergeResult",
    "merge_documents",
]
