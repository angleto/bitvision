"""``bvphoenix-backfill embed`` candidate query is SOP-class aware.

Regression for Flow task 100ecc3b. The candidate query
(``cli.backfill._series_candidate_ids``) must mirror exactly what the
``embed_series`` worker will actually embed, so ``--only-missing``
*converges* instead of re-offering the same series forever:

* a series whose only instances are non-image SOP classes (Raw Data
  Storage ``.66``, or a SEG-modality series) is NEVER offered — without
  this the worker silently skips them at decode, no ``embeddings`` row is
  written, and the audit never reaches 0 (the original 16 TCIA ReMIND /
  UPENN-GBM MR series that churned every run);
* a series carrying at least one image instance (incl. an Enhanced MR
  ``.4.1`` that the worker decodes via its multi-frame path — see
  ``workers/tests/test_embed_series_skip.py``) IS offered;
* a mixed series (one non-image instance + one image instance) IS offered;
* ``--only-missing`` excludes a series that already has the ``biomedclip-v1``
  vector; ``--all-series`` re-offers it but still never the non-image ones.

The query runs over the real schema, so it is a DB integration test.
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SyncSession

from bvphoenix.cli.backfill import _IMAGE_MODEL_ID, _series_candidate_ids
from bvphoenix.config import get_settings
from bvphoenix.db.models import Instance, Series
from tests.conftest import skip_if_no_db

# SOP Class UIDs exercised by the candidate query.
RAW_DATA = "1.2.840.10008.5.1.4.1.1.66"  # Raw Data Storage — no diagnostic pixels
SEG = "1.2.840.10008.5.1.4.1.1.66.4"  # Segmentation — non-image raster
ENHANCED_MR = "1.2.840.10008.5.1.4.1.1.4.1"  # Enhanced MR (multi-frame) — embeddable
MR_IMAGE = "1.2.840.10008.5.1.4.1.1.4"  # MR Image — embeddable
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"  # CT Image — embeddable


def _uid() -> str:
    return f"1.2.840.{uuid.uuid4().int}"[:64]


async def _add_series(db, study, *, modality: str, sop_classes: list[str]) -> Series:
    """Create a Series under ``study`` with one Instance per SOP class."""
    series = Series(
        id=uuid.uuid4(),
        study_id=study.id,
        series_instance_uid=_uid(),
        modality=modality,
    )
    db.add(series)
    await db.flush()
    for i, scu in enumerate(sop_classes):
        db.add(
            Instance(
                id=uuid.uuid4(),
                series_id=series.id,
                sop_instance_uid=_uid(),
                sop_class_uid=scu,
                instance_number=i,
                s3_bucket="test-bucket",
                s3_key=f"instances/{uuid.uuid4()}.dcm",
            )
        )
    await db.flush()
    await db.commit()
    return series


@skip_if_no_db
async def test_candidate_query_is_sop_class_aware(
    db_session, make_user, make_study, make_embedding
):
    owner = await make_user()
    study, seed_series = await make_study(owner, modality="MR")
    pid = study.patient_id

    raw_only = await _add_series(db_session, study, modality="MR", sop_classes=[RAW_DATA, RAW_DATA])
    enhanced = await _add_series(db_session, study, modality="MR", sop_classes=[ENHANCED_MR])
    mixed = await _add_series(db_session, study, modality="MR", sop_classes=[SEG, MR_IMAGE])
    seg_modality = await _add_series(db_session, study, modality="SEG", sop_classes=[SEG])
    embedded = await _add_series(db_session, study, modality="CT", sop_classes=[CT_IMAGE])
    await make_embedding(embedded, model_id=_IMAGE_MODEL_ID)

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    try:
        with SyncSession(engine) as s:
            missing = set(_series_candidate_ids(s, patient_id=pid, only_missing=True))
            all_series = set(_series_candidate_ids(s, patient_id=pid, only_missing=False))
    finally:
        engine.dispose()

    # --only-missing converges: real images offered, non-image / already-done excluded.
    assert enhanced.id in missing  # Enhanced MR decodes via the worker multi-frame path
    assert mixed.id in missing  # has ≥1 image instance despite the SEG sibling
    assert raw_only.id not in missing  # Raw Data Storage only -> never offered (the churn bug)
    assert seg_modality.id not in missing  # non-image modality -> excluded up front
    assert embedded.id not in missing  # already has the biomedclip-v1 vector
    assert seed_series.id not in missing  # no instances at all -> EXISTS fails

    # --all-series re-offers the embedded image series, but the SOP-class /
    # modality exclusions are unconditional — they hold regardless of coverage.
    assert embedded.id in all_series
    assert enhanced.id in all_series
    assert mixed.id in all_series
    assert raw_only.id not in all_series
    assert seg_modality.id not in all_series
