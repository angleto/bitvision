"""Document reference inventory + delete-block guard.

Centralised place that answers "what *clinically* references this
document?". Three reference shapes are tracked, all blocking soft-delete
unless the caller is opting into the admin GDPR-purge path:

1. ``document_study_links`` — every link_kind except ``mentions``
   counts as a live reference. ``mentions`` is the transient
   "I referenced this in passing" flavour and does not block.
2. ``content_document_links`` — every role counts (``extracted_from``,
   ``cites``, ``mentions``). The ``mentions`` role here is per-content
   (not per-study) and the user expects to clean them up before
   tombstoning the document, so we keep it blocking.
3. ``report_content_citations`` — only rows with
   ``target_kind = 'document'`` and ``target_id = doc_id`` count.

Folder containment is *not* a clinical reference: the smart-delete
flow at the API layer is responsible for orchestrating the unlink-from-
folder vs delete-document split. This module never looks at
``folder_items``.

The 409 payload returned to the client carries enough context for the
UI to navigate to the offending entity and remove the reference there
(see ``BlockingReferencesModal``).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import DocumentStudyLink
from bvphoenix.db.models.report_contents import ContentDocumentLink, ReportContentCitation


class BlockingRef(TypedDict):
    """Structured row in the 409 response. ``detail_url`` is the
    relative path the FE can route to so the user fixes the
    reference at its source."""

    kind: Literal["study_link", "content_link", "citation"]
    id: str
    label: str
    detail_url: str
    extra: dict[str, Any]


# Mentions on document_study_links are intentionally allowed to dangle:
# they are non-load-bearing and removing them as a precondition would
# generate friction without protecting any clinical artefact. All
# other link kinds block.
_BLOCKING_STUDY_LINK_KINDS: frozenset[str] = frozenset(
    {"primary_report", "addendum", "second_opinion", "extracted_from", "cites"}
)


async def collect_blocking_references(
    db: AsyncSession,
    doc_id: uuid.UUID,
) -> list[BlockingRef]:
    """Return the list of clinical references that currently block a
    soft-delete of ``doc_id``. Empty list means the delete is free to
    proceed.

    The query batch is intentionally small (3 SELECTs, each on an
    indexed column). For the common case of "no references at all"
    the function returns an empty list after three index probes — well
    under the 50ms SLA of the delete route.
    """
    blocking: list[BlockingRef] = []

    study_links = (
        (await db.execute(select(DocumentStudyLink).where(DocumentStudyLink.document_id == doc_id)))
        .scalars()
        .all()
    )
    for link in study_links:
        if link.link_kind not in _BLOCKING_STUDY_LINK_KINDS:
            continue
        blocking.append(
            BlockingRef(
                kind="study_link",
                id=str(link.id),
                label=f"Studio (link_kind={link.link_kind})",
                detail_url=f"/studies/{link.study_id}",
                extra={
                    "study_id": str(link.study_id),
                    "link_kind": link.link_kind,
                },
            )
        )

    content_links = (
        (
            await db.execute(
                select(ContentDocumentLink).where(ContentDocumentLink.document_id == doc_id)
            )
        )
        .scalars()
        .all()
    )
    for cl in content_links:
        blocking.append(
            BlockingRef(
                kind="content_link",
                id=str(cl.id),
                label=f"ReportContent (role={cl.role})",
                detail_url=f"/report-contents/{cl.report_content_id}",
                extra={
                    "report_content_id": str(cl.report_content_id),
                    "role": cl.role,
                },
            )
        )

    citations = (
        (
            await db.execute(
                select(ReportContentCitation).where(
                    ReportContentCitation.target_kind == "document",
                    ReportContentCitation.target_id == doc_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for cit in citations:
        blocking.append(
            BlockingRef(
                kind="citation",
                id=str(cit.id),
                label="Citazione granulare in report",
                detail_url=f"/report-contents/{cit.report_content_id}",
                extra={
                    "report_content_id": str(cit.report_content_id),
                    "page": cit.page,
                },
            )
        )

    return blocking


__all__ = ["BlockingRef", "collect_blocking_references"]
