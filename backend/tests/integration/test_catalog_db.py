"""DB-backed aggregation for the public dataset catalog.

Inserts public + private studies across two uniquely-named collections
and asserts ``services.dataset_catalog`` groups, counts, and resolves
them correctly. Assertions are scoped to the run's own collections (the
handles carry a random suffix), so the test is correct whether the DB is
empty or shared with other public data.

Requires a live Postgres with migrations applied (the ``db_session``
fixture). Run with ``BVP_DATABASE_URL`` pointing at a migrated test DB.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, ImagingStudy, Patient, Series
from bvphoenix.db.models.principals import Subject
from bvphoenix.services import dataset_catalog as catalog
from tests.conftest import skip_if_no_db

# Needs a live Postgres: skips in the no-DB ``backend-test`` job, runs in
# the DB-backed CI job (and locally against a migrated test DB).
pytestmark = [pytest.mark.asyncio, skip_if_no_db]


async def _add_study(
    db: AsyncSession,
    *,
    owner_subject_id: uuid.UUID,
    patient_id: uuid.UUID,
    collection: str | None,
    is_public: bool,
    modalities: list[str],
    body_part: str,
    instances: int,
    license_spdx: str | None = "CC-BY-4.0",
    license_url: str | None = "https://creativecommons.org/licenses/by/4.0/",
    citation_text: str | None = "Author A, et al. doi:10.7937/TCIA.TEST-0001",
) -> uuid.UUID:
    event = ClinicalEvent(
        id=uuid.uuid4(),
        patient_id=patient_id,
        kind="imaging_study",
        title="catalog test study",
    )
    db.add(event)
    await db.flush()
    study = ImagingStudy(
        id=uuid.uuid4(),
        patient_id=patient_id,
        clinical_event_id=event.id,
        study_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
        owner_subject_id=owner_subject_id,
        study_description="catalog test",
        modalities=modalities,
        is_public=is_public,
        # A public row must be tier t4 (ck_imaging_studies_public_tier_t4).
        contribution_tier="t4" if is_public else "t1",
        source_collection=collection,
        source_subject_id=str(patient_id) if collection else None,
        # A public (t4) row must carry a license regardless of whether it
        # belongs to a catalog collection (ck_imaging_studies_t4_license).
        license_spdx=license_spdx,
        license_url=license_url,
        citation_required=bool(collection),
        citation_text=citation_text if collection else None,
    )
    db.add(study)
    await db.flush()
    db.add(
        Series(
            id=uuid.uuid4(),
            study_id=study.id,
            series_instance_uid=f"1.2.840.{uuid.uuid4().int}"[:64],
            modality=modalities[0],
            body_part_examined=body_part,
            received_instance_count=instances,
        )
    )
    await db.flush()
    return study.id


@pytest_asyncio.fixture
async def catalog_fixture(db_session: AsyncSession):
    """Two collections + decoy rows, scoped by a random run suffix."""
    run = uuid.uuid4().hex[:8]
    liver = f"TESTCAT/Liver-{run}"
    breast_nc = f"TESTCAT/Breast-NC-{run}"

    owner = uuid.uuid4()
    db_session.add(Subject(id=owner, kind="user", display_name=f"owner-{run}"))
    await db_session.flush()

    # Three patients (managed by the owner subject). Patient A carries two
    # studies in the liver collection, to exercise distinct-subject counting.
    pat_a, pat_b, pat_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for pid in (pat_a, pat_b, pat_c):
        db_session.add(Patient(id=pid, managed_by_subject_id=owner, display_name=f"patient-{run}"))
    await db_session.flush()

    created: list[uuid.UUID] = []
    # Liver collection: 3 studies across 2 subjects, 100+200+300 instances.
    created.append(
        await _add_study(
            db_session,
            owner_subject_id=owner,
            patient_id=pat_a,
            collection=liver,
            is_public=True,
            modalities=["CT", "SEG"],
            body_part="LIVER",
            instances=100,
        )
    )
    created.append(
        await _add_study(
            db_session,
            owner_subject_id=owner,
            patient_id=pat_a,
            collection=liver,
            is_public=True,
            modalities=["CT"],
            body_part="LIVER",
            instances=200,
        )
    )
    created.append(
        await _add_study(
            db_session,
            owner_subject_id=owner,
            patient_id=pat_b,
            collection=liver,
            is_public=True,
            modalities=["MR"],
            body_part="ABDOMEN",
            instances=300,
        )
    )
    # Breast NC collection: 1 study, 1 subject, NonCommercial license.
    created.append(
        await _add_study(
            db_session,
            owner_subject_id=owner,
            patient_id=pat_c,
            collection=breast_nc,
            is_public=True,
            modalities=["MR"],
            body_part="BREAST",
            instances=50,
            license_spdx="CC-BY-NC-4.0",
            license_url="https://creativecommons.org/licenses/by-nc/4.0/",
        )
    )
    # Decoy: a private study that carries the same collection handle but
    # is NOT public — it must never inflate the catalog counts. (A public
    # study without a source_collection is impossible at the DB level:
    # ck_imaging_studies_public_tier_t4 forces is_public ⇒ t4, and
    # ck_imaging_studies_t4_license forces t4 ⇒ source_collection IS NOT
    # NULL. So is_public ⇒ has a collection; the catalog filter is total.)
    created.append(
        await _add_study(
            db_session,
            owner_subject_id=owner,
            patient_id=pat_c,
            collection=liver,
            is_public=False,
            modalities=["CT"],
            body_part="LIVER",
            instances=999,
        )
    )
    await db_session.commit()

    yield {"liver": liver, "breast_nc": breast_nc}

    for sid in created:
        await db_session.execute(ImagingStudy.__table__.delete().where(ImagingStudy.id == sid))
    await db_session.execute(
        ClinicalEvent.__table__.delete().where(ClinicalEvent.patient_id.in_([pat_a, pat_b, pat_c]))
    )
    for pid in (pat_a, pat_b, pat_c):
        await db_session.execute(Patient.__table__.delete().where(Patient.id == pid))
    await db_session.execute(Subject.__table__.delete().where(Subject.id == owner))
    await db_session.commit()


async def test_aggregate_groups_and_counts(db_session: AsyncSession, catalog_fixture) -> None:
    liver = catalog_fixture["liver"]
    aggs = {a.collection: a for a in await catalog.aggregate_collections(db_session)}

    assert liver in aggs
    liv = aggs[liver]
    assert liv.studies == 3
    assert liv.subjects == 2  # pat_a (x2) + pat_b — distinct
    assert liv.series == 3
    assert liv.instances == 600  # 100 + 200 + 300; the private decoy (999) excluded
    assert set(liv.modalities) == {"CT", "SEG", "MR"}
    assert set(liv.body_parts) == {"LIVER", "ABDOMEN"}
    assert liv.license_spdx == "CC-BY-4.0"
    assert liv.commercial_use_allowed is True
    assert liv.slug == catalog.slugify(liver)


async def test_noncommercial_license_flagged(db_session: AsyncSession, catalog_fixture) -> None:
    breast = catalog_fixture["breast_nc"]
    aggs = {a.collection: a for a in await catalog.aggregate_collections(db_session)}
    assert breast in aggs
    assert aggs[breast].commercial_use_allowed is False
    assert aggs[breast].instances == 50


async def test_private_and_uncollected_excluded(db_session: AsyncSession, catalog_fixture) -> None:
    liver = catalog_fixture["liver"]
    aggs = {a.collection: a for a in await catalog.aggregate_collections(db_session)}
    # The private decoy carried source_collection == liver but is_public
    # False, so it must not inflate the liver counts (already 600/3 above),
    # and there must be no NULL-collection bucket.
    assert None not in aggs
    assert aggs[liver].instances == 600


async def test_get_collection_resolves_by_slug(db_session: AsyncSession, catalog_fixture) -> None:
    liver = catalog_fixture["liver"]
    slug = catalog.slugify(liver)
    agg = await catalog.get_collection(db_session, slug)
    assert agg is not None
    assert agg.collection == liver
    assert agg.studies == 3
    # Unknown slug → None.
    assert await catalog.get_collection(db_session, "no-such-collection-xyz") is None


async def test_sample_studies_returns_collection_studies(
    db_session: AsyncSession, catalog_fixture
) -> None:
    liver = catalog_fixture["liver"]
    samples = await catalog.sample_studies(db_session, liver)
    assert len(samples) == 3
    assert all(s.id for s in samples)
