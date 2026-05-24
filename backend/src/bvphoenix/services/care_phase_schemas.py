"""Pydantic schemas for the care-phase / care-timeline API surface.

Kept in a dedicated module to avoid bloating ``_schemas.py``; imported
by ``api/care_phases.py`` (REST), ``api/clinical_events.py`` (which
embeds ``EventTarget`` into ``ClinicalEventOut``), the SVG renderer
service, and the MCP tool layer.

The discriminated ``EventTarget`` union lets the frontend and MCP
clients know where a click on a timeline dot should navigate
without a second round trip: the backend resolves the target
(imaging study, document, report, consultation, fallback to the
generic clinical-event detail page) once at fetch time.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------
# EventTarget — discriminated union: where a click on a timeline dot
# should navigate. Resolved server-side so the frontend (and any MCP
# client) needs no extra round trip to know the link target.
# ----------------------------------------------------------------------


class _TargetBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    # Frontend navigation URL (relative path).
    url: str
    # MCP resource URI for clients that resolve mcp:// links.
    mcp_uri: str


class StudyTarget(_TargetBase):
    kind: Literal["imaging_study"] = "imaging_study"
    id: uuid.UUID


class ReportTarget(_TargetBase):
    kind: Literal["report"] = "report"
    id: uuid.UUID


class DocumentTarget(_TargetBase):
    kind: Literal["document"] = "document"
    id: uuid.UUID


class ConsultationTarget(_TargetBase):
    kind: Literal["consultation"] = "consultation"
    id: uuid.UUID


class GenericEventTarget(_TargetBase):
    kind: Literal["event"] = "event"
    id: uuid.UUID


EventTarget = Annotated[
    StudyTarget | ReportTarget | DocumentTarget | ConsultationTarget | GenericEventTarget,
    Field(discriminator="kind"),
]


# ----------------------------------------------------------------------
# CarePhase
# ----------------------------------------------------------------------


class CarePhaseCounts(BaseModel):
    n_events: int = 0
    n_studies: int = 0
    n_documents: int = 0
    n_reports: int = 0
    n_consultations: int = 0


class CarePhaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    slug: str
    name: str
    name_i18n: dict[str, str] = Field(default_factory=dict)
    kind: str
    color_hex: str
    start_date: date | None = None
    end_date: date | None = None
    ordinal: int
    narrative_md: str | None = None
    author_kind: str
    proposed_by_agent_id: uuid.UUID | None = None
    confirmed_by_user_id: uuid.UUID | None = None
    confirmed_at: datetime | None = None
    etag: uuid.UUID
    created_at: datetime
    updated_at: datetime
    counts: CarePhaseCounts = Field(default_factory=CarePhaseCounts)


# ----------------------------------------------------------------------
# ClinicalEvent (timeline-specific projection — the wire shape used
# by /care-timeline. Distinct from the standalone ClinicalEventOut in
# clinical_events.py; this one always carries a resolved target.)
# ----------------------------------------------------------------------


class TimelineEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    kind: str
    event_date: date | None
    title: str
    body_part: str | None = None
    code_loinc: str | None = None
    code_snomed: str | None = None
    narrative: str | None = None
    phase_id: uuid.UUID | None = None
    phase_assigned_by: str | None = None
    phase_assignment_confidence: float | None = None
    target: EventTarget
    etag: uuid.UUID
    # ---- planning & calendar (migration 0098) ----------------------
    # Surfaced on the timeline projection so the FE can render
    # planned/cancelled/missed events distinctively. Default
    # ``completed`` mirrors the DB server_default, keeping the wire
    # contract back-compatible: callers built against the pre-0098
    # API see every existing event as ``completed``.
    event_status: str = "completed"
    planned_start_at: datetime | None = None
    actual_start_at: datetime | None = None
    timezone: str | None = None


class CarePhaseDetailOut(CarePhaseOut):
    events: list[TimelineEventOut] = Field(default_factory=list)


class CareTimelineOut(BaseModel):
    patient_id: uuid.UUID
    phases: list[CarePhaseDetailOut]
    unassigned_events: list[TimelineEventOut] = Field(default_factory=list)
    generated_at: datetime
    lang: str = "it"


# ----------------------------------------------------------------------
# Material aggregate — what hangs off the events of a phase.
# ----------------------------------------------------------------------


class MaterialItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    kind: Literal["study", "document", "report", "consultation", "annotation"]
    id: uuid.UUID
    title: str
    secondary: str | None = None
    event_id: uuid.UUID | None = None
    event_date: date | None = None
    url: str
    mcp_uri: str


class CarePhaseMaterialOut(BaseModel):
    phase_id: uuid.UUID
    studies: list[MaterialItem] = Field(default_factory=list)
    documents: list[MaterialItem] = Field(default_factory=list)
    reports: list[MaterialItem] = Field(default_factory=list)
    consultations: list[MaterialItem] = Field(default_factory=list)
    annotations: list[MaterialItem] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------


class CarePhaseCreateIn(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    # ``name`` is optional on the wire: when omitted we derive it from
    # ``name_i18n`` (preferring 'it' then 'en' then any value), falling
    # back to the slug. Both the FE and the MCP tool send ``name_i18n``
    # as the canonical bilingual map, so making them also send the
    # collapsed ``name`` was a foot-gun (the agent's session report
    # round 5 surfaced it as systematic 422 on every create call).
    name: str | None = Field(default=None, min_length=1, max_length=255)
    name_i18n: dict[str, str] = Field(default_factory=dict)
    kind: str
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    start_date: date | None = None
    end_date: date | None = None
    ordinal: int | None = Field(default=None, ge=0)
    narrative_md: str | None = None


class CarePhaseUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    name_i18n: dict[str, str] | None = None
    kind: str | None = None
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    start_date: date | None = None
    end_date: date | None = None
    ordinal: int | None = Field(default=None, ge=0)
    narrative_md: str | None = None


class AssignPhaseIn(BaseModel):
    confidence: float | None = Field(default=None, ge=0, le=1)


class ReorderItem(BaseModel):
    phase_id: uuid.UUID
    ordinal: int = Field(ge=0)


class ReorderIn(BaseModel):
    ordinals: list[ReorderItem]


# ----------------------------------------------------------------------
# Proposal flow (LLM classifier)
# ----------------------------------------------------------------------


class ProposedPhase(BaseModel):
    slug: str
    name: str
    name_i18n: dict[str, str] = Field(default_factory=dict)
    kind: str
    color_hex: str | None = None
    ordinal: int
    narrative_md: str | None = None


class ProposedAssignment(BaseModel):
    event_id: uuid.UUID
    phase_slug: str
    confidence: float = Field(ge=0, le=1)


class ProposalPayload(BaseModel):
    phases: list[ProposedPhase]
    assignments: list[ProposedAssignment]


class ProposePhasesOut(BaseModel):
    proposal_id: uuid.UUID
    job_id: uuid.UUID | None
    status: str
    payload: ProposalPayload
    model_id: str
    cached: bool
    created_at: datetime


class ApplyProposalIn(BaseModel):
    proposal_id: uuid.UUID
    accept_phases: list[str]  # slugs to accept
    accept_assignments: list[uuid.UUID]  # event ids to apply


class ApplyProposalOut(BaseModel):
    applied_phases: list[uuid.UUID]
    applied_assignments: int
    skipped_assignments: int


# ----------------------------------------------------------------------
# Revisions
# ----------------------------------------------------------------------


class CarePhaseRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    phase_id: uuid.UUID
    revision_no: int
    snapshot: dict[str, Any]
    change_kind: str
    author_kind: str
    actor_id: uuid.UUID | None
    diff_summary: str | None
    created_at: datetime


class RestoreRevisionIn(BaseModel):
    revision_no: int = Field(ge=1)


# ----------------------------------------------------------------------
# Salute panel (timeline health snapshot)
# ----------------------------------------------------------------------


class TimelineHealthOut(BaseModel):
    patient_id: uuid.UUID
    n_phases: int
    n_events: int
    n_events_assigned: int
    pct_assigned: float
    pending_proposals: int
    last_classifier_run: datetime | None


__all__ = [
    "ApplyProposalIn",
    "ApplyProposalOut",
    "AssignPhaseIn",
    "CarePhaseCounts",
    "CarePhaseCreateIn",
    "CarePhaseDetailOut",
    "CarePhaseMaterialOut",
    "CarePhaseOut",
    "CarePhaseRevisionOut",
    "CarePhaseUpdateIn",
    "CareTimelineOut",
    "ConsultationTarget",
    "DocumentTarget",
    "EventTarget",
    "GenericEventTarget",
    "MaterialItem",
    "ProposalPayload",
    "ProposePhasesOut",
    "ProposedAssignment",
    "ProposedPhase",
    "ReorderIn",
    "ReorderItem",
    "ReportTarget",
    "RestoreRevisionIn",
    "StudyTarget",
    "TimelineEventOut",
    "TimelineHealthOut",
]
