"""Cascade summarizer — series → study → patient.

The three ``summarize_*`` functions form a strict hierarchy:

* :func:`summarize_series` reads the raw fascicolo of one series
  (description + annotations + related study reports) and asks the LLM
  for a compact descriptor. This is the leaf of the cascade — every
  other level delegates to it.
* :func:`summarize_study` aggregates the already-minted *series*
  summaries for each series in the study. If any series has no cached
  summary (or its cache is stale), we recurse into :func:`summarize_series`
  for that one series only. The study-level LLM call then sees a few
  paragraphs of pre-digested text instead of dozens of raw annotations
  — this is where the token-saving lives.
* :func:`summarize_patient` does the same trick one level up: it reads
  every study summary, the patient demographics, free-standing patient
  documents, and any study reports, then builds a patient-level digest.

``source_version_hash`` (SHA-256 hex) is computed over the logical input
set at each level so identical inputs never trigger a fresh LLM call.
The hash composition is layer-specific — see each ``_hash_*`` helper —
and the same helper is used by ``api/summaries.py`` to answer "is the
cache still valid?" without repeating the summary build.

All public functions accept an ``AsyncSession`` and return the persisted
:class:`Summary` row. Callers are responsible for their own transaction
boundaries: the service calls ``commit`` exactly once per top-level
invocation after the row is inserted, which is the standard pattern used
across the backend (e.g. ``services/consent_snapshot``).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import (
    Document,
    ImagingStudy,
    Patient,
    Series,
    Summary,
)
from bvphoenix.services.billing import debit_llm_call
from bvphoenix.services.credits import InsufficientCreditsError
from bvphoenix.services.llm import get_llm_provider
from bvphoenix.services.sponsorship import ScopeMatch

logger = logging.getLogger(__name__)

# --- prompts ---------------------------------------------------------------

_SYSTEM_SERIES = (
    "You are a clinical imaging assistant. Draft a concise, conservative "
    "summary of the DICOM series described below. Do not invent findings. "
    "Reply with plain prose, one short paragraph."
)

_SYSTEM_STUDY = (
    "You are a clinical imaging assistant. The user will paste a list of "
    "per-series summaries from the same study. Aggregate them into a "
    "single study-level summary. Do not invent findings; preserve any "
    "uncertainty expressed in the input. One short paragraph."
)

_SYSTEM_PATIENT = (
    "You are a clinical assistant. You will be given demographic data and "
    "a list of study-level summaries, reports, and ancillary documents "
    "for one patient. Produce a patient-level summary: key history, "
    "imaging context, and any pending follow-up. Be conservative — do "
    "not extrapolate beyond what is stated. Two short paragraphs maximum."
)


# --- hash helpers ----------------------------------------------------------


def _digest(parts: Iterable[str]) -> str:
    """SHA-256 over the ``|``-joined parts. Trivial helper kept here so
    every level hashes its inputs the same way."""
    h = hashlib.sha256()
    h.update("|".join(parts).encode("utf-8"))
    return h.hexdigest()


def _iso(dt: datetime | None) -> str:
    return dt.astimezone(UTC).isoformat() if dt else "-"


async def _hash_series(db: AsyncSession, series: Series) -> str:
    """series hash = description + latest report updated_at on the
    parent study. The values are all strings so the digest is stable
    across Python sessions.
    """
    last_report = (
        await db.execute(
            select(Report.created_at)
            .where(Report.study_id == series.study_id)
            .order_by(Report.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _digest(
        [
            "series",
            str(series.id),
            series.series_description or "",
            series.modality or "",
            series.body_part_examined or "",
            _iso(last_report),
        ]
    )


def _hash_study(study: ImagingStudy, series_summaries: list[Summary]) -> str:
    """study hash = sorted (series_summary_id, updated_at) tuples + the
    study's own description. Changing a series summary bumps the hash;
    adding a new series to the study likewise.
    """
    parts: list[str] = [
        "study",
        str(study.id),
        study.study_description or "",
    ]
    for s in sorted(series_summaries, key=lambda r: str(r.id)):
        parts.append(str(s.id))
        parts.append(_iso(s.updated_at))
    return _digest(parts)


def _hash_patient(
    patient: Patient,
    study_summaries: list[Summary],
    docs: list[Document],
) -> str:
    parts: list[str] = [
        "patient",
        str(patient.id),
        patient.display_name or "",
        str(patient.birth_date) if patient.birth_date else "",
        patient.sex or "",
        patient.blood_type or "",
        patient.allergies or "",
    ]
    for s in sorted(study_summaries, key=lambda r: str(r.id)):
        parts.append(str(s.id))
        parts.append(_iso(s.updated_at))
    for d in sorted(docs, key=lambda r: str(r.id)):
        parts.append(str(d.id))
        parts.append(_iso(d.created_at))
    return _digest(parts)


# --- cache lookup ----------------------------------------------------------


async def get_cached_summary(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    lang: str,
    source_version_hash: str,
) -> Summary | None:
    """Return the cached summary row for (kind, id, lang, hash) or None."""
    return (
        await db.execute(
            select(Summary).where(
                Summary.target_kind == target_kind,
                Summary.target_id == target_id,
                Summary.lang == lang,
                Summary.source_version_hash == source_version_hash,
            )
        )
    ).scalar_one_or_none()


async def compute_source_hash(db: AsyncSession, *, target_kind: str, target_id: uuid.UUID) -> str:
    """Public helper — lets the API answer "has anything changed?"
    without committing to a full summarize pass.
    """
    if target_kind == "series":
        series = (
            await db.execute(select(Series).where(Series.id == target_id))
        ).scalar_one_or_none()
        if series is None:
            raise LookupError("series not found")
        return await _hash_series(db, series)
    if target_kind == "study":
        study = (
            await db.execute(select(ImagingStudy).where(ImagingStudy.id == target_id))
        ).scalar_one_or_none()
        if study is None:
            raise LookupError("study not found")
        series_rows = (
            (await db.execute(select(Series).where(Series.study_id == study.id))).scalars().all()
        )
        series_summaries: list[Summary] = []
        for s in series_rows:
            # For hash purposes we only peek at the latest cached row
            # per series. Missing summaries are reflected as their own
            # sentinel entry so the hash still changes once they land.
            row = (
                await db.execute(
                    select(Summary)
                    .where(
                        Summary.target_kind == "series",
                        Summary.target_id == s.id,
                    )
                    .order_by(Summary.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                series_summaries.append(row)
        return _hash_study(study, series_summaries)
    if target_kind == "patient":
        patient = (
            await db.execute(select(Patient).where(Patient.id == target_id))
        ).scalar_one_or_none()
        if patient is None:
            raise LookupError("patient not found")
        studies = (
            (await db.execute(select(ImagingStudy).where(ImagingStudy.patient_id == patient.id)))
            .scalars()
            .all()
        )
        study_summaries: list[Summary] = []
        for st in studies:
            row = (
                await db.execute(
                    select(Summary)
                    .where(
                        Summary.target_kind == "study",
                        Summary.target_id == st.id,
                    )
                    .order_by(Summary.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                study_summaries.append(row)
        docs = (
            (await db.execute(select(Document).where(Document.patient_id == patient.id)))
            .scalars()
            .all()
        )
        return _hash_patient(patient, study_summaries, docs)
    raise ValueError(f"unsupported target_kind: {target_kind}")


# --- upsert helper ---------------------------------------------------------


async def _upsert_summary(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    lang: str,
    text: str,
    model_id: str | None,
    provider: str | None,
    token_usage: dict | None,
    source_version_hash: str,
) -> Summary:
    """Insert a new Summary row, or return the existing one if an
    identical (kind, id, lang, hash) row is already present.

    The upsert is done via a fresh ``SELECT`` under the same session —
    Postgres would also let us do ``INSERT ... ON CONFLICT``, but the
    select-first pattern keeps the ORM happy and matches the style used
    in :mod:`bvphoenix.services.consent_snapshot`.
    """
    existing = await get_cached_summary(
        db,
        target_kind=target_kind,
        target_id=target_id,
        lang=lang,
        source_version_hash=source_version_hash,
    )
    if existing is not None:
        return existing

    now = datetime.now(UTC)
    row = Summary(
        target_kind=target_kind,
        target_id=target_id,
        lang=lang,
        text=text,
        model_id=model_id,
        provider=provider,
        token_usage=token_usage,
        source_version_hash=source_version_hash,
        updated_at=now,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


# --- public entry points ---------------------------------------------------


async def summarize_series(
    db: AsyncSession,
    series_id: uuid.UUID,
    lang: str = "en",
    *,
    force_refresh: bool = False,
    user_subject_id: uuid.UUID | None = None,
) -> Summary:
    """Mint (or reuse) a summary for one DICOM series.

    ``user_subject_id`` is the wallet to bill when the call actually
    hits the LLM. ``None`` skips billing entirely and is the default
    for legacy callers (worker jobs enqueued before F7.3 wiring landed,
    or tests that don't care about the ledger). Cache hits never bill,
    regardless.
    """
    series = (await db.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if series is None:
        raise LookupError("series not found")

    source_hash = await _hash_series(db, series)
    if not force_refresh:
        cached = await get_cached_summary(
            db,
            target_kind="series",
            target_id=series.id,
            lang=lang,
            source_version_hash=source_hash,
        )
        if cached is not None:
            return cached

    # Gather raw material: description + reports.
    reports = (
        (
            await db.execute(
                select(Report)
                .where(Report.study_id == series.study_id)
                .order_by(Report.version.desc())
                .limit(3)
            )
        )
        .scalars()
        .all()
    )

    lines: list[str] = [
        f"Language: {lang}",
        f"Modality: {series.modality or 'unknown'}",
        f"Body part: {series.body_part_examined or 'unknown'}",
        f"Description: {series.series_description or '(none)'}",
    ]
    if reports:
        lines.append("Most recent reports:")
        for r in reports:
            lines.append(f"- v{r.version}: {r.text[:800]}")

    provider_impl = get_llm_provider()
    result = await provider_impl.summarize(
        system=_SYSTEM_SERIES,
        user_prompt="\n".join(lines),
        max_tokens=512,
    )

    if force_refresh:
        # Invalidate any pre-existing row for this (kind, id, lang) so
        # UI consumers that bind to the latest row see the refreshed
        # text even when the new hash happens to match the old one.
        await _delete_prior_summaries(db, target_kind="series", target_id=series.id, lang=lang)

    row = await _upsert_summary(
        db,
        target_kind="series",
        target_id=series.id,
        lang=lang,
        text=result.text,
        model_id=result.model_id,
        provider=_provider_name(),
        token_usage=result.token_usage,
        source_version_hash=source_hash,
    )
    series_patient_id = (
        await db.execute(select(ImagingStudy.patient_id).where(ImagingStudy.id == series.study_id))
    ).scalar_one_or_none()
    await _debit_for_summary(
        db,
        user_subject_id=user_subject_id,
        summary=row,
        model_id=result.model_id,
        token_usage=result.token_usage,
        patient_id=series_patient_id,
    )
    await db.commit()
    await db.refresh(row)
    return row


async def summarize_study(
    db: AsyncSession,
    study_id: uuid.UUID,
    lang: str = "en",
    *,
    force_refresh: bool = False,
    user_subject_id: uuid.UUID | None = None,
) -> Summary:
    """Aggregate per-series summaries into a study-level digest.

    See :func:`summarize_series` for the ``user_subject_id`` semantics.
    The cascade propagates the id into inline series-level calls so a
    single request bills the caller for every LLM hit it triggered.
    """
    study = (
        await db.execute(select(ImagingStudy).where(ImagingStudy.id == study_id))
    ).scalar_one_or_none()
    if study is None:
        raise LookupError("study not found")

    series_rows = (
        (await db.execute(select(Series).where(Series.study_id == study.id))).scalars().all()
    )

    # Ensure each series has a fresh-enough summary cached — this is the
    # heart of the cascade. Missing or stale series summaries are filled
    # in inline so the study-level call always sees a full set.
    series_summaries: list[Summary] = []
    for s in series_rows:
        ser_hash = await _hash_series(db, s)
        cached = await get_cached_summary(
            db,
            target_kind="series",
            target_id=s.id,
            lang=lang,
            source_version_hash=ser_hash,
        )
        if cached is None:
            cached = await summarize_series(db, s.id, lang=lang, user_subject_id=user_subject_id)
        series_summaries.append(cached)

    source_hash = _hash_study(study, series_summaries)
    if not force_refresh:
        cached_study = await get_cached_summary(
            db,
            target_kind="study",
            target_id=study.id,
            lang=lang,
            source_version_hash=source_hash,
        )
        if cached_study is not None:
            return cached_study

    # Build the aggregation prompt from already-condensed series text.
    lines: list[str] = [
        f"Language: {lang}",
        f"ImagingStudy description: {study.study_description or '(none)'}",
        f"ImagingStudy date: {study.study_date.isoformat() if study.study_date else 'unknown'}",
        f"Modalities: {', '.join(study.modalities or []) or 'unknown'}",
        "",
        "Series summaries:",
    ]
    for idx, ss in enumerate(series_summaries, start=1):
        lines.append(f"[{idx}] {ss.text}")

    provider_impl = get_llm_provider()
    result = await provider_impl.summarize(
        system=_SYSTEM_STUDY,
        user_prompt="\n".join(lines),
        max_tokens=768,
    )

    if force_refresh:
        await _delete_prior_summaries(db, target_kind="study", target_id=study.id, lang=lang)

    row = await _upsert_summary(
        db,
        target_kind="study",
        target_id=study.id,
        lang=lang,
        text=result.text,
        model_id=result.model_id,
        provider=_provider_name(),
        token_usage=result.token_usage,
        source_version_hash=source_hash,
    )
    await _debit_for_summary(
        db,
        user_subject_id=user_subject_id,
        summary=row,
        model_id=result.model_id,
        token_usage=result.token_usage,
        patient_id=study.patient_id,
    )
    await db.commit()
    await db.refresh(row)
    return row


async def summarize_patient(
    db: AsyncSession,
    patient_id: uuid.UUID,
    lang: str = "en",
    *,
    force_refresh: bool = False,
    user_subject_id: uuid.UUID | None = None,
) -> Summary:
    """Aggregate study summaries + demographics + documents into a
    patient-level digest.

    See :func:`summarize_series` for the ``user_subject_id`` semantics.
    The id is forwarded into the study-level cascade so every LLM call
    in the tree bills the same wallet.
    """
    patient = (
        await db.execute(select(Patient).where(Patient.id == patient_id))
    ).scalar_one_or_none()
    if patient is None:
        raise LookupError("patient not found")

    studies = (
        (await db.execute(select(ImagingStudy).where(ImagingStudy.patient_id == patient.id)))
        .scalars()
        .all()
    )

    # ``summarize_study`` already owns the series-level cascade (cache
    # check + inline regeneration), so delegating here keeps a single
    # copy of the invalidation logic.
    study_summaries: list[Summary] = [
        await summarize_study(db, st.id, lang=lang, user_subject_id=user_subject_id)
        for st in studies
    ]

    docs = (
        (await db.execute(select(Document).where(Document.patient_id == patient.id)))
        .scalars()
        .all()
    )

    source_hash = _hash_patient(patient, study_summaries, docs)
    if not force_refresh:
        cached_pt = await get_cached_summary(
            db,
            target_kind="patient",
            target_id=patient.id,
            lang=lang,
            source_version_hash=source_hash,
        )
        if cached_pt is not None:
            return cached_pt

    lines: list[str] = [
        f"Language: {lang}",
        f"Patient: {patient.display_name}",
    ]
    if patient.birth_date:
        lines.append(f"Birth date: {patient.birth_date.isoformat()}")
    if patient.sex:
        lines.append(f"Sex: {patient.sex}")
    if patient.blood_type:
        lines.append(f"Blood type: {patient.blood_type}")
    if patient.allergies:
        lines.append(f"Allergies: {patient.allergies}")
    if patient.notes:
        lines.append(f"Notes: {patient.notes}")

    lines.append("")
    lines.append("ImagingStudy summaries:")
    for idx, ss in enumerate(study_summaries, start=1):
        lines.append(f"[{idx}] {ss.text}")

    if docs:
        lines.append("")
        lines.append("Documents:")
        for d in docs:
            body = (d.text or "").strip().replace("\n", " ")[:400]
            lines.append(f"- [{d.kind_id}] {d.title}: {body}")

    provider_impl = get_llm_provider()
    result = await provider_impl.summarize(
        system=_SYSTEM_PATIENT,
        user_prompt="\n".join(lines),
        max_tokens=1024,
    )

    if force_refresh:
        await _delete_prior_summaries(db, target_kind="patient", target_id=patient.id, lang=lang)

    row = await _upsert_summary(
        db,
        target_kind="patient",
        target_id=patient.id,
        lang=lang,
        text=result.text,
        model_id=result.model_id,
        provider=_provider_name(),
        token_usage=result.token_usage,
        source_version_hash=source_hash,
    )
    await _debit_for_summary(
        db,
        user_subject_id=user_subject_id,
        summary=row,
        model_id=result.model_id,
        token_usage=result.token_usage,
        patient_id=patient.id,
    )
    await db.commit()
    await db.refresh(row)
    return row


# --- internals -------------------------------------------------------------


async def _debit_for_summary(
    db: AsyncSession,
    *,
    user_subject_id: uuid.UUID | None,
    summary: Summary,
    model_id: str | None,
    token_usage: dict | None,
    patient_id: uuid.UUID | None = None,
) -> None:
    """Post-LLM debit helper shared by all three public entry points.

    On ``InsufficientCreditsError`` we log and swallow so the freshly-
    minted summary still persists. The LLM has already been invoked and
    the upstream API was paid; refusing to commit the summary here would
    throw that work away without refunding the platform. A follow-up
    can tighten this to a pre-flight balance check.

    ``patient_id`` opts the call into the wallet sponsorship resolver:
    when set, the patient owner's wallet (or any global pool) is
    consulted before falling back to the caller's own balance. Omitting
    it preserves the legacy "caller pays" behaviour.
    """
    if user_subject_id is None:
        return
    scopes: list[ScopeMatch] | None = None
    if patient_id is not None:
        scopes = [
            ScopeMatch(scope_kind="patient", scope_id=patient_id),
            ScopeMatch(scope_kind="global", scope_id=None),
        ]
    try:
        await debit_llm_call(
            db,
            user_subject_id=user_subject_id,
            model_id=model_id,
            token_usage=token_usage,
            is_byok=False,
            reference_kind="summary",
            reference_id=summary.id,
            scopes=scopes,
        )
    except InsufficientCreditsError as exc:
        logger.warning(
            "llm debit refused for user=%s summary=%s: %s",
            user_subject_id,
            summary.id,
            exc,
        )


async def _delete_prior_summaries(
    db: AsyncSession,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    lang: str,
) -> None:
    """Drop every existing (kind, id, lang) row. Only called on
    ``force_refresh`` paths so the fresh row replaces the previous one
    even when hashes accidentally collide (e.g. identical inputs but a
    user-driven retry)."""
    rows = (
        (
            await db.execute(
                select(Summary).where(
                    Summary.target_kind == target_kind,
                    Summary.target_id == target_id,
                    Summary.lang == lang,
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        await db.delete(r)
    await db.flush()


def _provider_name() -> str | None:
    """Best-effort label for the active LLM provider. Kept as a tiny
    helper so we don't scatter ``get_settings()`` imports through the
    summarizer."""
    from bvphoenix.config import get_settings

    s = get_settings()
    return s.llm_provider or None
