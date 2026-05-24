"""Shared DICOM ingestion pipeline.

Bytes-in, rows-out. Both the CLI (`cli/import_dicom.py`) and the web
upload endpoints (`api/dicom_upload.py`) feed one instance at a time
into :class:`DicomIngestor`, which:

1. Validates the payload is a real DICOM file (preamble magic + header
   parse).
2. Uploads it to the raw S3 bucket under the canonical
   ``studies/<study>/series/<series>/<sop>.dcm`` key.
3. Upserts ``ImagingStudy`` → ``Series`` → ``Instance`` rows, reusing existing
   rows when UIDs already exist.

Keeping the logic here (rather than in the CLI) lets the web route stay
an HTTP adapter and keeps both code paths honest about the same
validation / dedup rules.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import pydicom
from pydicom.errors import InvalidDicomError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, ImagingStudy, Instance, Series, Subject
from bvphoenix.storage import S3Storage

# DICOM Part 10: 128-byte preamble (any bytes, typically zeros) + "DICM" magic.
DICM_MAGIC = b"DICM"
DICM_MAGIC_OFFSET = 128
DICM_MIN_BYTES = DICM_MAGIC_OFFSET + len(DICM_MAGIC)


@dataclass
class InstanceResult:
    sop_instance_uid: str
    series_instance_uid: str
    study_instance_uid: str
    created: bool  # False means the SOPInstanceUID already existed
    size_bytes: int


@dataclass
class UploadError:
    filename: str
    message: str


@dataclass
class IngestSummary:
    """Running counters for a batch ingest — tally of instances created,
    skipped, and per-file errors. Touched studies/series are discoverable
    via :attr:`DicomIngestor.touched_studies` / ``touched_series``.
    """

    instances_created: int = 0
    instances_existing: int = 0
    errors: list[UploadError] = field(default_factory=list)


@dataclass
class BulkIngestStats:
    """Outcome counters for :meth:`DicomIngestor.bulk_ingest_blobs`.

    Distinct from :class:`IngestSummary` because the bulk path uses
    ``INSERT … ON CONFLICT DO NOTHING`` and can't cheaply distinguish
    "newly inserted" from "row already existed" — both fall under
    ``created`` for the purposes of progress reporting.
    """

    total: int = 0
    created: int = 0
    errors: list[UploadError] = field(default_factory=list)


def has_dicm_preamble(blob: bytes) -> bool:
    """Cheap magic-byte check. Not all valid DICOM objects have a preamble
    (e.g. some raw DIMSE exports), so callers should still try the header
    parse as a fallback before rejecting.
    """
    return len(blob) >= DICM_MIN_BYTES and blob[DICM_MAGIC_OFFSET:DICM_MIN_BYTES] == DICM_MAGIC


def parse_dicom_header(blob: bytes) -> pydicom.Dataset:
    """Raise :class:`InvalidDicomError` if the bytes don't parse."""
    ds = pydicom.dcmread(
        io.BytesIO(blob),
        stop_before_pixels=True,
        force=not has_dicm_preamble(blob),  # permissive for preamble-less files
    )
    return ds


def validate_dicom(blob: bytes) -> pydicom.Dataset:
    """Return the parsed header or raise :class:`InvalidDicomError`.

    Accepts either a proper Part-10 file (preamble + DICM) or any bytes
    that pydicom can make sense of with ``force=True``. We still require
    the core UIDs to be present — without them the instance is not
    addressable.
    """
    try:
        ds = parse_dicom_header(blob)
    except (InvalidDicomError, OSError, EOFError) as exc:
        raise InvalidDicomError(f"not a DICOM file: {exc}") from exc

    for tag in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        if not getattr(ds, tag, None):
            raise InvalidDicomError(f"missing required tag: {tag}")
    return ds


