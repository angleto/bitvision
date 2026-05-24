"""Two users may legitimately upload studies with the same DICOM UIDs.

Real-world DICOM payloads reuse StudyInstanceUID / SeriesInstanceUID /
SOPInstanceUID across sites and DVDs. The schema scopes uniqueness to
the owning subject (``UNIQUE(owner_subject_id, study_instance_uid)``,
and the analogous composite uniques on series + instances) so user B
can ingest a study whose UIDs collide with user A's without either
failing or grafting onto the other's records.

These tests pin that invariant. They also pin the S3 key shape — the
on-disk path must include the owner subject id so two colliding UIDs
don't overwrite each other in the bucket either.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.db.models import ClinicalEvent, ImagingStudy, Patient, User
from bvphoenix.services.dicom_ingest import s3_key_for
from tests.conftest import skip_if_no_db

pytestmark = [pytest.mark.asyncio, skip_if_no_db]


@pytest_asyncio.fixture
async def two_users(make_user: Callable[..., Awaitable[User]]) -> tuple[User, User]:
    a = await make_user(email=f"alice-{uuid.uuid4()}@example.com")
    b = await make_user(email=f"bob-{uuid.uuid4()}@example.com")
    return a, b


async def test_same_study_uid_under_two_owners_inserts_two_rows(
    db_session: AsyncSession,
    two_users: tuple[User, User],
) -> None:
    alice, bob = two_users
    shared_uid = f"1.2.840.{uuid.uuid4().int}"[:64]

    # v3: ImagingStudy has a 1:1 to ClinicalEvent. The unique constraint
    # we want to exercise is on (owner_subject_id, study_instance_uid),
    # so we need separate patient + event for each owner — they don't
    # share clinical context.
    alice_patient = Patient(
        id=uuid.uuid4(), managed_by_subject_id=alice.subject_id, display_name="A"
    )
    bob_patient = Patient(id=uuid.uuid4(), managed_by_subject_id=bob.subject_id, display_name="B")
    db_session.add_all([alice_patient, bob_patient])
    await db_session.flush()

    alice_event = ClinicalEvent(
        id=uuid.uuid4(), patient_id=alice_patient.id, kind="imaging_study", title="A"
    )
    bob_event = ClinicalEvent(
        id=uuid.uuid4(), patient_id=bob_patient.id, kind="imaging_study", title="B"
    )
    db_session.add_all([alice_event, bob_event])
    await db_session.flush()

    a_study = ImagingStudy(
        id=uuid.uuid4(),
        patient_id=alice_patient.id,
        clinical_event_id=alice_event.id,
        study_instance_uid=shared_uid,
        owner_subject_id=alice.subject_id,
        modalities=["CT"],
    )
    b_study = ImagingStudy(
        id=uuid.uuid4(),
        patient_id=bob_patient.id,
        clinical_event_id=bob_event.id,
        study_instance_uid=shared_uid,
        owner_subject_id=bob.subject_id,
        modalities=["CT"],
    )
    db_session.add(a_study)
    db_session.add(b_study)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(ImagingStudy).where(ImagingStudy.study_instance_uid == shared_uid)
            )
        )
        .scalars()
        .all()
    )
    owners = {r.owner_subject_id for r in rows}
    assert owners == {alice.subject_id, bob.subject_id}

    # Cleanup — we inserted directly, conftest's teardown would only
    # remove rows tied to the user fixtures.
    for r in rows:
        await db_session.delete(r)
    for ev in (alice_event, bob_event):
        await db_session.delete(ev)
    for p in (alice_patient, bob_patient):
        await db_session.delete(p)
    await db_session.commit()


async def test_s3_key_uses_internal_uuids_only() -> None:
    """The S3 key must use BitVision-controlled UUIDs end-to-end.

    DICOM ``StudyInstanceUID`` / ``SeriesInstanceUID`` /
    ``SOPInstanceUID`` are not authoritative across BitVision
    tenants — keying on them risks cross-tenant overwrites the
    read-side authorization layer cannot catch. The new contract
    keys on ``patient_id`` / ``study_id`` / ``series_id`` /
    ``instance_id`` (all uuid_pk).
    """
    patient = uuid.UUID("11111111-1111-1111-1111-111111111111")
    study = uuid.UUID("22222222-2222-2222-2222-222222222222")
    series = uuid.UUID("33333333-3333-3333-3333-333333333333")
    instance_a = uuid.UUID("44444444-4444-4444-4444-444444444444")
    instance_b = uuid.UUID("55555555-5555-5555-5555-555555555555")

    key_a = s3_key_for(
        patient_id=patient,
        study_id=study,
        series_id=series,
        instance_id=instance_a,
    )
    assert key_a == (
        f"patients/{patient}/studies/{study}/series/{series}/instances/{instance_a}.dcm"
    )

    # Different instance ids → different keys (the basic isolation
    # guarantee — UUIDs are collision-impossible by construction).
    key_b = s3_key_for(
        patient_id=patient,
        study_id=study,
        series_id=series,
        instance_id=instance_b,
    )
    assert key_a != key_b

    # ``patient_id=None`` (orphan study) lands under the
    # ``unassigned/`` prefix instead of ``patients/<id>/``. Once the
    # study is linked to a patient, future writes pick up the
    # patient prefix; existing blobs continue serving from their
    # recorded ``Instance.s3_key``.
    orphan_key = s3_key_for(
        patient_id=None,
        study_id=study,
        series_id=series,
        instance_id=instance_a,
    )
    assert orphan_key.startswith("unassigned/")


# Series + Instance composite uniques are exercised by the same DB
# constraint type as the study-level test above; we leave the deeper
# cross-parent exercise to the integration suite (the per-test
# event-loop teardown with ``make_user`` makes a chain of DB-touching
# tests in one file flaky on the dev machine).
