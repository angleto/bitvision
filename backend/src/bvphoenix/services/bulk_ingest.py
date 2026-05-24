"""Bulk ingest pipeline — runs from a worker against files already
staged in the raw S3 bucket.

The HTTP endpoint (``api/bulk_upload``) only stages multipart bytes
into ``_ingest_jobs/<job_id>/`` and enqueues the
``ingest_bulk_files`` arq task; this module is what that task calls
to do the heavy work (DICOMDIR-aware DICOM ingest + non-DICOM
document creation).

Splitting the logic out of the API handler lets the caller (the
worker) own the long-running session and report progress through a
callback hooked into ``services.jobs.update_progress``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    Document,
    Folder,
    FolderItem,
    Patient,
    Subject,
)
from bvphoenix.services.consent_auto import ensure_tier_consents
from bvphoenix.services.dicom_ingest import DicomIngestor, has_dicm_preamble
from bvphoenix.storage import get_s3_storage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedFile:
    """A file already uploaded into the staging prefix."""

    relative_path: str
    filename: str
    s3_key: str  # e.g. ``_ingest_jobs/<job_id>/0042.bin``


@dataclass(slots=True)
class IngestSkipped:
    filename: str
    reason: str


@dataclass(slots=True)
class StudyOut:
    id: str
    name: str
    series_count: int


@dataclass(slots=True)
class DocumentOut:
    id: str
    name: str
    document_type: str
    kind: str


@dataclass(slots=True)
class IngestSummary:
    studies_created: list[StudyOut] = field(default_factory=list)
    documents_created: list[DocumentOut] = field(default_factory=list)
    skipped: list[IngestSkipped] = field(default_factory=list)
    dicomdir_found: bool = False
    total_files: int = 0
    # IDs of every Series the ingest touched (created or updated).
    # The worker uses this list to enqueue ``pack_volume`` per series
    # so the Next.js viewer doesn't have to wait for on-demand packing
    # the first time a user opens the study.
    series_ids: list[str] = field(default_factory=list)


# Stage labels surface to the frontend as ``Job.stage`` — keep them
# short and stable; the UI maps them to localised strings.
STAGE_DOWNLOAD = "downloading"
STAGE_INGEST_DICOM = "ingest_dicom"
STAGE_INGEST_OTHER = "ingest_other"
STAGE_FINALIZE = "finalize"


# Progress callback shape:  (done, total, stage_label) -> awaitable
ProgressCallback = Callable[[int, int, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Internal helpers (mirror what api/bulk_upload.py used to do inline)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _VFile:
    relative_path: str
    filename: str
    data: bytes


# Files matched by these patterns are vendor-side noise that almost
# never carry clinical content: Windows autorun, OS metadata, embedded
# proprietary viewers, lookup tables, shortcuts. Skipped at the staging
# phase so they never make it past classify and don't bloat the
# fascicolo with junk Document rows. The match is intentionally
# narrow — anything ambiguous (eg ``*.txt``) is left to the regular
# classifier so a real patient report doesn't get dropped.
_VENDOR_NOISE_FILENAMES: frozenset[str] = frozenset(
    {
        "autorun.inf",
        "desktop.ini",
        "thumbs.db",
        ".ds_store",
        "luttable.xml",
        "windowsindex.dat",
    }
)
_VENDOR_NOISE_SUFFIXES: tuple[str, ...] = (
    ".lut",
    ".lnk",
    ".scr",
    ".bat",
    ".cmd",
    ".vbs",
)


def _is_vendor_noise(filename: str, relative_path: str) -> bool:
    """True for files clearly produced by the CD authoring tool."""
    name_lower = filename.lower()
    if name_lower in _VENDOR_NOISE_FILENAMES:
        return True
    if name_lower.endswith(_VENDOR_NOISE_SUFFIXES):
        return True
    # Bundled viewer EXE/DLL — almost every clinical CD ships one.
    return bool(name_lower.endswith((".exe", ".dll")) and "viewer" in relative_path.lower())


def _fallback_classify(data: bytes, filename: str) -> str:
    if has_dicm_preamble(data):
        return "dicom"
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image"
    lower = filename.lower()
    if lower.endswith((".dcm", ".dicom")):
        return "dicom"
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif")):
        return "image"
    if lower.endswith((".txt", ".md", ".csv", ".json", ".xml", ".html")):
        return "text"
    return "other"


def _classify(data: bytes, filename: str) -> str:
    try:
        from bvphoenix.services.file_classify import classify_file  # type: ignore[import-not-found]
    except ImportError:
        return _fallback_classify(data, filename)
    try:
        result = classify_file(data, filename)
    except Exception:
        return _fallback_classify(data, filename)
    if isinstance(result, str):
        return result
    kind = getattr(result, "kind", None) or (
        result.get("kind") if isinstance(result, dict) else None
    )
    return kind or _fallback_classify(data, filename)


def _guess_document_type(filename: str) -> str:
    # v3: returns a kind_id drawn from the document_kinds catalog
    # (seeded in migration 0072). Validation is delegated to the FK
    # on documents.kind_id; if the heuristic returns an unknown value
    # the DB rejects the insert with a 422.
    try:
        from bvphoenix.services.document_classify import (  # type: ignore[import-not-found]
            guess_document_type,
        )
    except ImportError:
        return "unclassified"
    try:
        dt = guess_document_type(filename, None)
    except Exception:
        return "unclassified"
    return dt or "unclassified"


def _parse_dicomdir_entries(blob: bytes) -> list[dict] | None:
    try:
        from bvphoenix.services.dicomdir import parse_dicomdir  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        entries = parse_dicomdir(blob)
    except Exception:
        return None
    out: list[dict] = []
    for e in entries or []:
        if isinstance(e, dict):
            rp = e.get("relative_path") or e.get("path")
        else:
            rp = getattr(e, "relative_path", None) or getattr(e, "path", None)
        if rp:
            out.append({"relative_path": str(rp).replace("\\", "/")})
    return out


def _extract_dicomdir(
    files: list[_VFile],
) -> tuple[_VFile | None, list[_VFile]]:
    dd: _VFile | None = None
    rest: list[_VFile] = []
    for vf in files:
        tail = vf.relative_path.rsplit("/", 1)[-1]
        if dd is None and tail.upper() == "DICOMDIR":
            dd = vf
            continue
        rest.append(vf)
    return dd, rest


def _match_by_path(entries: list[dict], files: list[_VFile]) -> tuple[list[_VFile], list[_VFile]]:
    if not entries:
        return [], files
    by_tail: dict[str, _VFile] = {}
    for vf in files:
        norm = vf.relative_path.lower().replace("\\", "/")
        parts = norm.split("/")
        for i in range(len(parts)):
            by_tail.setdefault("/".join(parts[i:]), vf)
    matched: list[_VFile] = []
    seen: set[int] = set()
    for entry in entries:
        rel = str(entry.get("relative_path", "")).lower().replace("\\", "/").lstrip("/")
        if not rel:
            continue
        hit = by_tail.get(rel)
        if hit is None:
            parts = rel.split("/")
            for i in range(1, len(parts)):
                hit = by_tail.get("/".join(parts[i:]))
                if hit is not None:
                    break
        if hit is not None and id(hit) not in seen:
            matched.append(hit)
            seen.add(id(hit))
    unmatched = [vf for vf in files if id(vf) not in seen]
    return matched, unmatched


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def process_bulk_ingest(
    db: AsyncSession,
    *,
    staged_files: list[StagedFile],
    owner_subject_id: uuid.UUID,
    patient_id: uuid.UUID | None,
    folder_id: uuid.UUID | None,
    tier: str,
    progress_cb: ProgressCallback | None = None,
) -> IngestSummary:
    """Run the full ingest against files staged in the raw S3 bucket.

    Loose contract:
    - The worker owns the ``db`` session for its full lifetime; this
      function does its own ``commit()`` at the end.
    - ``progress_cb`` (optional) is invoked at each stage transition
      and roughly every 5 files within a stage.
    - The function never raises ``HTTPException``; surface failures
      via the returned ``IngestSummary.skipped`` list and let the
      caller flip the Job to ``failed`` only on truly fatal errors
      (missing patient when one was promised, etc).
    """
    settings = get_settings()
    storage = get_s3_storage()

    summary = IngestSummary()
    summary.total_files = len(staged_files)

    owner = (await db.execute(select(Subject).where(Subject.id == owner_subject_id))).scalar_one()
    patient: Patient | None = None
    if patient_id is not None:
        patient = (
            await db.execute(select(Patient).where(Patient.id == patient_id))
        ).scalar_one_or_none()
    folder: Folder | None = None
    if folder_id is not None:
        folder = (
            await db.execute(select(Folder).where(Folder.id == folder_id))
        ).scalar_one_or_none()

    # ---- Stage 1: pull file bytes back from staging S3 keys ----
    # Skip vendor noise (autorun, README, lookup tables, embedded
    # viewer binaries) before the download so we don't burn bandwidth
    # / classifier CPU on files we'd discard anyway. The skipped list
    # surfaces them to the operator so a false positive can be
    # noticed.
    if progress_cb:
        await progress_cb(0, summary.total_files, STAGE_DOWNLOAD)
    vfiles: list[_VFile] = []
    for i, sf in enumerate(staged_files):
        if _is_vendor_noise(sf.filename, sf.relative_path):
            summary.skipped.append(
                IngestSkipped(filename=sf.filename, reason="vendor noise (auto-skipped)")
            )
            continue
        try:
            data = await asyncio.to_thread(
                storage.get_object_bytes,
                bucket=settings.s3_bucket_raw,
                key=sf.s3_key,
            )
        except Exception as exc:
            summary.skipped.append(
                IngestSkipped(filename=sf.filename, reason=f"staging fetch: {exc}")
            )
            continue
        vfiles.append(_VFile(relative_path=sf.relative_path, filename=sf.filename, data=data))
        if progress_cb and ((i + 1) % 5 == 0 or (i + 1) == summary.total_files):
            await progress_cb(i + 1, summary.total_files, STAGE_DOWNLOAD)

    # ---- Stage 2: classify (DICOMDIR-first if present) ----
    dd, remaining = _extract_dicomdir(vfiles)
    summary.dicomdir_found = dd is not None
    entries = _parse_dicomdir_entries(dd.data) if dd else None

    dicom_files: list[_VFile] = []
    other_files: list[_VFile] = []
    if entries:
        matched, unclassified = _match_by_path(entries, remaining)
        dicom_files.extend(matched)
    else:
        unclassified = remaining
    for vf in unclassified:
        kind = _classify(vf.data, vf.filename)
        (dicom_files if kind == "dicom" else other_files).append(vf)

    # ---- Stage 3: DICOM ingest (batched, parallel S3 uploads) ----
    if progress_cb:
        await progress_cb(0, len(dicom_files), STAGE_INGEST_DICOM)
    ingestor = DicomIngestor(
        db=db,
        storage=storage,
        bucket=settings.s3_bucket_raw,
        owner=owner,
        tier=tier,
        is_public=False,
    )

    # Use the optimised bulk path: parses headers in parallel, runs S3
    # uploads in a bounded asyncio.gather, and bulk-inserts Instance
    # rows per series. Replaces a per-file loop that flushed once per
    # blob — measured ~6× speedup on a 1500-file CT series in
    # production.
    async def _dicom_progress(done: int) -> None:
        if progress_cb is not None:
            await progress_cb(done, len(dicom_files), STAGE_INGEST_DICOM)

    bulk_stats = await ingestor.bulk_ingest_blobs(
        [vf.data for vf in dicom_files],
        max_concurrency=8,
        on_progress=_dicom_progress,
    )
    # Map bulk_ingest errors back to the per-file VirtualFile filenames
    # so the user-facing skipped list remains useful.
    for err in bulk_stats.errors:
        # err.filename is "blob[<idx>]"; recover the real filename.
        try:
            idx = int(err.filename[len("blob[") : -1])
            real_name = dicom_files[idx].filename if 0 <= idx < len(dicom_files) else err.filename
        except (ValueError, IndexError):
            real_name = err.filename
        summary.skipped.append(IngestSkipped(filename=real_name, reason=err.message))

    if patient is not None:
        for study in ingestor.touched_studies.values():
            study.patient_id = patient.id

    await ingestor.finalize()

    # Capture series IDs so the worker can fire pack_volume per
    # series — the viewer's /volume.raw endpoint can pack on demand
    # but pre-packing avoids the cold-open spinner and keeps memory
    # spikes off the request thread.
    summary.series_ids = [str(s.id) for s in ingestor.touched_series.values()]

    series_per_study: dict[uuid.UUID, int] = {}
    for series in ingestor.touched_series.values():
        series_per_study[series.study_id] = series_per_study.get(series.study_id, 0) + 1
    for study in ingestor.touched_studies.values():
        summary.studies_created.append(
            StudyOut(
                id=str(study.id),
                name=study.study_description or study.study_instance_uid,
                series_count=series_per_study.get(study.id, 0),
            )
        )

    # ---- Stage 4: non-DICOM documents ----
    if progress_cb:
        await progress_cb(0, len(other_files), STAGE_INGEST_OTHER)
    for i, vf in enumerate(other_files):
        kind = _classify(vf.data, vf.filename)
        if kind not in ("pdf", "image", "text"):
            summary.skipped.append(
                IngestSkipped(filename=vf.filename, reason=f"unsupported kind: {kind}")
            )
            continue
        if patient is None:
            summary.skipped.append(
                IngestSkipped(
                    filename=vf.filename,
                    reason="non-dicom file requires patient_id",
                )
            )
            continue
        sha = hashlib.sha256(vf.data).hexdigest()
        existing = (
            await db.execute(
                select(Document).where(
                    Document.patient_id == patient.id,
                    Document.content_sha256 == sha,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            summary.skipped.append(
                IngestSkipped(
                    filename=vf.filename,
                    reason=f"already uploaded ({existing.title})",
                )
            )
            continue
        document_kind = _guess_document_type(vf.filename)
        # v3: classifier produces the kind; provenance + authority
        # are derived from the file shape. Sensible defaults:
        #   - phone-ish images → photo_of_paper
        #   - scans (pdf with image-only content) → scanned_paper
        #     (we don't OCR-detect that here; default scanned_paper for
        #      pdf-of-image, native PDF otherwise)
        #   - native text → digital_native_pdf for pdfs, manual_entry
        #     for everything else
        # The classifier and the provenance heuristic stay simple on
        # purpose: a richer pass is post-OCR, in a follow-up worker.
        if kind == "image":
            provenance = "photo_of_paper"
        elif kind == "pdf":
            provenance = "digital_native_pdf"
        else:
            provenance = "manual_entry"
        # Authority: every freshly uploaded document is treated as
        # ``original`` until the similarity-matching dedup pass demotes
        # it to ``derived`` (next phase). Conservative default — better
        # to lose a tiny bit of trust signal than to mark a real
        # original as derived.
        authority = "original"
        doc_id = uuid.uuid4()
        ext = vf.filename.rsplit(".", 1)[-1] if "." in vf.filename else "bin"
        s3_key = f"patient-docs/{patient.id}/{doc_id}.{ext}"
        await asyncio.to_thread(
            storage.upload_bytes,
            vf.data,
            bucket=settings.s3_bucket_raw,
            key=s3_key,
        )
        content_type = {
            "pdf": "application/pdf",
            "image": "image/jpeg" if ext.lower() in ("jpg", "jpeg") else "image/png",
            "text": "text/plain",
        }[kind]
        doc = Document(
            id=doc_id,
            patient_id=patient.id,
            uploaded_by_subject_id=owner.id,
            kind_id=document_kind,
            provenance_id=provenance,
            authority_id=authority,
            title=vf.filename,
            file_s3_key=s3_key,
            file_content_type=content_type,
            content_sha256=sha,
            # v3: defaults to content_sha256 at ingestion; the
            # similarity-matching pass updates it for confirmed copies
            # of an existing original.
            original_blob_hash=sha,
        )
        db.add(doc)
        summary.documents_created.append(
            DocumentOut(
                id=str(doc_id),
                name=vf.filename,
                document_type=document_kind,
                kind=kind,
            )
        )
        if progress_cb and ((i + 1) % 5 == 0 or (i + 1) == len(other_files)):
            await progress_cb(i + 1, len(other_files), STAGE_INGEST_OTHER)

    # ---- Stage 5: folder linking + tier consents + commit ----
    if progress_cb:
        await progress_cb(0, 1, STAGE_FINALIZE)
    if folder is not None:
        for s in summary.studies_created:
            db.add(
                FolderItem(
                    folder_id=folder.id,
                    resource_kind="study",
                    resource_id=uuid.UUID(s.id),
                )
            )
        # Without an entry the Document shows up in the fascicolo root,
        # not in the folder the user dropped the file onto.
        for d in summary.documents_created:
            db.add(
                FolderItem(
                    folder_id=folder.id,
                    resource_kind="document",
                    resource_id=uuid.UUID(d.id),
                )
            )

    if tier in ("t3", "t4") and summary.studies_created:
        await ensure_tier_consents(
            db,
            user_subject_id=owner.id,
            tier=tier,
            study_ids=[uuid.UUID(s.id) for s in summary.studies_created],
        )

    await db.commit()
    if progress_cb:
        await progress_cb(1, 1, STAGE_FINALIZE)

    return summary


def summary_to_dict(s: IngestSummary) -> dict[str, Any]:
    """Serialise an :class:`IngestSummary` to a JSON-friendly dict for
    storage in ``Job.input.result`` (or whatever surface the worker
    chooses to expose the result on)."""
    return {
        "studies_created": [
            {"id": st.id, "name": st.name, "series_count": st.series_count}
            for st in s.studies_created
        ],
        "documents_created": [
            {
                "id": d.id,
                "name": d.name,
                "document_type": d.document_type,
                "kind": d.kind,
            }
            for d in s.documents_created
        ],
        "skipped": [{"filename": k.filename, "reason": k.reason} for k in s.skipped],
        "dicomdir_found": s.dicomdir_found,
        "total_files": s.total_files,
        "series_ids": list(s.series_ids),
    }
