"""Patient Health Record (Fascicolo) export builder.

The ZIP-build logic and its serialization helpers live here so both
the synchronous HTTP route in :mod:`bvphoenix.api.patient_export`
and the async worker task ``export_patient_zip`` can share them.

The module is deliberately framework-agnostic: no FastAPI, no Arq.
It takes a session and a parsed includes set and returns
``(zip_bytes, manifest)``. Callers handle response shaping, S3
upload, audit logging, etc.

Two builder paths live side-by-side:

* :func:`build_export_zip` — legacy in-memory: assembles the whole ZIP
  into a ``BytesIO`` and returns the bytes. Kept for tests and callers
  that genuinely need the bytes back. Memory peaks at the full archive
  size; not safe for multi-GB DICOM exports.
* :func:`stream_export_to_s3` — streaming: walks the patient data,
  feeds a stream-zip iterator straight into an S3 multipart upload,
  reports progress via callback. Memory stays at the multipart part
  ceiling (default 8 MiB) regardless of archive size. This is the
  path the worker ``export_patient_zip`` uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import uuid
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from stream_zip import NO_COMPRESSION_64, ZIP_64, stream_zip

from bvphoenix.config import get_settings
from bvphoenix.db.models import (
    ClinicalEvent,
    ContentDocumentLink,
    Document,
    DocumentFile,
    Folder,
    FolderItem,
    ImagingStudy,
    Instance,
    Marker,
    Patient,
    ReportContent,
    Series,
    User,
)
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.file_ext import ext_for
from bvphoenix.services.permissions import (
    DOWNLOAD_DICOM,
    READ_ANNOTATIONS,
    READ_METADATA,
    can,
    effective_permissions_on_patient,
)
from bvphoenix.storage import get_s3_storage

logger = logging.getLogger(__name__)


ALLOWED_INCLUDES: frozenset[str] = frozenset(
    {"studies", "reports", "documents", "annotations", "dicom"}
)
DEFAULT_INCLUDES: tuple[str, ...] = (
    "studies",
    "reports",
    "documents",
    "annotations",
)


# ---------------------------------------------------------------------------
# Archive layout — ``flat`` (legacy UUID-keyed) vs ``tree`` (mirrors the
# patient's curated Folder tree with human-readable names).
# ---------------------------------------------------------------------------

LAYOUTS: frozenset[str] = frozenset({"flat", "tree"})
DEFAULT_LAYOUT = "flat"

# Characters a path component can never carry on the common filesystems
# (POSIX forbids ``/`` + NUL; we also strip ``\`` and ``:`` so the tree
# unzips cleanly on Windows / macOS). Everything else — accents, spaces,
# the em-dash the user types in folder names — is preserved.
_ILLEGAL_PATH_CHARS = re.compile(r"[/\\:\x00-\x1f]")


def _sanitize_component(name: str, *, maxlen: int = 120) -> str:
    """Make ``name`` safe as a single path segment without flattening
    its readability. Collapses whitespace, strips leading/trailing dots
    and spaces, caps the length so a pathological StudyDescription
    cannot blow past filesystem limits."""
    cleaned = _ILLEGAL_PATH_CHARS.sub("-", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".").strip()
    if len(cleaned) > maxlen:
        cleaned = cleaned[:maxlen].rstrip()
    # A name that survives only as separators (e.g. the input was all
    # control chars, now a run of ``-``) carries no information; fall
    # back rather than emit a folder literally named "--". Internal /
    # leading dashes in a real name are preserved.
    if not cleaned.strip(" .-_"):
        return "senza-nome"
    return cleaned


def _study_label(s: ImagingStudy) -> str:
    desc = (s.study_description or "").strip()
    if not desc:
        mods = " ".join(s.modalities or [])
        date = str(s.study_date) if s.study_date else ""
        desc = " ".join(p for p in (mods, date) if p).strip()
    return desc or f"studio {str(s.id)[:8]}"


def _series_label(n: int, s: Series) -> str:
    desc = (s.series_description or "").strip() or (s.modality or "").strip() or "serie"
    return f"serie_{n:03d}_{desc}"


def _document_label(d: Document) -> str:
    return (d.title or "").strip() or f"documento {str(d.id)[:8]}"


def _report_label(r: ReportContent) -> str:
    return (r.title or "").strip() or f"referto {str(r.id)[:8]}"


async def _build_folder_layout(
    db: AsyncSession, patient: Patient
) -> dict[tuple[str, uuid.UUID], str]:
    """Map ``(resource_kind, resource_id) -> POSIX folder path`` derived
    from the patient's curated Folder tree.

    The materialised root folder (``is_root``) contributes no path
    segment, so anything filed only under it lands at the archive root.
    When a resource is filed in more than one folder the deepest (most
    specific) path wins; ties break on the longer string so the choice
    is deterministic across reruns (keeps the cached-artifact dedup
    hash stable).
    """
    folders = list(
        (await db.execute(select(Folder).where(Folder.patient_id == patient.id))).scalars().all()
    )
    if not folders:
        return {}
    by_id: dict[uuid.UUID, Folder] = {f.id: f for f in folders}
    path_cache: dict[uuid.UUID, str] = {}

    def _folder_path(fid: uuid.UUID) -> str:
        cached = path_cache.get(fid)
        if cached is not None:
            return cached
        folder = by_id.get(fid)
        if folder is None or folder.is_root:
            path_cache[fid] = ""
            return ""
        parent = _folder_path(folder.parent_folder_id) if folder.parent_folder_id else ""
        seg = _sanitize_component(folder.name)
        full = f"{parent}/{seg}" if parent else seg
        path_cache[fid] = full
        return full

    rows = (
        await db.execute(
            select(
                FolderItem.resource_kind,
                FolderItem.resource_id,
                FolderItem.folder_id,
            ).where(FolderItem.folder_id.in_(list(by_id.keys())))
        )
    ).all()
    best: dict[tuple[str, uuid.UUID], str] = {}
    for kind, rid, fid in rows:
        if kind == "subfolder":
            continue
        path = _folder_path(fid)
        key = (kind, rid)
        cur = best.get(key)
        if cur is None or len(path) > len(cur):
            best[key] = path
    return best


class _ExportNamer:
    """Computes ZIP member paths for an export.

    ``flat`` (default) reproduces the historical UUID-keyed layout
    byte-for-byte, so existing callers and any cached artifact are
    unaffected. ``tree`` mirrors the patient's curated Folder tree:
    every study / document lands under the path of the folder that
    contains it, named by its clinical description / title, with
    OS-style `` (2)`` de-duplication so two members can never collide
    on one path (a duplicate member would corrupt the archive).

    Stateful: the de-dup bookkeeping means a single instance must build
    every member name for one export, and ``study_root`` must be called
    exactly once per study (cache the result; the studies branch and
    the markers branch share it).
    """

    def __init__(self, layout: str, folder_paths: dict[tuple[str, uuid.UUID], str]):
        self.tree = layout == "tree"
        self._paths = folder_paths
        self._used: dict[str, set[str]] = {}

    def _child(self, parent: str, name: str) -> str:
        used = self._used.setdefault(parent, set())
        candidate = name
        if candidate.casefold() in used:
            if "." in name:
                base, ext = name.rsplit(".", 1)
                suffix = f".{ext}"
            else:
                base, suffix = name, ""
            i = 2
            while True:
                candidate = f"{base} ({i}){suffix}"
                if candidate.casefold() not in used:
                    break
                i += 1
        used.add(candidate.casefold())
        return f"{parent}/{candidate}" if parent else candidate

    def _folder_of(self, kind: str, rid: uuid.UUID) -> str:
        return self._paths.get((kind, rid), "")

    def study_root(self, study: ImagingStudy) -> str:
        if not self.tree:
            return f"studies/{study.id}"
        return self._child(
            self._folder_of("study", study.id), _sanitize_component(_study_label(study))
        )

    def series_root(self, study_root: str, n: int, series: Series) -> str:
        if not self.tree:
            return f"{study_root}/series_{n}"
        return self._child(study_root, _sanitize_component(_series_label(n, series)))

    def series_manifest(self, series_root: str) -> str:
        return f"{series_root}/_serie.json" if self.tree else f"{series_root}/manifest.json"

    def instance(self, series_root: str, inst: Instance) -> str:
        if not self.tree:
            return f"{series_root}/{inst.sop_instance_uid}.dcm"
        num = inst.instance_number
        stem = f"{num:04d}" if num is not None else (inst.sop_instance_uid or "img")[-16:]
        return self._child(series_root, f"{stem}.dcm")

    def document_base(self, doc: Document) -> str:
        """Return the path stem for a document; callers append the
        extension (``.txt`` / ``.{ext}``) or a multi-file subdir."""
        if not self.tree:
            return f"documents/{doc.id}"
        return self._child(
            self._folder_of("document", doc.id), _sanitize_component(_document_label(doc))
        )

    def report_base(self, rep: ReportContent) -> str:
        if not self.tree:
            return f"reports/{rep.id}"
        return self._child(
            self._folder_of("report", rep.id), _sanitize_component(_report_label(rep))
        )

    def markers(self, study: ImagingStudy, study_root: str) -> str:
        if not self.tree:
            return f"markers/{study.id}.json"
        return self._child(study_root, "_annotazioni.json")


def patient_to_dict(p: Patient) -> dict[str, Any]:
    # v3: tax_id + external_id columns dropped; the legacy export
    # keys are derived from the ``external_identifiers`` JSONB so
    # downstream consumers (GDPR exports, fascicolo bundles) keep
    # the shape they expected. The full identifier list is also
    # surfaced for v3-aware consumers.
    legacy_cf: str | None = None
    legacy_external_id: str | None = None
    for entry in p.external_identifiers or []:
        if not isinstance(entry, dict):
            continue
        v = entry.get("value")
        if not isinstance(v, str):
            continue
        if entry.get("type") == "fiscal-code" and legacy_cf is None:
            legacy_cf = v
        elif entry.get("type") == "MR" and legacy_external_id is None:
            legacy_external_id = v
    return {
        "id": str(p.id),
        "display_name": p.display_name,
        "external_id": legacy_external_id,
        "birth_date": str(p.birth_date) if p.birth_date else None,
        "sex": p.sex,
        "tax_id": legacy_cf,
        "external_identifiers": list(p.external_identifiers or []),
        "phone": p.phone,
        "email": p.email,
        "address": p.address,
        "blood_type": p.blood_type,
        "allergies": p.allergies,
        "notes": p.notes,
        "created_at": p.created_at.isoformat(),
    }


def study_to_dict(s: ImagingStudy) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "study_instance_uid": s.study_instance_uid,
        "study_description": s.study_description,
        "study_date": str(s.study_date) if s.study_date else None,
        "modalities": list(s.modalities or []),
        "is_public": s.is_public,
        "created_at": s.created_at.isoformat(),
    }


def series_to_dict(s: Series) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "series_instance_uid": s.series_instance_uid,
        "series_number": s.series_number,
        "modality": s.modality,
        "body_part_examined": s.body_part_examined,
        "series_description": s.series_description,
        "received_instance_count": s.received_instance_count,
    }


def instance_to_dict(i: Instance) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "sop_instance_uid": i.sop_instance_uid,
        "sop_class_uid": i.sop_class_uid,
        "instance_number": i.instance_number,
        "size_bytes": i.size_bytes,
        "content_sha256": i.content_sha256,
    }


def marker_to_dict(m: Marker) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "target_kind": m.target_kind,
        "target_id": str(m.target_id),
        "author_subject_id": (str(m.author_subject_id) if m.author_subject_id else None),
        "author_kind": m.author_kind,
        "model_id": m.model_id,
        "provider": m.provider,
        "kind": m.kind,
        "geometry": m.geometry,
        "computed": m.computed,
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
    }


async def build_export_zip(
    db: AsyncSession,
    user: User | None,
    patient: Patient,
    includes: set[str],
) -> tuple[bytes, dict[str, Any]]:
    """Assemble the export ZIP and return ``(bytes, manifest)``.

    Permission model (mirrors the sync route in
    :func:`bvphoenix.api.patient_export.export_patient`):

    * READ on the patient must already have been checked by the
      caller; this function does not re-check patient-level access.
    * Each study is re-checked for ``READ_METADATA`` and
      ``READ_ANNOTATIONS`` before its metadata / reports / markers
      are included.
    * Raw DICOM is included only when ``"dicom"`` is in ``includes``
      AND the caller has ``DOWNLOAD_DICOM`` on the patient. If
      ``"dicom"`` is requested without the grant, the function raises
      ``HTTPException(403)`` so the API layer can surface the same
      4xx response the sync route produces.
    """
    storage = get_s3_storage()
    settings = get_settings()
    buf = io.BytesIO()
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "patient": patient_to_dict(patient),
        "includes": sorted(includes),
        "counts": {
            "studies": 0,
            "reports": 0,
            "documents": 0,
            "annotations": 0,
            "dicom_files": 0,
        },
        "studies": [],
        "reports": [],
        "documents": [],
        "annotations": [],
    }

    patient_perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    include_dicom = "dicom" in includes and DOWNLOAD_DICOM in patient_perms

    if "dicom" in includes and not include_dicom:
        raise HTTPException(
            status_code=403,
            detail="download:dicom grant required to include DICOM files",
        )

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        studies: list[ImagingStudy] = list(
            (
                await db.execute(
                    select(ImagingStudy)
                    .where(ImagingStudy.patient_id == patient.id)
                    .order_by(ImagingStudy.study_date.desc().nullslast())
                )
            )
            .scalars()
            .all()
        )

        readable_studies: list[ImagingStudy] = []
        for study in studies:
            if not await can(db, user=user, action=READ_METADATA, study=study):
                continue
            readable_studies.append(study)

        study_ids = [s.id for s in readable_studies]

        if "studies" in includes or include_dicom:
            for study in readable_studies:
                study_dict = study_to_dict(study)
                study_series_list: list[dict[str, Any]] = []

                study_dicom_ok = include_dicom and await can(
                    db, user=user, action=DOWNLOAD_DICOM, study=study
                )

                series_rows: list[Series] = list(
                    (
                        await db.execute(
                            select(Series)
                            .where(Series.study_id == study.id)
                            .order_by(Series.series_number.asc().nullslast())
                        )
                    )
                    .scalars()
                    .all()
                )
                for n, series in enumerate(series_rows, start=1):
                    instances: list[Instance] = list(
                        (
                            await db.execute(
                                select(Instance)
                                .where(Instance.series_id == series.id)
                                .order_by(Instance.instance_number.asc().nullslast())
                            )
                        )
                        .scalars()
                        .all()
                    )
                    series_dict = series_to_dict(series)
                    series_dict["instances"] = [instance_to_dict(i) for i in instances]
                    study_series_list.append(series_dict)

                    zf.writestr(
                        f"studies/{study.id}/series_{n}/manifest.json",
                        json.dumps(series_dict, indent=2, ensure_ascii=False),
                    )

                    if study_dicom_ok:
                        for inst in instances:
                            try:
                                data = storage.get_object_bytes(
                                    bucket=inst.s3_bucket, key=inst.s3_key
                                )
                            except Exception:
                                continue
                            zf.writestr(
                                f"studies/{study.id}/series_{n}/{inst.sop_instance_uid}.dcm",
                                data,
                            )
                            manifest["counts"]["dicom_files"] += 1

                study_dict["series"] = study_series_list
                manifest["studies"].append(study_dict)
                manifest["counts"]["studies"] += 1

        if "reports" in includes:
            # v3: Report (study-scoped) was replaced by ReportContent
            # (clinical-event-scoped, n:m with documents). We pull all
            # ReportContent rows for the patient via the ClinicalEvent
            # join so the export covers narratives that aren't tied to
            # any imaging study (lab summaries, discharge letters, ...).
            #
            # Each row emits a manifest entry plus a ``.md`` file with
            # the human-readable narrative. The PDF/image artefacts that
            # back the report are exported under documents/ via the
            # ContentDocumentLink → Document path: we record the linked
            # document ids so the consumer can correlate the two trees.
            report_rows: list[ReportContent] = list(
                (
                    await db.execute(
                        select(ReportContent)
                        .join(
                            ClinicalEvent,
                            ClinicalEvent.id == ReportContent.clinical_event_id,
                        )
                        .where(ClinicalEvent.patient_id == patient.id)
                        .order_by(ReportContent.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for rep in report_rows:
                # Linked documents (any role) — recorded for traceability
                # so the consumer can find the source artefacts inside
                # the documents/ tree of the same archive.
                linked_doc_ids: list[str] = list(
                    (
                        await db.execute(
                            select(ContentDocumentLink.document_id).where(
                                ContentDocumentLink.report_content_id == rep.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                entry: dict[str, Any] = {
                    "id": str(rep.id),
                    "clinical_event_id": str(rep.clinical_event_id),
                    "authority": rep.authority_id,
                    "status": rep.status,
                    "language": rep.language,
                    "title": rep.title,
                    "author_kind": rep.author_kind,
                    "created_by_subject_id": (
                        str(rep.created_by_subject_id) if rep.created_by_subject_id else None
                    ),
                    "model_id": rep.model_id,
                    "provider": rep.provider,
                    "linked_document_ids": [str(d) for d in linked_doc_ids],
                    "created_at": rep.created_at.isoformat(),
                    "updated_at": rep.updated_at.isoformat(),
                }
                # Emit the narrative as a Markdown file inside reports/.
                # Filename keys on the report id (UUID) — stable and
                # patient-unique, no collisions with the legacy
                # study-scoped naming.
                narrative = rep.narrative_md or ""
                zf.writestr(f"reports/{rep.id}.md", narrative)
                entry["narrative_path"] = f"reports/{rep.id}.md"
                # Synthesis carries findings/recommendations as separate
                # markdown chunks; keep them in the manifest as text and
                # also dump them as files for offline reading.
                if rep.findings_md:
                    zf.writestr(f"reports/{rep.id}.findings.md", rep.findings_md)
                    entry["findings_path"] = f"reports/{rep.id}.findings.md"
                if rep.recommendations_md:
                    zf.writestr(
                        f"reports/{rep.id}.recommendations.md",
                        rep.recommendations_md,
                    )
                    entry["recommendations_path"] = f"reports/{rep.id}.recommendations.md"
                manifest["reports"].append(entry)
                manifest["counts"]["reports"] += 1

        if ("annotations" in includes or "markers" in includes) and study_ids:
            for study in readable_studies:
                if not await can(db, user=user, action=READ_ANNOTATIONS, study=study):
                    continue
                marker_rows: list[Marker] = list(
                    (
                        await db.execute(
                            select(Marker)
                            .where(
                                Marker.target_kind == "study",
                                Marker.target_id == study.id,
                            )
                            .order_by(Marker.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not marker_rows:
                    continue
                payload = [marker_to_dict(m) for m in marker_rows]
                zf.writestr(
                    f"markers/{study.id}.json",
                    json.dumps(payload, indent=2, ensure_ascii=False),
                )
                manifest.setdefault("markers", []).extend(payload)
                manifest["counts"]["annotations"] += len(payload)

        if "documents" in includes:
            # v3: filter out soft-deleted documents. The 3-axis taxonomy
            # (kind/provenance/authority) replaces the legacy
            # ``document_type`` column. Per-document binary lives either
            # on the legacy ``file_s3_key`` *or* on N child rows in
            # ``document_files`` — both paths are emitted.
            docs: list[Document] = list(
                (
                    await db.execute(
                        select(Document)
                        .where(
                            Document.patient_id == patient.id,
                            Document.deleted_at.is_(None),
                        )
                        .order_by(Document.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for doc in docs:
                entry: dict[str, Any] = {
                    "id": str(doc.id),
                    "kind": doc.kind_id,
                    "provenance": doc.provenance_id,
                    "authority": doc.authority_id,
                    "title": doc.title,
                    "text": doc.text,
                    "document_date": (str(doc.document_date) if doc.document_date else None),
                    "created_at": doc.created_at.isoformat(),
                    "files": [],
                }
                if doc.text:
                    zf.writestr(f"documents/{doc.id}.txt", doc.text)
                if doc.file_s3_key:
                    # Documents canonically live in ``s3_bucket_raw``
                    # (see patients.py:1970 — the legacy
                    # ``s3_bucket_derivatives`` write path was a bug
                    # that left documents invisible to OCR + binary
                    # download). Read from raw to match.
                    ext = ext_for(doc.file_content_type, doc.file_s3_key)
                    try:
                        data = storage.get_object_bytes(
                            bucket=settings.s3_bucket_raw,
                            key=doc.file_s3_key,
                        )
                        zf.writestr(f"documents/{doc.id}.{ext}", data)
                        entry["file_path"] = f"documents/{doc.id}.{ext}"
                        entry["file_content_type"] = doc.file_content_type
                    except Exception:
                        entry["file_error"] = "blob unavailable"

                # Multi-file children (DocumentFile). A document may
                # have N attached files — e.g. a paper report scanned
                # into 5 JPEGs. Each is emitted under
                # documents/{doc_id}/{seq}-{original_filename} so the
                # consumer can navigate by document.
                doc_files: list[DocumentFile] = list(
                    (
                        await db.execute(
                            select(DocumentFile)
                            .where(DocumentFile.document_id == doc.id)
                            .order_by(DocumentFile.sequence, DocumentFile.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                for df in doc_files:
                    file_entry: dict[str, Any] = {
                        "id": str(df.id),
                        "sequence": df.sequence,
                        "original_filename": df.original_filename,
                        "content_type": df.file_content_type,
                        "size_bytes": df.size_bytes,
                    }
                    safe_name = (df.original_filename or "").replace("/", "_") or (
                        f"{df.sequence}.{ext_for(df.file_content_type, df.file_s3_key)}"
                    )
                    member = f"documents/{doc.id}/{df.sequence:03d}-{safe_name}"
                    try:
                        data = storage.get_object_bytes(
                            bucket=settings.s3_bucket_raw,
                            key=df.file_s3_key,
                        )
                        zf.writestr(member, data)
                        file_entry["file_path"] = member
                    except Exception:
                        file_entry["file_error"] = "blob unavailable"
                    entry["files"].append(file_entry)

                manifest["documents"].append(entry)
                manifest["counts"]["documents"] += 1

        zf.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )

    return buf.getvalue(), manifest


# ---------------------------------------------------------------------------
# Streaming export (memory-bounded: ~part_size, regardless of archive size)
# ---------------------------------------------------------------------------


# Tuple shape stream-zip expects: (name, modified_at, mode, method, chunks_iter).
_ZipMember = tuple[str, datetime, int, Any, Iterable[bytes]]


# Callback signature: ``on_progress(done, total, stage)``. The worker
# uses it to drive ``jobs_service.update_progress`` so the UI bar
# moves while the ZIP is being assembled. Fully optional — pass
# ``None`` to disable.
ProgressCallback = Callable[[int, int, str], Awaitable[None]] | None


def _bytes_member(name: str, body: bytes, *, compress: bool = True) -> _ZipMember:
    """Wrap a fully-buffered byte payload as a stream-zip member.

    Compression mode matters a lot for export wall-clock:

    * Text-ish payloads (``manifest.json``, ``.md`` narratives) →
      ``ZIP_64`` (DEFLATE). They compress 5-10x and the CPU cost is
      negligible at their size.
    * Already-compressed binary blobs (DICOM with internal
      JPEG-LS / lossless / RLE, PDFs, JPEGs, ISO images) →
      ``NO_COMPRESSION_64``. zlib on these saves <1% size and burns
      ~150x more CPU per byte than the store path: a 50 MiB random
      blob takes 3000 ms with DEFLATE vs 20 ms stored. Pre-fix the
      worker steady-stated at ~1 CPU just running zlib on data that
      didn't compress — the bottleneck for the user-reported
      "il download è molto lento" on 13k-instance exports.

    All export paths funnel through here now that the prefetch pool
    materialises every blob to bytes before yielding. The upload
    side stays streaming via ``upload_iter`` so the archive never
    lands in RAM as a whole.
    """
    method = ZIP_64 if compress else NO_COMPRESSION_64
    return (name, datetime.now(UTC), 0o600, method, [body])


async def _build_export_plan(
    db: AsyncSession,
    user: User | None,
    patient: Patient,
    includes: set[str],
    *,
    scope_study_ids: set[uuid.UUID] | None = None,
    scope_document_ids: set[uuid.UUID] | None = None,
    deidentify_dicom: bool = False,
    layout: str = DEFAULT_LAYOUT,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Walk the DB and emit ``(manifest, work_items)``.

    The plan is the metadata-only pass: it pre-counts every blob the
    streaming step will fetch, so the worker can publish a meaningful
    ``progress_total`` BEFORE the heavy S3 traffic starts. Each
    ``work_item`` is a small dict tagged with ``kind`` (``"instance"``
    / ``"stream"`` / ``"bytes"``) plus the keys the streaming builder
    needs to materialise the member.

    Permission re-checks (DOWNLOAD_DICOM per study, READ_ANNOTATIONS
    per study for markers) happen here too — same gate as the
    in-memory path so the two builders never disagree.

    Scoping (used by folder + bulk download):

    * ``scope_study_ids``: when set, restricts the studies branch
      (and the markers/annotations attached to those studies) to the
      given UUIDs. Studies outside the set are silently skipped.
    * ``scope_document_ids``: same idea for documents.
    * ``reports`` are patient-level in v3, so when *either* scope is
      set the reports branch is reduced to ReportContents linked to
      the in-scope clinical events (studies' parents) or to the
      in-scope documents via ContentDocumentLink. With no scope the
      whole patient's reports are emitted, as before.

    A scope of ``set()`` (empty set) means "no items of this kind",
    distinct from ``None`` ("no filter"). The bulk-download path
    relies on this distinction to honour a request like "ZIP only
    these documents — no studies, no reports".
    """
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "patient": patient_to_dict(patient),
        "includes": sorted(includes),
        "layout": layout,
        "counts": {
            "studies": 0,
            "reports": 0,
            "documents": 0,
            "annotations": 0,
            "dicom_files": 0,
        },
        "studies": [],
        "reports": [],
        "documents": [],
        "annotations": [],
    }
    work: list[dict[str, Any]] = []

    # ``tree`` layout resolves every member path against the patient's
    # curated Folder tree; ``flat`` keeps the legacy UUID-keyed paths
    # (folder query skipped entirely). The namer is stateful (de-dup
    # bookkeeping) so a single instance services the whole plan.
    folder_paths = await _build_folder_layout(db, patient) if layout == "tree" else {}
    namer = _ExportNamer(layout, folder_paths)

    patient_perms = await effective_permissions_on_patient(db, user=user, patient=patient)
    include_dicom = "dicom" in includes and DOWNLOAD_DICOM in patient_perms
    if "dicom" in includes and not include_dicom:
        raise HTTPException(
            status_code=403,
            detail="download:dicom grant required to include DICOM files",
        )

    # Studies branch — filtered to ``scope_study_ids`` if set. An
    # empty set short-circuits the whole branch (no studies emitted).
    if scope_study_ids is not None and not scope_study_ids:
        studies: list[ImagingStudy] = []
    else:
        study_query = select(ImagingStudy).where(ImagingStudy.patient_id == patient.id)
        if scope_study_ids is not None:
            study_query = study_query.where(ImagingStudy.id.in_(scope_study_ids))
        study_query = study_query.order_by(ImagingStudy.study_date.desc().nullslast())
        studies = list((await db.execute(study_query)).scalars().all())
    readable_studies: list[ImagingStudy] = []
    for study in studies:
        if not await can(db, user=user, action=READ_METADATA, study=study):
            continue
        readable_studies.append(study)
    study_ids = [s.id for s in readable_studies]

    # Reserve one stable directory per study up front: the studies
    # branch and the markers branch both file under it, and
    # ``study_root`` must be called exactly once per study (it mutates
    # the namer's de-dup state). Only compute it when a branch will
    # actually use it, so a documents-only export does not let study
    # names shadow document names in the de-dup space.
    need_study_dirs = (
        "studies" in includes or include_dicom or "annotations" in includes or "markers" in includes
    )
    study_roots: dict[uuid.UUID, str] = (
        {s.id: namer.study_root(s) for s in readable_studies} if need_study_dirs else {}
    )

    if "studies" in includes or include_dicom:
        for study in readable_studies:
            study_dict = study_to_dict(study)
            study_root = study_roots[study.id]
            if namer.tree:
                study_dict["export_path"] = study_root
            study_series_list: list[dict[str, Any]] = []
            study_dicom_ok = include_dicom and await can(
                db, user=user, action=DOWNLOAD_DICOM, study=study
            )
            series_rows: list[Series] = list(
                (
                    await db.execute(
                        select(Series)
                        .where(Series.study_id == study.id)
                        .order_by(Series.series_number.asc().nullslast())
                    )
                )
                .scalars()
                .all()
            )
            for n, series in enumerate(series_rows, start=1):
                instances: list[Instance] = list(
                    (
                        await db.execute(
                            select(Instance)
                            .where(Instance.series_id == series.id)
                            .order_by(Instance.instance_number.asc().nullslast())
                        )
                    )
                    .scalars()
                    .all()
                )
                series_dict = series_to_dict(series)
                series_dict["instances"] = [instance_to_dict(i) for i in instances]
                series_root = namer.series_root(study_root, n, series)
                if namer.tree:
                    series_dict["export_path"] = series_root
                study_series_list.append(series_dict)
                # Per-series manifest as a small JSON member.
                work.append(
                    {
                        "kind": "bytes",
                        "name": namer.series_manifest(series_root),
                        "body": json.dumps(series_dict, indent=2, ensure_ascii=False).encode(
                            "utf-8"
                        ),
                    }
                )
                if study_dicom_ok:
                    for inst in instances:
                        work.append(
                            {
                                "kind": "blob",
                                "name": namer.instance(series_root, inst),
                                "bucket": inst.s3_bucket,
                                "key": inst.s3_key,
                                # PHI scrub at fetch time when the
                                # caller asked for a deidentified
                                # archive. Non-DICOM blobs (PDFs,
                                # images, ISOs) ignore this flag —
                                # they're not DICOM and the deidentify
                                # routine would crash on a non-DICOM
                                # header. Limited to instance blobs
                                # by where this branch lives.
                                "deidentify": deidentify_dicom,
                            }
                        )
                        manifest["counts"]["dicom_files"] += 1
            study_dict["series"] = study_series_list
            manifest["studies"].append(study_dict)
            manifest["counts"]["studies"] += 1

    if "reports" in includes:
        # Patient-wide reports unless a scope was passed in. With a
        # scope, narrow to ReportContent that touches the in-scope
        # studies (via clinical_event) or in-scope documents (via
        # ContentDocumentLink). Without scope_*, ``patient_id`` is
        # the only filter so the legacy "all reports" behaviour is
        # preserved.
        scoped_export = scope_study_ids is not None or scope_document_ids is not None
        if scoped_export:
            relevant_event_ids: set[uuid.UUID] = set()
            if scope_study_ids:
                rows = (
                    await db.execute(
                        select(ImagingStudy.clinical_event_id).where(
                            ImagingStudy.id.in_(scope_study_ids),
                            ImagingStudy.clinical_event_id.is_not(None),
                        )
                    )
                ).all()
                relevant_event_ids.update(eid for (eid,) in rows if eid is not None)
            relevant_rc_ids: set[uuid.UUID] = set()
            if relevant_event_ids:
                rows_rc = (
                    await db.execute(
                        select(ReportContent.id).where(
                            ReportContent.clinical_event_id.in_(relevant_event_ids)
                        )
                    )
                ).all()
                relevant_rc_ids.update(rid for (rid,) in rows_rc)
            if scope_document_ids:
                rows_link = (
                    await db.execute(
                        select(ContentDocumentLink.report_content_id).where(
                            ContentDocumentLink.document_id.in_(scope_document_ids)
                        )
                    )
                ).all()
                relevant_rc_ids.update(rid for (rid,) in rows_link)
            if relevant_rc_ids:
                report_rows: list[ReportContent] = list(
                    (
                        await db.execute(
                            select(ReportContent)
                            .where(ReportContent.id.in_(relevant_rc_ids))
                            .order_by(ReportContent.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                report_rows = []
        else:
            report_rows = list(
                (
                    await db.execute(
                        select(ReportContent)
                        .join(
                            ClinicalEvent,
                            ClinicalEvent.id == ReportContent.clinical_event_id,
                        )
                        .where(ClinicalEvent.patient_id == patient.id)
                        .order_by(ReportContent.created_at)
                    )
                )
                .scalars()
                .all()
            )
        for rep in report_rows:
            linked_doc_ids: list[str] = list(
                (
                    await db.execute(
                        select(ContentDocumentLink.document_id).where(
                            ContentDocumentLink.report_content_id == rep.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            entry: dict[str, Any] = {
                "id": str(rep.id),
                "clinical_event_id": str(rep.clinical_event_id),
                "authority": rep.authority_id,
                "status": rep.status,
                "language": rep.language,
                "title": rep.title,
                "author_kind": rep.author_kind,
                "created_by_subject_id": (
                    str(rep.created_by_subject_id) if rep.created_by_subject_id else None
                ),
                "model_id": rep.model_id,
                "provider": rep.provider,
                "linked_document_ids": [str(d) for d in linked_doc_ids],
                "created_at": rep.created_at.isoformat(),
                "updated_at": rep.updated_at.isoformat(),
            }
            report_base = namer.report_base(rep)
            narrative_path = f"{report_base}.md"
            entry["narrative_path"] = narrative_path
            work.append(
                {
                    "kind": "bytes",
                    "name": narrative_path,
                    "body": (rep.narrative_md or "").encode("utf-8"),
                }
            )
            if rep.findings_md:
                entry["findings_path"] = f"{report_base}.findings.md"
                work.append(
                    {
                        "kind": "bytes",
                        "name": f"{report_base}.findings.md",
                        "body": rep.findings_md.encode("utf-8"),
                    }
                )
            if rep.recommendations_md:
                entry["recommendations_path"] = f"{report_base}.recommendations.md"
                work.append(
                    {
                        "kind": "bytes",
                        "name": f"{report_base}.recommendations.md",
                        "body": rep.recommendations_md.encode("utf-8"),
                    }
                )
            manifest["reports"].append(entry)
            manifest["counts"]["reports"] += 1

    if ("annotations" in includes or "markers" in includes) and study_ids:
        for study in readable_studies:
            if not await can(db, user=user, action=READ_ANNOTATIONS, study=study):
                continue
            marker_rows: list[Marker] = list(
                (
                    await db.execute(
                        select(Marker)
                        .where(
                            Marker.target_kind == "study",
                            Marker.target_id == study.id,
                        )
                        .order_by(Marker.created_at)
                    )
                )
                .scalars()
                .all()
            )
            if not marker_rows:
                continue
            payload = [marker_to_dict(m) for m in marker_rows]
            markers_name = namer.markers(study, study_roots.get(study.id, f"studies/{study.id}"))
            work.append(
                {
                    "kind": "bytes",
                    "name": markers_name,
                    "body": json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
                }
            )
            manifest.setdefault("markers", []).extend(payload)
            manifest["counts"]["annotations"] += len(payload)

    if "documents" in includes:
        settings = get_settings()
        if scope_document_ids is not None and not scope_document_ids:
            docs: list[Document] = []
        else:
            doc_query = select(Document).where(
                Document.patient_id == patient.id,
                Document.deleted_at.is_(None),
            )
            if scope_document_ids is not None:
                doc_query = doc_query.where(Document.id.in_(scope_document_ids))
            doc_query = doc_query.order_by(Document.created_at)
            docs = list((await db.execute(doc_query)).scalars().all())
        for doc in docs:
            entry = {
                "id": str(doc.id),
                "kind": doc.kind_id,
                "provenance": doc.provenance_id,
                "authority": doc.authority_id,
                "title": doc.title,
                "text": doc.text,
                "document_date": (str(doc.document_date) if doc.document_date else None),
                "created_at": doc.created_at.isoformat(),
                "files": [],
            }
            doc_base = namer.document_base(doc)
            if namer.tree:
                entry["export_path"] = doc_base
            if doc.text:
                work.append(
                    {
                        "kind": "bytes",
                        "name": f"{doc_base}.txt",
                        "body": doc.text.encode("utf-8"),
                    }
                )
            if doc.file_s3_key:
                ext = ext_for(doc.file_content_type, doc.file_s3_key)
                # In tree layout the base is the document title, which
                # often already carries its extension ("referto.pdf");
                # don't double it. Flat bases (``documents/{id}``) never
                # end in the ext, so legacy names are untouched.
                doc_blob_name = (
                    doc_base
                    if doc_base.casefold().endswith(f".{ext}".casefold())
                    else f"{doc_base}.{ext}"
                )
                entry["file_path"] = doc_blob_name
                entry["file_content_type"] = doc.file_content_type
                work.append(
                    {
                        "kind": "blob",
                        "name": doc_blob_name,
                        "bucket": settings.s3_bucket_raw,
                        "key": doc.file_s3_key,
                    }
                )
            doc_files: list[DocumentFile] = list(
                (
                    await db.execute(
                        select(DocumentFile)
                        .where(DocumentFile.document_id == doc.id)
                        .order_by(DocumentFile.sequence, DocumentFile.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for df in doc_files:
                file_entry: dict[str, Any] = {
                    "id": str(df.id),
                    "sequence": df.sequence,
                    "original_filename": df.original_filename,
                    "content_type": df.file_content_type,
                    "size_bytes": df.size_bytes,
                }
                safe_name = (df.original_filename or "").replace("/", "_") or (
                    f"{df.sequence}.{ext_for(df.file_content_type, df.file_s3_key)}"
                )
                member = f"{doc_base}/{df.sequence:03d}-{safe_name}"
                file_entry["file_path"] = member
                entry["files"].append(file_entry)
                work.append(
                    {
                        "kind": "blob",
                        "name": member,
                        "bucket": settings.s3_bucket_raw,
                        "key": df.file_s3_key,
                    }
                )
            manifest["documents"].append(entry)
            manifest["counts"]["documents"] += 1

    return manifest, work


def _fetch_blob_bytes(storage: Any, bucket: str, key: str, *, deidentify: bool = False) -> bytes:
    """Read an entire S3 object into memory. Used by the prefetch
    pool; must be sync because boto3 is sync. Returns ``b""`` on
    failure so a single missing object doesn't abort the whole
    archive (matches the legacy graceful-degrade behaviour where
    ``file_error`` is recorded in the manifest).

    When ``deidentify`` is True the blob is assumed to be a DICOM
    instance and is run through ``deidentify_dicom_bytes`` before
    being returned to the ZIP writer. The cache key for the
    resulting artifact already includes the deidentify flag (see
    the share-link enqueue path), so an identifying ZIP and a
    pseudonymized ZIP are stored at distinct S3 keys and never
    confused.
    """
    try:
        body_iter, _, _ = storage.iter_object(bucket=bucket, key=key)
        raw = b"".join(body_iter)
    except Exception as exc:
        logger.warning("blob unavailable, skipping (%s/%s): %s", bucket, key, exc)
        return b""
    if not deidentify:
        return raw
    try:
        return deidentify_dicom_bytes(raw)
    except Exception as exc:
        # A scrub failure is not fatal for the archive — we keep the
        # original bytes out (they would leak PHI), so we drop the
        # instance entirely. The manifest still lists it so the
        # recipient knows their archive is missing one file rather
        # than silently containing un-scrubbed PHI.
        logger.warning("deidentify failed for %s/%s, dropping instance: %s", bucket, key, exc)
        return b""


class ExportCancelledError(RuntimeError):
    """Raised by the streaming pipeline when ``should_cancel`` returns
    True between members. The caller (worker task) maps it to the
    standard ``cancelled`` Job state instead of ``failed``.

    The S3 multipart upload is aborted by the surrounding
    ``upload_iter`` context manager when this propagates out of the
    member generator, so we don't leak orphan parts.
    """


def _stream_zip_to_s3_sync(
    work: list[dict[str, Any]],
    manifest_bytes: bytes,
    *,
    bucket: str,
    key: str,
    progress_q: list[int],
    parallelism: int,
    should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
) -> int:
    """Sync core: feed stream-zip with the planned members and pipe
    its output into a single S3 multipart upload, with a
    bounded-prefetch S3 reader pool so the pipeline doesn't stall on
    sequential GetObject round-trips.

    Order is preserved: each blob work item submits a Future to the
    pool, and ``_members`` pulls Futures in submission order — so the
    archive layout matches a sequential build, but blobs are fetched
    up to ``parallelism`` items ahead of where stream-zip is
    consuming. With 32 readers, ~13k DICOM round-trips that used to
    serialise into ~30-60 min wall-clock collapse to ~3-5 min,
    capped by S3 backend throughput rather than RTT × N.

    Runs inside ``asyncio.to_thread`` because both stream_zip and
    boto3 are sync. Updates ``progress_q[0]`` (a one-element list,
    used as a thread-safe counter) every time a member finishes so
    the async caller can publish progress.

    Memory: O(parallelism × max blob size). For ~1 MB DICOM and 32
    readers the steady-state buffer is ~32 MiB, well under the
    worker's 4 GiB limit. Pass ``parallelism=1`` to disable
    prefetching when blobs are individually huge (e.g. multi-GiB
    DocumentFile).
    """
    from concurrent.futures import Future, ThreadPoolExecutor

    storage = get_s3_storage()
    pool = ThreadPoolExecutor(
        max_workers=max(1, parallelism),
        thread_name_prefix="bvp-export-fetch",
    )
    pending: deque[tuple[dict[str, Any], Future[bytes] | None]] = deque()
    work_iter = iter(work)

    def _submit_next() -> bool:
        """Submit the next work item. ``bytes`` items carry a None
        future (already in memory); ``blob`` items submit a fetch
        task. Returns False when the source iterator is exhausted."""
        try:
            item = next(work_iter)
        except StopIteration:
            return False
        if item["kind"] == "bytes":
            pending.append((item, None))
        else:
            fut = pool.submit(
                _fetch_blob_bytes,
                storage,
                item["bucket"],
                item["key"],
                deidentify=bool(item.get("deidentify")),
            )
            pending.append((item, fut))
        return True

    # Prime the pipeline with up to ``parallelism`` items so the
    # first stream-zip pull doesn't see an empty pool.
    for _ in range(max(1, parallelism)):
        if not _submit_next():
            break

    def _members() -> Iterator[_ZipMember]:
        while pending:
            # Cooperative cancellation: check between members so a user
            # who clicked Cancel on a 70%-done multi-GB export actually
            # stops the worker. The check is cheap (Job.status read in a
            # tiny session) and runs before each S3 fetch wait, so the
            # worst case is one more blob downloaded after the cancel.
            # Raising here propagates out of stream_zip into upload_iter,
            # which aborts the multipart upload (no orphan parts).
            if should_cancel is not None and should_cancel():
                raise ExportCancelledError("export cancelled by caller")
            item, fut = pending.popleft()
            # Keep the pool primed: as soon as we consume one, queue
            # the next so prefetching stays ``parallelism`` deep.
            _submit_next()
            if fut is None:
                # ``bytes`` items are JSON manifests / .md narratives —
                # text that compresses 5-10x with DEFLATE. CPU cost is
                # noise at their size.
                body = item["body"]
                yield _bytes_member(item["name"], body, compress=True)
            else:
                # ``blob`` items are S3-fetched binaries — DICOM
                # (already JPEG-LS / lossless / RLE), PDFs, JPEGs,
                # ISO images. zlib on these saves <1% and burns ~150x
                # the CPU of the store path. STORE.
                body = fut.result()
                yield _bytes_member(item["name"], body, compress=False)
            progress_q[0] += 1
        # Final manifest, after every other member so its byte count
        # reflects the same plan we just streamed. Compressed: it's
        # JSON.
        yield _bytes_member("manifest.json", manifest_bytes, compress=True)
        progress_q[0] += 1

    try:
        result = storage.upload_iter(
            stream_zip(_members()),
            bucket=bucket,
            key=key,
            content_type="application/zip",
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return result.size_bytes


async def stream_export_to_s3(
    db: AsyncSession,
    user: User | None,
    patient: Patient,
    includes: set[str],
    *,
    job_id: uuid.UUID,
    on_progress: ProgressCallback = None,
    scope_study_ids: set[uuid.UUID] | None = None,
    scope_document_ids: set[uuid.UUID] | None = None,
    s3_key_override: str | None = None,
    should_cancel: Callable[[], bool | Awaitable[bool]] | None = None,
    deidentify_dicom: bool = False,
    layout: str = DEFAULT_LAYOUT,
) -> tuple[str, str, dict[str, Any], int]:
    """Streaming export builder.

    Walks the patient with the same permission gates and manifest
    layout as :func:`build_export_zip`, but instead of buffering the
    archive in RAM:

    1. Runs a metadata pass to compute the full plan (every blob the
       worker will fetch). This makes ``len(work)`` a meaningful
       ``progress_total``.
    2. Calls ``on_progress(0, total, "building_zip")`` so the UI shows
       a moving bar before any heavy S3 traffic.
    3. Spawns the sync stream-zip → S3 multipart pipeline in a worker
       thread; meanwhile ticks ``on_progress`` every 250 ms with the
       current member count.
    4. Returns ``(bucket, key, manifest, size_bytes)``.

    Memory profile: O(part_size) — default 8 MiB — independent of
    archive size.
    """
    settings = get_settings()
    manifest, work = await _build_export_plan(
        db,
        user,
        patient,
        includes,
        scope_study_ids=scope_study_ids,
        scope_document_ids=scope_document_ids,
        deidentify_dicom=deidentify_dicom,
        layout=layout,
    )
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    bucket = settings.s3_bucket_derivatives
    # ``s3_key_override`` lets callers (study export, future kind-
    # specific exports, ...) land the artifact under their own prefix
    # and filename so the suggested download name reflects what was
    # exported instead of always saying ``fascicolo-...``. Falls back
    # to the patient-wide layout used by the legacy fascicolo export.
    key = s3_key_override or export_s3_key(job_id=job_id, patient=patient)

    total = len(work) + 1  # +1 for the trailing manifest.json
    if on_progress is not None:
        await on_progress(0, total, "building_zip")

    progress_q: list[int] = [0]

    async def _ticker() -> None:
        last = -1
        while True:
            await asyncio.sleep(0.25)
            cur = progress_q[0]
            if cur != last and on_progress is not None:
                with _suppress_progress_errors():
                    await on_progress(cur, total, "building_zip")
                last = cur

    # Cancel poll: an async-side ticker updates a shared boolean; the
    # sync member generator reads it via the lambda below. This keeps
    # the cancel check on the main event loop (no nested asyncio.run
    # in the to_thread sync path) and bounds the latency between a
    # DELETE /api/jobs/{id} and the worker actually stopping at one
    # poll interval.
    cancel_ref: list[bool] = [False]
    cancel_ticker_task: asyncio.Task[None] | None = None
    sync_cancel: Callable[[], bool] | None = None
    if should_cancel is not None:
        # ``should_cancel`` from the worker is async-callable
        # (``Callable[[], Awaitable[bool] | bool]``) so we accept either
        # shape. The ticker keeps polling until cancelled.
        async def _cancel_ticker() -> None:
            while True:
                await asyncio.sleep(1.0)
                try:
                    res = should_cancel()
                    if asyncio.iscoroutine(res):
                        res = await res
                    cancel_ref[0] = bool(res)
                except Exception:
                    # Don't let a transient DB blip kill the build.
                    logger.debug("cancel poll failed, continuing", exc_info=True)

        cancel_ticker_task = asyncio.create_task(_cancel_ticker())
        sync_cancel = lambda: cancel_ref[0]  # noqa: E731

    ticker_task = asyncio.create_task(_ticker())
    try:
        size_bytes = await asyncio.to_thread(
            _stream_zip_to_s3_sync,
            work,
            manifest_bytes,
            bucket=bucket,
            key=key,
            progress_q=progress_q,
            parallelism=settings.export_prefetch_parallelism,
            should_cancel=sync_cancel,
        )
    finally:
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass
        if cancel_ticker_task is not None:
            cancel_ticker_task.cancel()
            try:
                await cancel_ticker_task
            except asyncio.CancelledError:
                pass

    if on_progress is not None:
        await on_progress(total, total, "uploading")

    return bucket, key, manifest, size_bytes


@contextlib.contextmanager
def _suppress_progress_errors() -> Iterator[None]:
    """Don't let a transient progress-update DB hiccup kill the
    streaming upload — the export is what matters, the progress bar
    is best-effort UI feedback."""
    try:
        yield
    except Exception:
        logger.debug("progress update failed, continuing", exc_info=True)


def export_filename(patient: Patient) -> str:
    """Stable filename for the downloadable ZIP. Used both as the S3
    object suffix and as the browser ``Content-Disposition`` filename
    so the user sees the same name regardless of route."""
    safe = patient.display_name.replace("/", "_").replace("\\", "_")
    safe = safe or str(patient.id)
    return f"fascicolo-{safe}-{patient.id}.zip"


def export_s3_key(*, job_id: uuid.UUID, patient: Patient) -> str:
    """Return the canonical S3 key for an export artifact.

    Keyed by job id so two concurrent exports of the same patient by
    different users don't collide. The patient slug suffix is purely
    cosmetic (helps when listing the bucket); the job id is the
    primary discriminator.
    """
    return f"exports/fascicolo/{job_id}/{export_filename(patient)}"


def upload_export_zip(zip_bytes: bytes, *, job_id: uuid.UUID, patient: Patient) -> tuple[str, str]:
    """Persist the ZIP on S3 and return ``(bucket, key)``. The caller
    decides whether to store ``s3://bucket/key`` in ``Job.result_uri``
    or to wrap it in a presigned URL."""
    storage = get_s3_storage()
    settings = get_settings()
    bucket = settings.s3_bucket_derivatives
    key = export_s3_key(job_id=job_id, patient=patient)
    storage.upload_bytes(zip_bytes, bucket=bucket, key=key)
    return bucket, key


def make_export_download_url(
    *,
    bucket: str,
    key: str,
    filename: str,
    ttl_seconds: int = 3600,
) -> str:
    """Sign a short-lived GET URL for the browser download. Re-signed
    on every read of the parent Job so the URL never outlives the
    Job's expires_at."""
    storage = get_s3_storage()
    return storage.presigned_get_url(
        bucket=bucket,
        key=key,
        expires_in=ttl_seconds,
        response_content_disposition=f'attachment; filename="{filename}"',
        response_content_type="application/zip",
    )


__all__ = [
    "ALLOWED_INCLUDES",
    "DEFAULT_INCLUDES",
    "DEFAULT_LAYOUT",
    "LAYOUTS",
    "ProgressCallback",
    "build_export_zip",
    "export_filename",
    "export_s3_key",
    "ext_for",
    "instance_to_dict",
    "make_export_download_url",
    "marker_to_dict",
    "patient_to_dict",
    "series_to_dict",
    "stream_export_to_s3",
    "study_to_dict",
    "upload_export_zip",
]
