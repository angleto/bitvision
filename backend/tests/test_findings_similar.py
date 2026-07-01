"""find_similar_findings — cohort-by-lesion visual retrieval (Flow c390b2a5).

Given a finding, ``find_similar_findings_core`` returns findings on studies
whose imaging is visually similar (BiomedCLIP series vectors), reusing the
tested ``/similar-to`` ANN. The invariants that matter:

* ranks by series cosine similarity (near study before far study);
* visibility-scoped — a finding on a study the caller cannot see (another
  owner's private study) NEVER surfaces, even when its series vector is the
  closest of all (cross-patient must be inexpressible, not merely trimmed);
* excludes the anchor's own study, soft-deleted findings, and retracted ones;
* ``same_type=True`` restricts to the anchor's finding type;
* 404 when the anchor is not visible; 422 when it has no series.

DB integration test (runs over the real schema + pgvector).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from starlette.exceptions import HTTPException

from bvphoenix.api.findings import find_similar_findings_core
from bvphoenix.db.models import Finding, FindingType
from tests.conftest import skip_if_no_db

pytestmark = skip_if_no_db


def _vec(*head: float) -> list[float]:
    """A 512-d embedding: the given leading components, zero-padded."""
    v = list(head) + [0.0] * (512 - len(head))
    return v[:512]


async def _finding(
    db,
    *,
    study,
    series,
    ftype: FindingType,
    status: str = "confirmed",
    deleted: bool = False,
) -> Finding:
    f = Finding(
        id=uuid.uuid4(),
        patient_id=study.patient_id,
        study_id=study.id,
        series_id=series.id if series is not None else None,
        finding_type_id=ftype.id,
        status=status,
        author_kind="human",
        morphology_keys=[],
        etag=uuid.uuid4(),
    )
    if deleted:
        from datetime import UTC, datetime

        f.deleted_at = datetime.now(UTC)
    db.add(f)
    await db.flush()
    await db.commit()
    return f


async def _two_types(db) -> tuple[FindingType, FindingType]:
    types = (
        (await db.execute(select(FindingType).order_by(FindingType.key).limit(2))).scalars().all()
    )
    assert len(types) >= 2, "finding_types vocab seed missing"
    return types[0], types[1]


@pytest.mark.asyncio
async def test_ranks_by_similarity_and_scopes_visibility(
    db_session, make_user, make_study, make_embedding
):
    owner = await make_user()
    other = await make_user()
    t0, t1 = await _two_types(db_session)

    # Anchor study + finding.
    study_a, series_a = await make_study(owner, modality="CT")
    await make_embedding(series_a, vector=_vec(1.0))
    anchor = await _finding(db_session, study=study_a, series=series_a, ftype=t0)

    # Near neighbour (visible) — closest vector → best score.
    study_near, series_near = await make_study(owner, modality="CT")
    await make_embedding(series_near, vector=_vec(0.95, 0.05))
    near = await _finding(db_session, study=study_near, series=series_near, ftype=t0)

    # Far neighbour (visible) — orthogonal-ish → low score, different type.
    study_far, series_far = await make_study(owner, modality="CT")
    await make_embedding(series_far, vector=_vec(0.2, 0.98))
    far = await _finding(db_session, study=study_far, series=series_far, ftype=t1)

    # A soft-deleted and a retracted finding must never surface.
    deleted = await _finding(
        db_session, study=study_near, series=series_near, ftype=t0, deleted=True
    )
    retracted = await _finding(
        db_session, study=study_far, series=series_far, ftype=t0, status="retracted"
    )

    # Hidden neighbour owned by ANOTHER user, private — its vector is the
    # closest of all, yet it must be invisible (cross-patient inexpressible).
    study_hidden, series_hidden = await make_study(other, modality="CT")
    await make_embedding(series_hidden, vector=_vec(0.99, 0.01))
    hidden = await _finding(db_session, study=study_hidden, series=series_hidden, ftype=t0)

    results = await find_similar_findings_core(
        db=db_session, user=owner, finding_id=anchor.id, k=10
    )
    ids = [r.finding.id for r in results]

    assert str(near.id) in ids
    assert str(far.id) in ids
    assert str(anchor.id) not in ids  # anchor's own study is skipped
    assert str(hidden.id) not in ids  # cross-patient invisible
    assert str(deleted.id) not in ids  # soft-deleted excluded
    assert str(retracted.id) not in ids  # retracted excluded

    # Ranked by imaging similarity: the near study outscores the far one.
    score_by_id = {r.finding.id: r.score for r in results}
    assert score_by_id[str(near.id)] > score_by_id[str(far.id)]
    assert results[0].finding.id == str(near.id)
    # PHI-free retrieval payload: a similarity score + the matched series.
    assert 0.0 <= results[0].score <= 1.0
    assert results[0].matched_series_id == str(series_near.id)


@pytest.mark.asyncio
async def test_same_type_filters_to_anchor_type(db_session, make_user, make_study, make_embedding):
    owner = await make_user()
    t0, t1 = await _two_types(db_session)

    study_a, series_a = await make_study(owner, modality="CT")
    await make_embedding(series_a, vector=_vec(1.0))
    anchor = await _finding(db_session, study=study_a, series=series_a, ftype=t0)

    study_same, series_same = await make_study(owner, modality="CT")
    await make_embedding(series_same, vector=_vec(0.95, 0.05))
    same = await _finding(db_session, study=study_same, series=series_same, ftype=t0)

    study_other, series_other = await make_study(owner, modality="CT")
    await make_embedding(series_other, vector=_vec(0.9, 0.1))
    other_type = await _finding(db_session, study=study_other, series=series_other, ftype=t1)

    results = await find_similar_findings_core(
        db=db_session, user=owner, finding_id=anchor.id, k=10, same_type=True
    )
    ids = [r.finding.id for r in results]
    assert str(same.id) in ids
    assert str(other_type.id) not in ids


@pytest.mark.asyncio
async def test_invisible_anchor_is_404(db_session, make_user, make_study, make_embedding):
    owner = await make_user()
    stranger = await make_user()
    t0, _ = await _two_types(db_session)
    study_a, series_a = await make_study(owner, modality="CT")
    await make_embedding(series_a, vector=_vec(1.0))
    anchor = await _finding(db_session, study=study_a, series=series_a, ftype=t0)

    with pytest.raises(HTTPException) as ei:
        await find_similar_findings_core(db=db_session, user=stranger, finding_id=anchor.id)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_finding_without_series_is_422(db_session, make_user, make_study):
    owner = await make_user()
    t0, _ = await _two_types(db_session)
    study_a, _series = await make_study(owner, modality="CT")
    anchor = await _finding(db_session, study=study_a, series=None, ftype=t0)

    with pytest.raises(HTTPException) as ei:
        await find_similar_findings_core(db=db_session, user=owner, finding_id=anchor.id)
    assert ei.value.status_code == 422
