"""``list_user_datasets`` — the contributor's "my data in datasets" view.

Flow task a5c3f73e (Option 3 Phase E). A contributor sees every dataset that
bundled at least one of their studies: dataset id + status + tiers + counts,
scoped to their own contribution and never leaking another contributor's
datasets, any study/patient id, or a storage location.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete

from bvphoenix.db.models import DatasetStudy, LicensedDataset
from bvphoenix.services.contributor_payouts import UserDatasetView, list_user_datasets
from tests.conftest import skip_if_no_db


def _dataset(status: str, *, study_count: int, contributor_count: int) -> LicensedDataset:
    return LicensedDataset(
        id=uuid.uuid4(),
        status=status,
        manifest_hash="a" * 64,
        study_count=study_count,
        contributor_count=contributor_count,
        k_anon=5,
        manifest_s3_bucket="b",
        manifest_s3_key="k",
    )


def _member(dataset_id: uuid.UUID, study_id: uuid.UUID, contributor_id: uuid.UUID) -> DatasetStudy:
    return DatasetStudy(
        dataset_id=dataset_id,
        study_id=study_id,
        contributor_subject_id=contributor_id,
        anonymized_s3_bucket="b",
        anonymized_s3_key="k",
        content_sha256="c" * 64,
        size_bytes=10,
    )


async def _study(db, make_study, owner):
    study, _series = await make_study(owner, modality="CT")
    study.contribution_tier = "t3"
    db.add(study)
    await db.flush()
    return study


@skip_if_no_db
async def test_list_user_datasets_scoped_and_storage_isolated(db_session, make_user, make_study):
    me = await make_user()
    other = await make_user()
    s1 = await _study(db_session, make_study, me)
    s2 = await _study(db_session, make_study, me)
    s_other = await _study(db_session, make_study, other)
    await db_session.commit()

    ds_a = _dataset("open", study_count=2, contributor_count=1)  # both my studies
    ds_b = _dataset("frozen", study_count=1, contributor_count=1)  # one of mine, sold
    ds_c = _dataset("open", study_count=1, contributor_count=1)  # someone else's
    db_session.add_all([ds_a, ds_b, ds_c])
    await db_session.flush()
    db_session.add_all(
        [
            _member(ds_a.id, s1.id, me.subject_id),
            _member(ds_a.id, s2.id, me.subject_id),
            _member(ds_b.id, s1.id, me.subject_id),
            _member(ds_c.id, s_other.id, other.subject_id),
        ]
    )
    await db_session.commit()

    try:
        mine = await list_user_datasets(db_session, user_subject_id=me.subject_id)
        by_id = {v.dataset_id: v for v in mine}
        assert set(by_id) == {ds_a.id, ds_b.id}  # ds_c (not mine) excluded

        a = by_id[ds_a.id]
        assert a.my_study_count == 2
        assert a.study_count == 2
        assert a.contributor_count == 1
        assert a.tiers == ["t3"]
        assert a.status == "open"
        assert by_id[ds_b.id].my_study_count == 1
        assert by_id[ds_b.id].status == "frozen"

        # Storage isolation: the view type carries no s3 / study-id surface.
        fields = set(UserDatasetView.__dataclass_fields__)
        assert not (fields & {"anonymized_s3_key", "manifest_s3_key", "study_id", "bucket", "key"})

        # The other contributor sees only their own dataset.
        theirs = await list_user_datasets(db_session, user_subject_id=other.subject_id)
        assert {v.dataset_id for v in theirs} == {ds_c.id}
    finally:
        # DatasetStudy.study_id is ON DELETE RESTRICT — drop the datasets
        # (cascading to membership) before make_study teardown deletes studies.
        await db_session.execute(
            delete(LicensedDataset).where(LicensedDataset.id.in_([ds_a.id, ds_b.id, ds_c.id]))
        )
        await db_session.commit()
