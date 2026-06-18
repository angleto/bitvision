"""Revoking training consent stales the open datasets that bundled the study.

Flow task a5c3f73e (Option 3 Phase D). When a contributor opts a study back
out of the training pool, every OPEN dataset that already bundled that study
now holds revoked data and must be rebuilt before it can be licensed — so it
flips ``open`` -> ``stale``. Frozen datasets (immutable snapshots already
sold under a signed license) are left untouched, and open datasets that
never contained the study are unaffected. Together with the sign gate
(which refuses a stale dataset) this makes revoked data unsellable.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete

from bvphoenix.db.models import DatasetStudy, LicensedDataset, TrainingConsent
from bvphoenix.services.consent_auto import revoke_tier_consent_for_study
from tests.conftest import skip_if_no_db


def _dataset(status: str) -> LicensedDataset:
    return LicensedDataset(
        id=uuid.uuid4(),
        status=status,
        manifest_hash="a" * 64,
        study_count=1,
        contributor_count=1,
        k_anon=5,
        manifest_s3_bucket="b",
        manifest_s3_key="k",
    )


def _member(dataset_id: uuid.UUID, study_id: uuid.UUID) -> DatasetStudy:
    return DatasetStudy(
        dataset_id=dataset_id,
        study_id=study_id,
        anonymized_s3_bucket="b",
        anonymized_s3_key="k",
        content_sha256="c" * 64,
        size_bytes=10,
    )


@skip_if_no_db
async def test_revoke_stales_only_open_datasets_with_study(db_session, make_user, make_study):
    owner = await make_user()
    study, _series = await make_study(owner, modality="CT")
    study.contribution_tier = "t3"
    db_session.add(study)
    await db_session.flush()
    db_session.add(
        TrainingConsent(
            user_subject_id=owner.subject_id,
            study_id=study.id,
            tier="t3",
            consent_hash="h" * 64,
        )
    )
    other, _series2 = await make_study(owner, modality="CT")
    other.contribution_tier = "t3"
    db_session.add(other)
    await db_session.flush()
    await db_session.commit()

    ds_open = _dataset("open")  # contains study -> should stale
    ds_frozen = _dataset("frozen")  # contains study but already sold -> untouched
    ds_other = _dataset("open")  # open but does not contain study -> untouched
    db_session.add_all([ds_open, ds_frozen, ds_other])
    await db_session.flush()
    db_session.add_all(
        [
            _member(ds_open.id, study.id),
            _member(ds_frozen.id, study.id),
            _member(ds_other.id, other.id),
        ]
    )
    await db_session.commit()

    try:
        touched = await revoke_tier_consent_for_study(
            db_session, user_subject_id=owner.subject_id, study_id=study.id
        )
        await db_session.commit()

        await db_session.refresh(ds_open)
        await db_session.refresh(ds_frozen)
        await db_session.refresh(ds_other)
        assert len(touched) == 1
        assert ds_open.status == "stale"  # open + contains the revoked study
        assert ds_frozen.status == "frozen"  # immutable signed snapshot, untouched
        assert ds_other.status == "open"  # open but never bundled this study
    finally:
        # DatasetStudy.study_id is ON DELETE RESTRICT, so the datasets (which
        # cascade to their membership rows) must go before make_study's
        # teardown can delete the studies.
        await db_session.execute(
            delete(LicensedDataset).where(
                LicensedDataset.id.in_([ds_open.id, ds_frozen.id, ds_other.id])
            )
        )
        await db_session.commit()
