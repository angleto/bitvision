"""``resolve_cohort_contributors`` maps each study to its consenting user.

Flow task a5c3f73e (Option 3 producer). The dataset producer attributes
each ``DatasetStudy`` (and thus the payout weight) to the user holding the
active training consent at the study's contribution tier. Consents that are
revoked, or whose tier does not match the study's, must not resolve.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from bvphoenix.db.models import TrainingConsent
from bvphoenix.services import training_cohort
from tests.conftest import skip_if_no_db


async def _study_at_tier(db, make_study, owner, *, tier: str):
    study, _series = await make_study(owner, modality="CT")
    study.contribution_tier = tier
    db.add(study)
    await db.flush()
    return study


@skip_if_no_db
async def test_resolve_cohort_contributors_only_active_tier_matched(
    db_session, make_user, make_study
):
    owner = await make_user()
    contributor = await make_user()

    # t3 throughout: t4 studies carry a CHECK requiring license fields, which
    # is orthogonal to what this resolver tests.
    # 1) active consent at the study's tier -> resolves to the contributor.
    s_active = await _study_at_tier(db_session, make_study, owner, tier="t3")
    db_session.add(
        TrainingConsent(
            user_subject_id=contributor.subject_id,
            study_id=s_active.id,
            tier="t3",
            consent_hash="a" * 64,
        )
    )
    # 2) revoked consent -> excluded.
    s_revoked = await _study_at_tier(db_session, make_study, owner, tier="t3")
    db_session.add(
        TrainingConsent(
            user_subject_id=contributor.subject_id,
            study_id=s_revoked.id,
            tier="t3",
            consent_hash="b" * 64,
            revoked_at=datetime.now(UTC),
        )
    )
    # 3) tier-mismatch (consent t4, study t3) -> excluded.
    s_mismatch = await _study_at_tier(db_session, make_study, owner, tier="t3")
    db_session.add(
        TrainingConsent(
            user_subject_id=contributor.subject_id,
            study_id=s_mismatch.id,
            tier="t4",
            consent_hash="c" * 64,
        )
    )
    await db_session.commit()

    out = await training_cohort.resolve_cohort_contributors(
        db_session, [s_active.id, s_revoked.id, s_mismatch.id]
    )
    assert out == {s_active.id: contributor.subject_id}


@skip_if_no_db
async def test_resolve_cohort_contributors_empty_input(db_session):
    assert await training_cohort.resolve_cohort_contributors(db_session, []) == {}
    # An unknown study id simply resolves to nothing (no contributor).
    assert await training_cohort.resolve_cohort_contributors(db_session, [uuid.uuid4()]) == {}
