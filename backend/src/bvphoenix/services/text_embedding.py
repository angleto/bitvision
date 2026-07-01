"""Coarse, whole-object text embedding for /search/semantic targets.

The chunk pipeline (``workers.tasks.chunk_and_embed``) embeds *chunks*
(``target_kind='document_chunk'``) so Q&A retrieval can cite passages.
Separately, ``/search/semantic`` has a *coarse* arm that matches a single
whole-object vector per target — ``target_kind`` in {``document``,
``patient``, ``report_content``, ``finding``, ...}. Those coarse rows were
historically produced by nothing (only ``finding`` got one, via a
hard-coded MiniLM enqueue), so the coarse search arms returned empty.

This module is the single home for the on-write coarse path:

* :func:`enqueue_text_embed` fans an embed job out to EVERY routed, active
  text model in the ``embedding_models`` registry (``spec.arq_task``). One
  call lands the target in the MiniLM store today and *additionally* in the
  BGE-M3 store the moment that model is activated — no call-site change.
* :func:`patient_embed_text` / :func:`report_content_embed_text` /
  :func:`finding_embed_text` compose the coarse free-text for the synchronous
  API write paths. (Documents have no synchronous text — OCR is async — so the
  chunk worker enqueues their coarse vector post-OCR.) ``finding_embed_text``
  is the single source of truth shared by the on-write path
  (``api.findings._enqueue_finding_embed``) and the catch-up backfill
  (``bvphoenix-backfill embed-findings``), so a re-embed is byte-identical.

Contract (mirrors ``api.findings._enqueue_finding_embed``): best-effort —
a failure never breaks the originating write; the backfill CLI
(``bvphoenix-backfill embed-text``) is the catch-up path. Blank text is a
no-op. The target row is NOT re-read: the caller passes the composed text,
and the worker upserts on ``(target_kind, target_id, model_id)`` so
re-enqueues are idempotent.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.text_models import load_text_model_specs

if TYPE_CHECKING:
    from bvphoenix.db.models import Patient, ReportContent

logger = logging.getLogger(__name__)


def patient_embed_text(patient: Patient) -> str:
    """Coarse free-text for a patient's whole-object semantic vector:
    display name + clinician notes. Blank when neither is set — the embed
    helper then no-ops."""
    return "\n\n".join(p for p in (patient.display_name, patient.notes) if p)


def report_content_embed_text(rc: ReportContent) -> str:
    """Coarse free-text for a report content's whole-object semantic vector,
    mirroring the chunk pipeline's composition: title + narrative + findings
    + recommendations. Blank fields are skipped; an all-blank row no-ops."""
    parts = [rc.title, rc.narrative_md, rc.findings_md, rc.recommendations_md]
    return "\n\n".join(p for p in parts if p)


def finding_embed_text(
    *,
    type_display: str,
    anatomy_display: str | None,
    laterality: str | None,
    morphology: list[str],
    description: str | None,
) -> str:
    """Coarse free-text for a finding's whole-object semantic vector: the
    coded type, anatomy (+ laterality), morphology descriptors and the
    free-text description. Shared by the on-write path and the backfill CLI
    so a re-embed is byte-identical. Blank when nothing is set — the embed
    helper then no-ops."""
    parts = [type_display]
    if anatomy_display:
        parts.append(anatomy_display + (f" {laterality}" if laterality else ""))
    if morphology:
        parts.append(", ".join(morphology))
    text = "; ".join(p for p in parts if p)
    if description:
        text = f"{text}. {description}" if text else description
    return text.strip()


def study_embed_text(
    *,
    study_description: str | None,
    modalities: list[str] | None,
    body_parts: list[str] | None,
) -> str:
    """Coarse free-text for a study's whole-object semantic vector, composed
    from its STRUCTURAL metadata: description + modalities + the distinct body
    parts of its series. This is what gives EVERY study — including the public
    OpenData studies that carry no report or finding — a dense-text vector for
    the ``/search/hybrid`` ``text_dense`` arm. Blank when nothing is set (the
    embed helper then no-ops). Order-preserving de-dupe on the list parts."""
    parts: list[str] = []
    if study_description:
        parts.append(study_description)
    if modalities:
        parts.append(", ".join(dict.fromkeys(m for m in modalities if m)))
    if body_parts:
        parts.append(", ".join(dict.fromkeys(b for b in body_parts if b)))
    return "; ".join(p for p in parts if p).strip()


async def enqueue_study_embed(db: AsyncSession, study_id: uuid.UUID | str) -> None:
    """Compose a study's coarse text (description + modalities + series body
    parts) and enqueue its ``target_kind='study'`` vector on every active text
    model. Best-effort: a failure never breaks the originating write; the
    backfill CLI (``bvphoenix-backfill embed-studies``) is the catch-up path.
    Idempotent (the worker upserts on ``(target_kind, target_id, model_id)``)."""
    try:
        from sqlalchemy import select

        from bvphoenix.db.models import ImagingStudy, Series

        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
        ).scalar_one_or_none()
        if study is None:
            return
        body_parts = list(
            (
                await db.execute(
                    select(Series.body_part_examined)
                    .where(
                        Series.study_id == study.id,
                        Series.body_part_examined.isnot(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        text_value = study_embed_text(
            study_description=study.study_description,
            modalities=list(study.modalities or []),
            body_parts=body_parts,
        )
        await enqueue_text_embed(db, target_kind="study", target_id=study.id, text=text_value)
    except Exception:  # pragma: no cover — best-effort, never break the write
        logger.exception("study embed enqueue failed for %s", study_id)


async def enqueue_text_embed(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID | str,
    text: str,
) -> None:
    """Enqueue one coarse text-embedding job per active text model.

    ``db`` resolves the active, routed model set from the registry. A
    short-lived arq pool is opened and closed per call. Best-effort: any
    failure is logged and swallowed so the originating write still commits.
    """
    body = (text or "").strip()
    if not body:
        return
    try:
        specs = list((await load_text_model_specs(db)).values())
        if not specs:
            return

        from arq import create_pool

        from bvphoenix.config import get_settings
        from bvphoenix.services.arq_redis import redis_settings

        redis = await create_pool(redis_settings(get_settings().redis_url))
        try:
            for spec in specs:
                await redis.enqueue_job(spec.arq_task, target_kind, str(target_id), body)
        finally:
            await redis.close()
    except Exception:  # pragma: no cover — best-effort, never break the write
        logger.exception("text embed enqueue failed for %s %s", target_kind, target_id)
