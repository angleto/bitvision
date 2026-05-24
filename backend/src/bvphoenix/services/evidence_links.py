"""Cross-patient guard for the Evidenze e sintesi link DSL.

The user-facing editor lets the clinician embed inline references
inside markdown via a single ``@kind:value`` syntax:

* ``@study:UID``         pointer to a DICOM study
* ``@series:UID``        pointer to a series
* ``@folder:UID``        pointer to a folder
* ``@document:UID``      pointer to a patient document
* ``@consultation:UID``  pointer to a consultation
* ``@report:UID``        pointer to a report
* ``@tag:value``         clickable tag chip (``#`` would collide with
                         markdown headings; ``@tag:`` unifies the trigger)

This module is the **persist-time enforcement** for those mentions.
The hard rule: every referenced UUID must resolve to a resource that
belongs to the **same** patient as the note being saved. A reference
to a resource of a different patient is rejected with HTTP 422 so
patient data isolation is preserved at the storage boundary, even
when the note's body is later rendered against a permission-laxer
viewer.

The frontend additionally renders failing mentions inline as broken
(red strikethrough) so the user catches the problem before save, but
the server is the source of truth.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    Document,
    Folder,
    ImagingStudy,
    Series,
)

# Recognised mention kinds and the lookup target for each. ``tag`` is
# handled separately because it's a value, not a UUID, and tags are
# patient-scoped via their target's patient.
MENTION_KINDS: tuple[str, ...] = (
    "study",
    "series",
    "folder",
    "document",
    "consultation",
    "report",
)

# Plural aliases the parser accepts and normalises to the canonical
# singular form. The DSL is officially singular (``@document:UUID``)
# but users naturally write ``@documents:UUID`` when listing several
# items in one mention; rejecting the plural silently turns it into a
# broken plain-markdown link with garbage href. Normalising at parse
# time keeps the downstream pipeline (validator, lookup, renderer)
# unaware of the surface variation.
_KIND_ALIASES: dict[str, str] = {
    "studies": "study",
    "series": "series",  # already invariant
    "folders": "folder",
    "documents": "document",
    "consultations": "consultation",
    "reports": "report",
}


def _normalise_kind(raw: str) -> str:
    return _KIND_ALIASES.get(raw, raw)


# Two surface forms are accepted, in priority order:
#
#   ``[Title](@kind:UUID)`` — markdown link form. The editor's
#                             autocomplete inserts this so the
#                             user sees the resolved title in
#                             both WYSIWYG and raw-markdown modes.
#   ``@kind:UUID``          — bare form, kept as a fallback for
#                             content typed by hand or migrated.
#
# Both forms share the same kind + UUID character set, hyphenated
# canonical UUID; only the optional title differs.
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Kind alternatives, longest-first so ``studies`` wins over ``study``
# under the leftmost-longest semantics of the regex engine. ``series``
# is invariant between singular and plural and only appears once.
_KIND_PATTERN = (
    r"studies|study|series|folders|folder|documents|document|"
    r"consultations|consultation|reports|report"
)

_LINK_MENTION_RE = re.compile(
    r"\[(?P<title>[^\]\n]+)\]"
    rf"\(@(?P<kind>{_KIND_PATTERN}):(?P<uuid>{_UUID_PATTERN})\)"
)
_BARE_MENTION_RE = re.compile(rf"@(?P<kind>{_KIND_PATTERN}):(?P<uuid>{_UUID_PATTERN})")

# ``@tag:value`` — alphanumeric plus dash / underscore / dot / slash.
# Length cap (64) matches the ``Tag.value`` column. Same dual-form
# pattern: ``[label](@tag:value)`` for autocomplete-inserted tags
# and ``@tag:value`` bare form for plain text / migrated content.
#
# ``@tag:`` (instead of the original ``#tag``) avoids the markdown
# heading collision: ``# Heading`` at the start of a line should be
# an H1, not a tag chip.
_TAG_VALUE_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}"
_LINK_TAG_RE = re.compile(rf"\[(?P<title>[^\]\n]+)\]\(@tag:(?P<value>{_TAG_VALUE_PATTERN})\)")
_BARE_TAG_RE = re.compile(rf"@tag:(?P<value>{_TAG_VALUE_PATTERN})")


@dataclass(frozen=True, slots=True)
class Mention:
    """A parsed inline reference. ``kind == 'tag'`` for ``@tag:`` hits;
    otherwise one of :data:`MENTION_KINDS`. ``title`` is populated for
    the markdown-link form (``[Title](@kind:UUID)``) and ``None`` for
    the bare form — the read-side renderer falls back to a default
    label when ``title`` is missing.
    """

    kind: str
    raw: str
    target_id: uuid.UUID | None = None
    tag_value: str | None = None
    title: str | None = None


def parse_mentions(body: str) -> list[Mention]:
    """Walk the markdown body and return every mention in order.

    Two passes per kind: first the markdown-link form
    (``[Title](@kind:UUID)``) which we mark as covered, then the
    bare form (``@kind:UUID``) which skips spans already matched by
    the link form so we don't double-count. Pure regex; no DB hit.
    """
    out: list[Mention] = []
    covered: list[tuple[int, int]] = []

    for m in _LINK_MENTION_RE.finditer(body):
        out.append(
            Mention(
                kind=_normalise_kind(m.group("kind")),
                raw=m.group(0),
                target_id=uuid.UUID(m.group("uuid")),
                title=m.group("title"),
            )
        )
        covered.append((m.start(), m.end()))
    for m in _LINK_TAG_RE.finditer(body):
        out.append(
            Mention(
                kind="tag",
                raw=m.group(0),
                tag_value=m.group("value"),
                title=m.group("title"),
            )
        )
        covered.append((m.start(), m.end()))

    def _inside(pos: int) -> bool:
        return any(s <= pos < e for s, e in covered)

    for m in _BARE_MENTION_RE.finditer(body):
        if _inside(m.start()):
            continue
        out.append(
            Mention(
                kind=_normalise_kind(m.group("kind")),
                raw=m.group(0),
                target_id=uuid.UUID(m.group("uuid")),
            )
        )
    for m in _BARE_TAG_RE.finditer(body):
        if _inside(m.start()):
            continue
        out.append(
            Mention(
                kind="tag",
                raw=m.group(0),
                tag_value=m.group("value"),
            )
        )
    return out


async def _study_belongs_to(db: AsyncSession, study_id: uuid.UUID) -> uuid.UUID | None:
    row = (
        await db.execute(select(ImagingStudy.patient_id).where(ImagingStudy.id == study_id))
    ).first()
    return row[0] if row else None


async def _series_belongs_to(db: AsyncSession, series_id: uuid.UUID) -> uuid.UUID | None:
    row = (
        await db.execute(
            select(ImagingStudy.patient_id)
            .join(Series, Series.study_id == ImagingStudy.id)
            .where(Series.id == series_id)
        )
    ).first()
    return row[0] if row else None


async def _folder_belongs_to(db: AsyncSession, folder_id: uuid.UUID) -> uuid.UUID | None:
    row = (await db.execute(select(Folder.patient_id).where(Folder.id == folder_id))).first()
    return row[0] if row else None


async def _document_belongs_to(db: AsyncSession, doc_id: uuid.UUID) -> uuid.UUID | None:
    row = (await db.execute(select(Document.patient_id).where(Document.id == doc_id))).first()
    return row[0] if row else None


async def _consultation_belongs_to(
    db: AsyncSession, consultation_id: uuid.UUID
) -> uuid.UUID | None:
    row = (
        await db.execute(select(Consultation.patient_id).where(Consultation.id == consultation_id))
    ).first()
    return row[0] if row else None


async def _report_belongs_to(db: AsyncSession, report_id: uuid.UUID) -> uuid.UUID | None:
    row = (
        await db.execute(
            select(ImagingStudy.patient_id)
            .join(Report, Report.study_id == ImagingStudy.id)
            .where(Report.id == report_id)
        )
    ).first()
    return row[0] if row else None


_LOOKUP = {
    "study": _study_belongs_to,
    "series": _series_belongs_to,
    "folder": _folder_belongs_to,
    "document": _document_belongs_to,
    "consultation": _consultation_belongs_to,
    "report": _report_belongs_to,
}


@dataclass(frozen=True, slots=True)
class MentionViolation:
    """One mention that failed validation. Surfaced in the 422 detail
    so the editor can highlight the offending span(s)."""

    raw: str
    kind: str
    reason: str  # "not_found" | "cross_patient"


async def _classify(db: AsyncSession, patient_id: uuid.UUID, m: Mention) -> MentionViolation | None:
    if m.kind == "tag" or m.target_id is None:
        # Tags are not cross-patient validated here; their resolution
        # is patient-scoped by query at read time. Same goes for any
        # mention that somehow lacks a parsed UUID (defensive).
        return None
    lookup = _LOOKUP.get(m.kind)
    if lookup is None:
        return MentionViolation(raw=m.raw, kind=m.kind, reason="not_found")
    owner = await lookup(db, m.target_id)
    if owner is None:
        return MentionViolation(raw=m.raw, kind=m.kind, reason="not_found")
    if owner != patient_id:
        return MentionViolation(raw=m.raw, kind=m.kind, reason="cross_patient")
    return None


async def validate_mentions_or_raise(
    db: AsyncSession,
    *,
    patient_id: uuid.UUID,
    body: str,
) -> list[Mention]:
    """Parse + check every mention in ``body``. Returns the parsed
    list on success; raises ``HTTPException(422)`` on a **cross-patient**
    violation (the security-critical case) with the full list of
    violations in ``detail``.

    ``not_found`` violations (the referenced resource simply does not
    exist anymore — e.g. a document deleted from production before
    the git-like soft-delete model landed) are returned in the
    response payload as **warnings** and rendered inline by the FE
    as broken-strikethrough chips, but they do **not** block the
    save. Otherwise users would be unable to edit any narrative that
    happens to mention an artefact that has since been cleaned up,
    which is a far more common failure mode than an actual leak.

    The 422 payload shape (cross-patient case)::

        {
          "detail": {
            "code": "cross_patient_or_missing_link",
            "violations": [
              {"raw": "@study:UUID", "kind": "study", "reason": "cross_patient"}
            ]
          }
        }

    so the editor can map every offending span back to a token.
    """
    mentions = parse_mentions(body)
    blocking: list[MentionViolation] = []
    for m in mentions:
        v = await _classify(db, patient_id, m)
        if v is None:
            continue
        if v.reason == "cross_patient":
            # Hard block: data isolation across patient records is
            # the security invariant that motivates the validator in
            # the first place.
            blocking.append(v)
        # ``not_found`` falls through silently. The mention renderer
        # surfaces the broken state inline so the user can decide
        # whether to clean it up; but they should never be locked
        # out of editing because of a stale reference.
    if blocking:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "cross_patient_or_missing_link",
                "violations": [
                    {"raw": v.raw, "kind": v.kind, "reason": v.reason} for v in blocking
                ],
            },
        )
    return mentions


def iter_mention_kinds(mentions: Iterable[Mention]) -> set[str]:
    """Convenience helper for callers who want to know which kinds
    appear without re-walking the body."""
    return {m.kind for m in mentions}
