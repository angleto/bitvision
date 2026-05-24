"""DTO mappers for patient documents.

The F12 chain (``services/versioning``) needs a serialisable snapshot
of a document row plus its attached files. The mapper lives in
``services/`` so both the REST handlers in ``api/patients`` and the
bulk-update service in ``services/document_bulk_update`` can call it
without the latter having to lazy-import out of the api layer to
dodge a circular dependency.

Sister mapper for :class:`Patient` (``_patient_versioning_payload``)
stays under ``api/patients`` because it is only consumed by the patient
PATCH route; if a non-api caller ever needs it, move it here too.
"""

from __future__ import annotations

from bvphoenix.db.models import Document, DocumentFile


def document_versioning_payload(d: Document, files: list[DocumentFile]) -> dict:
    """Snapshot of a patient document for the F12 versioning chain.

    Records metadata + the list of S3 keys + content types for attached
    files, but not the binary content (that lives in S3 and is
    immutable per key). Inline text payload is included verbatim so a
    text-only document is fully self-contained in the commit.
    """
    return {
        "id": str(d.id),
        "patient_id": str(d.patient_id),
        "document_type": d.kind_id,
        "title": d.title,
        "text": d.text,
        "document_date": d.document_date.isoformat() if d.document_date else None,
        "file_s3_key": d.file_s3_key,
        "file_content_type": d.file_content_type,
        "files": [
            {
                "sequence": f.sequence,
                "file_s3_key": f.file_s3_key,
                "file_content_type": f.file_content_type,
                "original_filename": f.original_filename,
                "size_bytes": f.size_bytes,
            }
            for f in sorted(files, key=lambda x: x.sequence)
        ],
        "uploaded_by_subject_id": (
            str(d.uploaded_by_subject_id) if d.uploaded_by_subject_id else None
        ),
        "schema_version": 1,
    }


__all__ = ["document_versioning_payload"]
