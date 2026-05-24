"""JSON schema for the care-phase classifier output.

Used both for prompt-side documentation (sent to the LLM verbatim so
the model knows the exact shape we expect) and for runtime validation
(via Pydantic, see :class:`ClassifierOutput`).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator

from bvphoenix.db.models.care_phases import (
    CARE_PHASE_DEFAULT_COLORS,
    CARE_PHASE_KINDS,
)


class ClassifierProposedPhase(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=255)
    name_i18n: dict[str, str] = Field(default_factory=dict)
    kind: str
    color_hex: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    ordinal: int = Field(ge=0)
    narrative_md: str | None = None

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in CARE_PHASE_KINDS:
            raise ValueError(f"unknown phase kind: {v}")
        return v


class ClassifierAssignment(BaseModel):
    event_id: uuid.UUID
    phase_slug: str
    confidence: float = Field(ge=0, le=1)


class ClassifierOutput(BaseModel):
    """Top-level shape returned by the classifier (either single-stage
    or after the verifier pass)."""

    phases: list[ClassifierProposedPhase]
    assignments: list[ClassifierAssignment]

    @field_validator("phases")
    @classmethod
    def _unique_slugs(cls, v: list[ClassifierProposedPhase]) -> list[ClassifierProposedPhase]:
        slugs = [p.slug for p in v]
        if len(set(slugs)) != len(slugs):
            raise ValueError("phase slugs must be unique within a patient")
        return v


JSON_SCHEMA_FOR_PROMPT: dict[str, Any] = {
    "type": "object",
    "required": ["phases", "assignments"],
    "additionalProperties": False,
    "properties": {
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["slug", "name", "name_i18n", "kind", "ordinal"],
                "additionalProperties": False,
                "properties": {
                    "slug": {
                        "type": "string",
                        "pattern": "^[a-z0-9-]+$",
                        "description": (
                            "Stable URL-safe identifier of the phase, e.g. "
                            "'imaging-pre-op'. Unique within the patient."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Default human-readable name (Italian).",
                    },
                    "name_i18n": {
                        "type": "object",
                        "required": ["it", "en"],
                        "additionalProperties": False,
                        "properties": {
                            "it": {"type": "string"},
                            "en": {"type": "string"},
                        },
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(CARE_PHASE_KINDS),
                        "description": (
                            "Semantic class. Must be one of: " + ", ".join(CARE_PHASE_KINDS)
                        ),
                    },
                    "color_hex": {
                        "type": ["string", "null"],
                        "pattern": "^#[0-9A-Fa-f]{6}$",
                        "description": (
                            "Optional override. If null, the server applies "
                            "the default palette per kind: "
                            + ", ".join(f"{k}={v}" for k, v in CARE_PHASE_DEFAULT_COLORS.items())
                        ),
                    },
                    "ordinal": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Chronological order index, starting from 0 for the earliest phase."
                        ),
                    },
                    "narrative_md": {
                        "type": ["string", "null"],
                        "description": (
                            "Brief Italian summary of what happens in this phase. Markdown allowed."
                        ),
                    },
                },
            },
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["event_id", "phase_slug", "confidence"],
                "additionalProperties": False,
                "properties": {
                    "event_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "An event id from the input list.",
                    },
                    "phase_slug": {
                        "type": "string",
                        "description": "Slug of one of the phases above.",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
            },
        },
    },
}


__all__ = [
    "JSON_SCHEMA_FOR_PROMPT",
    "ClassifierAssignment",
    "ClassifierOutput",
    "ClassifierProposedPhase",
]
