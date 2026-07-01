"""``bvphoenix-backfill embed-findings`` candidate query (Flow c390b2a5).

The catch-up backfill must offer exactly the pre-existing findings that
lack a coarse text vector for the chosen model, compose their text with the
SAME ``finding_embed_text`` the on-write path uses, and CONVERGE under
``--only-missing`` (a finding already embedded is not re-offered). Mirrors
the well-tested ``_series_candidate_ids`` / ``_chunk_candidates`` shape.

DB integration test (runs over the real schema).
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SyncSession

from bvphoenix.cli.backfill import _finding_candidates
from bvphoenix.config import get_settings
from bvphoenix.db.models import Finding, FindingType
from bvphoenix.db.models.text_embeddings import TEXT_EMBEDDING_DIM, TextEmbedding
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db

_STORE = "text_embeddings"
_MODEL = "minilm-multi-v1"


async def _finding(db, *, study, series, ftype, status="confirmed") -> Finding:
    f = Finding(
        id=uuid.uuid4(),
        patient_id=study.patient_id,
        study_id=study.id,
        series_id=series.id,
        finding_type_id=ftype.id,
        status=status,
        author_kind="human",
        morphology_keys=[],
        etag=uuid.uuid4(),
    )
    db.add(f)
    await db.flush()
    await db.commit()
    return f


async def test_finding_candidates_offer_compose_and_converge(db_session, make_user, make_study):
    from sqlalchemy import select

    owner = await make_user()
    ftype = (await db_session.execute(select(FindingType).limit(1))).scalar_one()
    study, series = await make_study(owner, modality="CT")
    pid = study.patient_id

    confirmed = await _finding(db_session, study=study, series=series, ftype=ftype)
    candidate = await _finding(
        db_session, study=study, series=series, ftype=ftype, status="candidate"
    )

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, future=True)
    try:
        with SyncSession(engine) as s:
            missing = _finding_candidates(
                s,
                patient_id=pid,
                confirmed_only=False,
                only_missing=True,
                store_table=_STORE,
                model_id=_MODEL,
            )
            confirmed_only = _finding_candidates(
                s,
                patient_id=pid,
                confirmed_only=True,
                only_missing=True,
                store_table=_STORE,
                model_id=_MODEL,
            )
        ids_missing = {fid for fid, _ in missing}
        # Both non-deleted findings are offered, each with non-blank text
        # composed from the finding-type display (single source of truth).
        assert confirmed.id in ids_missing
        assert candidate.id in ids_missing
        assert all(body.strip() for _, body in missing)
        assert dict(missing)[confirmed.id] == ftype.display

        # confirmed_only narrows to status='confirmed'.
        ids_conf = {fid for fid, _ in confirmed_only}
        assert confirmed.id in ids_conf
        assert candidate.id not in ids_conf

        # Embed the confirmed finding → --only-missing must drop it (converge),
        # --all-findings re-offers it.
        db_session.add(
            TextEmbedding(
                id=uuid.uuid4(),
                target_kind="finding",
                target_id=confirmed.id,
                model_id=_MODEL,
                vector=[0.0] * TEXT_EMBEDDING_DIM,
            )
        )
        await db_session.commit()

        with SyncSession(engine) as s:
            missing2 = {
                fid
                for fid, _ in _finding_candidates(
                    s,
                    patient_id=pid,
                    confirmed_only=False,
                    only_missing=True,
                    store_table=_STORE,
                    model_id=_MODEL,
                )
            }
            all_findings = {
                fid
                for fid, _ in _finding_candidates(
                    s,
                    patient_id=pid,
                    confirmed_only=False,
                    only_missing=False,
                    store_table=_STORE,
                    model_id=_MODEL,
                )
            }
    finally:
        engine.dispose()

    assert confirmed.id not in missing2  # already embedded → not re-offered
    assert candidate.id in missing2  # still missing
    assert confirmed.id in all_findings  # --all-findings ignores coverage