def _as_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_dicom_date(value: str | None) -> date | None:
    if not value or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def s3_key_for(
    *,
    patient_id: uuid.UUID | None,
    study_id: uuid.UUID | str,
    series_id: uuid.UUID | str,
    instance_id: uuid.UUID | str,
) -> str:
    """Build the S3 key for a DICOM instance, using internal UUIDs only.

    Earlier versions keyed on DICOM ``StudyInstanceUID`` /
    ``SeriesInstanceUID`` / ``SOPInstanceUID`` with an owner prefix.
    Those UIDs come from the source scanner / RIS and are not
    authoritative across BitVision tenants — two patients on the
    same deployment can carry identical UIDs (legitimate scanner
    misconfiguration, or a malicious upload). Keying on them risks
    cross-tenant overwrites that the read-side authorization layer
    cannot catch.

    The new scheme uses values BitVision controls end-to-end:

    * ``patient_id``  — uuid_pk on Patient; orphan studies fall
      back to the ``unassigned/`` prefix until linked to a patient.
    * ``study_id``    — uuid_pk on ImagingStudy.
    * ``series_id``   — uuid_pk on Series.
    * ``instance_id`` — uuid_pk on Instance (caller pre-allocates
      with ``uuid.uuid4()`` so the key can be built before the
      ``INSERT``; the same UUID becomes ``Instance.id``).

    DICOM UIDs are kept as descriptive metadata on the row; they
    never appear in storage paths.
    """
    prefix = f"patients/{patient_id}" if patient_id is not None else "unassigned"
    return f"{prefix}/studies/{study_id}/series/{series_id}/instances/{instance_id}.dcm"


