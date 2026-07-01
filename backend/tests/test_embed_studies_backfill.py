"""``bvphoenix-backfill embed-studies`` candidate query (Flow 0ece383b).

The catch-up backfill offers studies lacking a coarse ``study`` text vector,
composing each study's text (description + modalities + distinct series body
parts) via the shared ``study_embed_text``, and CONVERGES under
``--only-missing``. The novel bit vs the finding backfill is the body-part
``array_agg`` over the study's series.

DB integration test (runs over the real schema).
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SyncSession

from bvphoenix.cli.backfill import _study_candidates
from bvphoenix.config import get_settings
from bvphoenix.db.models.text_embeddings import TEXT_EMBEDDING_DIM, TextEmbedding
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db

_STORE = "text_embeddings"
_MODEL = "minilm-multi-v1"


async def test_study_candidates_compose_bodyparts_and_converge(db_session, make_user, make_study):
    owner = await make_user()
    # make_study seeds one series with body_part=CHEST, modality=CT.
    study, _series = await make_study(
        owner, description="TC torace", modality="CT", body_part="CHEST"
    )
    pid = study.patient_id

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    try:
        with SyncSession(engine) as s:
            missing = _study_candidates(
                s, patient_id=pid, only_missing=True, store_table=_STORE, model_id=_MODEL
            )
        by_id = dict(missing)
        assert study.id in by_id
        # description + modalities + distinct series body parts.
        assert by_id[study.id] == "TC torace; CT; CHEST"

        # Embed it → --only-missing drops it (converge); --all-studies re-offers.
        db_session.add(
            TextEmbedding(
                id=uuid.uuid4(),
                target_kind="study",
                target_id=study.id,
                model_id=_MODEL,
                vector=[0.0] * TEXT_EMBEDDING_DIM,
            )
        )
        await db_session.commit()

        with SyncSession(engine) as s:
            missing2 = {
                sid
                for sid, _ in _study_candidates(
                    s, patient_id=pid, only_missing=True, store_table=_STORE, model_id=_MODEL
                )
            }
            all_studies = {
                sid
                for sid, _ in _study_candidates(
                    s, patient_id=pid, only_missing=False, store_table=_STORE, model_id=_MODEL
                )
            }
    finally:
        engine.dispose()

    assert study.id not in missing2  # already embedded → not re-offered
    assert study.id in all_studies  # --all-studies ignores coverage
