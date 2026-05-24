"""Shared Pydantic response schemas. Kept in one module so the API
surface stays consistent across study / series / annotation endpoints.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class StudyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    study_instance_uid: str
    owner_subject_id: uuid.UUID
    patient_id: uuid.UUID | None = None
    contribution_tier: str
    is_public: bool
    is_listed_for_sale: bool
    ingestion_complete: bool
    study_description: str | None
    study_date: date | None
    modalities: list[str]
    created_at: datetime
    # Provenance / license — populated for OpenData public-dataset
    # imports (contribution_tier='t4'); NULL on every user-uploaded
    # private study. Used by the frontend to render the citation
    # badge + license dialog on the study viewer header.
    source_collection: str | None = None
    license_spdx: str | None = None
    license_url: str | None = None
    citation_required: bool = False
    citation_text: str | None = None
    # True when the study is owned by the platform-owner subject (the
    # OpenData public dataset). Computed from owner_subject_id at
    # serialisation; lets the frontend distinguish a curated OpenData
    # entry from a user study marked is_public, without recomputing
    # the platform-owner UUID client-side.
    is_opendata: bool = False

    @classmethod
    def model_validate(cls, obj, *args, **kwargs):  # type: ignore[override]
        # Compute is_opendata at validation time so the auto-mapped
        # ImagingStudy row picks up the derived field without needing
        # a custom select. Cheap (1 UUID compare) and keeps callers
        # free of platform-owner plumbing.
        from bvphoenix.services.permissions import platform_owner_subject_id

        out = super().model_validate(obj, *args, **kwargs)
        owner = getattr(obj, "owner_subject_id", None)
        if owner is not None:
            try:
                out.is_opendata = owner == platform_owner_subject_id()
            except Exception:
                out.is_opendata = False
        return out


class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    study_id: uuid.UUID
    series_instance_uid: str
    series_number: int | None
    modality: str | None
    body_part_examined: str | None
    series_description: str | None
    expected_instance_count: int | None
    received_instance_count: int
    ingestion_complete: bool
    # Populated by ``/api/series/{id}`` from the middle DICOM instance's
    # WindowCenter (0028,1050) / WindowWidth (0028,1051) tags when present.
    # Left as ``None`` on list endpoints to keep them cheap.
    suggested_wc: float | None = None
    suggested_ww: float | None = None


class InstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    series_id: uuid.UUID
    sop_instance_uid: str
    sop_class_uid: str | None
    instance_number: int | None
    size_bytes: int | None


class StudyDetailOut(StudyOut):
    series: list[SeriesOut]


class PaginatedStudies(BaseModel):
    items: list[StudyOut]
    total: int
    limit: int
    offset: int
    # Populated when ``/api/search?facets=true``. Shape: ``modality``,
    # ``body_part``, ``year`` are ``{bucket: count}`` dicts; ``top_tags``
    # is a list of ``{namespace, value, count}``.
    facets: dict | None = None
