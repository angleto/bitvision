"""DB orchestration for the contrast-phase classifier.

Thin glue between the pure classifier (:mod:`bvphoenix.services.contrast_phase`)
and the ``series`` table: load a study's series, classify, and persist
``acquisition_phase`` / ``phase_confidence`` / ``phase_source='auto'``.

Two invariants:
* A human override (``phase_source='human'``) is NEVER clobbered by the
  auto-classifier unless ``force=True`` (e.g. an explicit re-detect the
  human asked for). This is the spine of the "candidate + human confirm"
  model: once a clinician fixes a phase, re-running detection respects it.
* It only consumes the persisted series columns (description, timing,
  contrast agent). ProtocolName and per-instance AcquisitionTime are NOT
  read from S3 here — for legacy series whose timing columns are still
  NULL (ingested before Phase 0) classification falls back to description
  + agent only. A header-reading backfill that populates the timing
  columns for legacy series is a separate, explicit step (not silently
  faked here).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import Series
from bvphoenix.services.contrast_phase import (
    SeriesPhaseInput,
    SeriesPhaseResult,
    classify_study_phases,
)


def _input_from_row(row: Series) -> SeriesPhaseInput:
    return SeriesPhaseInput(
        series_id=row.id,
        modality=row.modality,
        series_number=row.series_number,
        body_part_examined=row.body_part_examined,
        series_description=row.series_description,
        protocol_name=None,  # not persisted; description-driven here
        acquisition_time_of_day=row.acquisition_time_of_day,
        contrast_bolus_agent=row.contrast_bolus_agent,
        contrast_bolus_start_time=row.contrast_bolus_start_time,
    )


async def classify_and_persist_study(
    db: AsyncSession,
    study_id: uuid.UUID,
    *,
    force: bool = False,
) -> list[SeriesPhaseResult]:
    """Classify the CT series of a study and persist the auto labels.

    Returns the per-series results (input order). Does not commit — the
    caller owns the transaction boundary.
    """
    rows = list(
        (
            await db.execute(
                select(Series)
                .where(Series.study_id == study_id)
                .order_by(Series.series_number.asc().nullslast(), Series.id)
            )
        )
        .scalars()
        .all()
    )
    results = classify_study_phases([_input_from_row(r) for r in rows])
    by_id = {r.series_id: r for r in results}

    for row in rows:
        if row.phase_source == "human" and not force:
            continue  # never clobber a human override
        res = by_id[row.id]
        if res.acquisition_phase is None:
            # Clear a stale auto label so the manifest reflects reality;
            # leave human/None rows untouched.
            if row.phase_source == "auto":
                row.acquisition_phase = None
                row.phase_confidence = None
                row.phase_source = None
            continue
        row.acquisition_phase = res.acquisition_phase
        row.phase_confidence = res.confidence
        row.phase_source = "auto"

    await db.flush()
    return results