class DicomIngestor:
    """Ingest DICOM blobs one-by-one into S3 + Postgres.

    Not async-safe to share across tasks: holds per-request caches for
    ImagingStudy/Series rows so a 400-slice upload hits the DB twice per series
    rather than 400 times.
    """

    def __init__(
        self,
        *,
        db: AsyncSession,
        storage: S3Storage,
        bucket: str,
        owner: Subject,
        tier: str = "t1",
        is_public: bool = False,
    ) -> None:
        self._db = db
        self._storage = storage
        self._bucket = bucket
        self._owner = owner
        self._tier = tier
        self._is_public = is_public
        self._study_cache: dict[str, ImagingStudy] = {}
        self._series_cache: dict[str, Series] = {}

    @property
    def touched_studies(self) -> dict[str, ImagingStudy]:
        """Studies handled so far, keyed by StudyInstanceUID. Ids are
        populated after the first flush for each row."""
        return self._study_cache

    @property
    def touched_series(self) -> dict[str, Series]:
        """Series handled so far, keyed by SeriesInstanceUID."""
        return self._series_cache

    async def ingest_blob(self, blob: bytes) -> InstanceResult:
        """Validate + persist a single ``.dcm`` blob. Raises on bad data
        so the caller can surface a per-file error to the user.
        """
        ds = validate_dicom(blob)

        study_uid = str(ds.StudyInstanceUID)
        series_uid = str(ds.SeriesInstanceUID)
        sop_uid = str(ds.SOPInstanceUID)

        study = await self._get_or_create_study(ds, study_uid)
        series = await self._get_or_create_series(ds, study, series_uid)

        # Scope the dedup lookup to *this* series. The same SOP UID can
        # legitimately appear in another user's series (and used to
        # collide on a global UNIQUE) — only worry about the parent we
        # just resolved.
        existing = (
            await self._db.execute(
                select(Instance).where(
                    Instance.series_id == series.id,
                    Instance.sop_instance_uid == sop_uid,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return InstanceResult(
                sop_instance_uid=sop_uid,
                series_instance_uid=series_uid,
                study_instance_uid=study_uid,
                created=False,
                size_bytes=existing.size_bytes or len(blob),
            )

        # Pre-allocate the Instance.id UUID so the S3 key can be
        # built from internal IDs only (no DICOM UIDs in the path).
        instance_id = uuid.uuid4()
        key = s3_key_for(
            patient_id=study.patient_id,
            study_id=study.id,
            series_id=series.id,
            instance_id=instance_id,
        )
        # Offload boto3 (sync) to a thread so we don't block the loop on
        # long uploads; callers (FastAPI handlers) await this coroutine.
        await asyncio.to_thread(self._storage.upload_bytes, blob, bucket=self._bucket, key=key)

        sha = hashlib.sha256(blob).hexdigest()
        instance = Instance(
            id=instance_id,
            series_id=series.id,
            sop_instance_uid=sop_uid,
            sop_class_uid=str(getattr(ds, "SOPClassUID", None) or "") or None,
            instance_number=_as_int(getattr(ds, "InstanceNumber", None)),
            s3_bucket=self._bucket,
            s3_key=key,
            size_bytes=len(blob),
            content_sha256=sha,
        )
        self._db.add(instance)
        series.received_instance_count = (series.received_instance_count or 0) + 1
        await self._db.flush()

        return InstanceResult(
            sop_instance_uid=sop_uid,
            series_instance_uid=series_uid,
            study_instance_uid=study_uid,
            created=True,
            size_bytes=len(blob),
        )

    async def finalize(self) -> None:
        """Flip ``ingestion_complete`` on every touched study/series and
        materialise the parent ``ClinicalEvent`` for studies that have a
        patient assigned.

        v3 invariant: every imaging study has a parent ClinicalEvent by
        the time ``ingestion_complete=true``. The DICOM ingestor itself
        does not know the patient (DICOM payload != app patient), so the
        caller assigns ``study.patient_id`` between ``ingest_blob`` and
        ``finalize``; here we read it back and create the umbrella event.

        Studies that remain orphan (no patient) skip event creation;
        the assignment path that links an orphan study to a patient
        must call :func:`ensure_imaging_event` to materialise the event
        at that point.
        """
        for series in self._series_cache.values():
            series.ingestion_complete = True
        for study in self._study_cache.values():
            study.ingestion_complete = True
            if study.patient_id is None or study.clinical_event_id is not None:
                continue
            event = ClinicalEvent(
                patient_id=study.patient_id,
                kind="imaging_study",
                event_date=study.study_date,
                title=study.study_description or "Imaging study",
                source="imaging_ingest",
            )
            self._db.add(event)
            await self._db.flush()
            study.clinical_event_id = event.id

    async def bulk_ingest_blobs(
        self,
        blobs: list[bytes],
        *,
        max_concurrency: int = 8,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
    ) -> BulkIngestStats:
        """Optimised batch ingest of many DICOM blobs.

        Pipeline (designed for the bulk_upload worker, where a single
        client posts hundreds-to-thousands of files at once):

        1. Parse all DICOM headers in a thread pool (``validate_dicom``
           is synchronous + CPU-bound).
        2. Pre-warm ImagingStudy/Series caches sequentially. Doing this up
           front avoids a race between concurrent ingest paths trying
           to ``INSERT`` the same parent rows.
        3. Upload every payload to S3 concurrently, bounded by
           ``max_concurrency``. S3 PUT latency dominates per-file cost
           (50-100ms each), so ``asyncio.gather`` over a thread pool
           is the biggest win here.
        4. ``INSERT … ON CONFLICT DO NOTHING`` the Instance rows in
           per-series batches. Replaces the per-file ``flush()`` from
           ``ingest_blob`` with one round-trip per series.

        DB writes (steps 2 + 4) stay sequential because the
        ``AsyncSession`` is not concurrency-safe.

        Returns a :class:`BulkIngestStats` with per-blob outcomes
        reported via ``on_progress`` callbacks (called every batch of
        ``max_concurrency`` files).
        """
        stats = BulkIngestStats(total=len(blobs))
        if not blobs:
            return stats

        # Phase 1: parse headers in parallel via thread pool. Skipped
        # blobs (bad DICOM) are tracked here and never reach the
        # later phases.
        async def _parse(
            idx: int, blob: bytes
        ) -> tuple[int, pydicom.Dataset | None, BaseException | None]:
            try:
                ds = await asyncio.to_thread(validate_dicom, blob)
            except (InvalidDicomError, ValueError) as exc:
                return idx, None, exc
            return idx, ds, None

        parsed = await asyncio.gather(*[_parse(i, b) for i, b in enumerate(blobs)])

        # Phase 2: group by (study_uid, series_uid). The first dataset
        # in each group seeds get/create.
        groups: dict[tuple[str, str], list[tuple[int, pydicom.Dataset, bytes]]] = {}
        for idx, ds, err in parsed:
            if err is not None or ds is None:
                stats.errors.append(UploadError(filename=f"blob[{idx}]", message=str(err)))
                continue
            key = (str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID))
            groups.setdefault(key, []).append((idx, ds, blobs[idx]))

        # Phase 3-5: per series, get/create + parallel S3 + batch insert.
        sem = asyncio.Semaphore(max_concurrency)
        for (study_uid, series_uid), members in groups.items():
            first_ds = members[0][1]
            study = await self._get_or_create_study(first_ds, study_uid)
            series = await self._get_or_create_series(first_ds, study, series_uid)

            # Bind the loop variables so the coroutine doesn't close
            # over the iterator's last value (B023). The semaphore is
            # the same across iterations.
            async def _upload(
                idx: int,
                ds: pydicom.Dataset,
                blob: bytes,
                *,
                _patient_id: uuid.UUID | None = study.patient_id,
                _study_id: uuid.UUID = study.id,
                _series_id: uuid.UUID = series.id,
            ) -> dict | None:
                sop_uid = str(ds.SOPInstanceUID)
                # Pre-allocate the Instance.id so the S3 key can be
                # built from internal UUIDs only — no DICOM UIDs in
                # the path. Same uuid lands in the row on insert.
                instance_id = uuid.uuid4()
                key = s3_key_for(
                    patient_id=_patient_id,
                    study_id=_study_id,
                    series_id=_series_id,
                    instance_id=instance_id,
                )
                try:
                    async with sem:
                        await asyncio.to_thread(
                            self._storage.upload_bytes,
                            blob,
                            bucket=self._bucket,
                            key=key,
                        )
                except Exception as exc:
                    stats.errors.append(
                        UploadError(filename=f"blob[{idx}]", message=f"s3 upload: {exc}")
                    )
                    return None
                return {
                    "id": instance_id,
                    "series_id": _series_id,
                    "sop_instance_uid": sop_uid,
                    "sop_class_uid": str(getattr(ds, "SOPClassUID", None) or "") or None,
                    "instance_number": _as_int(getattr(ds, "InstanceNumber", None)),
                    "s3_bucket": self._bucket,
                    "s3_key": key,
                    "size_bytes": len(blob),
                    "content_sha256": hashlib.sha256(blob).hexdigest(),
                }

            uploads = await asyncio.gather(*[_upload(i, ds, b) for i, ds, b in members])
            instance_values = [v for v in uploads if v is not None]

            if instance_values:
                # ON CONFLICT DO NOTHING on the (series_id, sop_instance_uid)
                # unique constraint absorbs re-uploads as no-ops without
                # the per-file SELECT the legacy ingest_blob path runs.
                stmt = pg_insert(Instance).values(instance_values)
                stmt = stmt.on_conflict_do_nothing(index_elements=["series_id", "sop_instance_uid"])
                await self._db.execute(stmt)

            # Series counter: best-effort. The unique constraint keeps
            # us idempotent against retries; the count may overshoot if
            # the same blob is sent twice within a batch but that's not
            # worse than the legacy code's behaviour.
            series.received_instance_count = (series.received_instance_count or 0) + len(
                instance_values
            )
            stats.created += len(instance_values)

            if on_progress is not None:
                await on_progress(stats.created + len(stats.errors))

        return stats

    async def _get_or_create_study(self, ds: pydicom.Dataset, study_uid: str) -> ImagingStudy:
        cached = self._study_cache.get(study_uid)
        if cached is not None:
            return cached
        # Lookup is scoped to the ingestor's owner — UIDs are not
        # globally unique, only unique within an owner namespace.
        row = (
            await self._db.execute(
                select(ImagingStudy).where(
                    ImagingStudy.study_instance_uid == study_uid,
                    ImagingStudy.owner_subject_id == self._owner.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = ImagingStudy(
                study_instance_uid=study_uid,
                owner_subject_id=self._owner.id,
                contribution_tier=self._tier,
                is_public=self._is_public,
                study_description=getattr(ds, "StudyDescription", None),
                study_date=_parse_dicom_date(getattr(ds, "StudyDate", None)),
                modalities=[],
            )
            self._db.add(row)
            await self._db.flush()
        self._study_cache[study_uid] = row
        return row

    async def _get_or_create_series(
        self, ds: pydicom.Dataset, study: ImagingStudy, series_uid: str
    ) -> Series:
        cached = self._series_cache.get(series_uid)
        if cached is not None:
            return cached
        # Same reasoning as the study lookup — scope by parent so a
        # SeriesUID collision across two studies (or two owners) does
        # not get folded into the wrong row.
        row = (
            await self._db.execute(
                select(Series).where(
                    Series.series_instance_uid == series_uid,
                    Series.study_id == study.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = Series(
                study_id=study.id,
                series_instance_uid=series_uid,
                series_number=_as_int(getattr(ds, "SeriesNumber", None)),
                modality=getattr(ds, "Modality", None),
                body_part_examined=getattr(ds, "BodyPartExamined", None),
                series_description=getattr(ds, "SeriesDescription", None),
                expected_instance_count=None,
            )
            self._db.add(row)
            await self._db.flush()

            modality = getattr(ds, "Modality", None)
            if modality and modality.upper() not in (study.modalities or []):
                study.modalities = [*list(study.modalities or []), modality.upper()]
        self._series_cache[series_uid] = row
        return row


async def ensure_imaging_event(db: AsyncSession, study: ImagingStudy) -> ClinicalEvent | None:
    """Idempotently materialise the parent ClinicalEvent for an
    ImagingStudy. Returns the event (existing or newly created) when
    the study has a patient assigned; ``None`` for orphan studies.

    Used by:
    - the patient-assignment path (orphan study → linked study), so
      the event materialises at the moment a patient is known;
    - the Alembic 0084 backfill, applied to every pre-fix orphan row
      via ``op.get_bind().run_sync(...)`` would be overkill, so the
      migration uses raw SQL with the same semantics.
    """
    if study.patient_id is None:
        return None
    if study.clinical_event_id is not None:
        existing = (
            await db.execute(
                select(ClinicalEvent).where(ClinicalEvent.id == study.clinical_event_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    event = ClinicalEvent(
        patient_id=study.patient_id,
        kind="imaging_study",
        event_date=study.study_date,
        title=study.study_description or "Imaging study",
        source="imaging_ingest",
    )
    db.add(event)
    await db.flush()
    study.clinical_event_id = event.id
    return event


def iter_stow_rs_parts(body: bytes, boundary: bytes) -> list[bytes]:
    """Very small multipart/related parser for STOW-RS requests.

    RFC 7230-style multipart: parts are separated by ``--<boundary>``
    lines and the body ends with ``--<boundary>--``. Each part has
    headers (ignored — we only need the payload) separated from the body
    by ``\\r\\n\\r\\n``. We return the raw DICOM bytes of each part.

    Starlette's ``UploadFile`` / multipart-form parser doesn't handle
    ``multipart/related`` (it's form-data only), so we parse it here.
    """
    delimiter = b"--" + boundary
    parts: list[bytes] = []
    # Skip preamble before the first boundary.
    chunks = body.split(delimiter)
    for chunk in chunks[1:]:
        # Strip the trailing CRLF that separates parts, and the closing
        # ``--`` marker on the final chunk.
        if chunk.startswith(b"--"):
            break
        # Leading CRLF after the boundary.
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        # Trailing CRLF before the next boundary.
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        header_end = chunk.find(b"\r\n\r\n")
        if header_end == -1:
            # No header block — whole chunk is the body (tolerate).
            payload = chunk
        else:
            payload = chunk[header_end + 4 :]
        if payload:
            parts.append(payload)
    return parts


def parse_related_boundary(content_type: str) -> bytes | None:
    """Pull the ``boundary=`` parameter out of a ``Content-Type`` header.

    Accepts both quoted and unquoted values; returns ``None`` if the
    header is not multipart/related or lacks a boundary.
    """
    if not content_type or "multipart/related" not in content_type.lower():
        return None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            value = part.split("=", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value.encode("ascii", errors="replace")
    return None
