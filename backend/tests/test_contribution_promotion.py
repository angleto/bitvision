"""Accept→publish loop: staged redaction at check time, provenance stamping and
publication at promote, purge on reject.

The safety-critical properties:

* the check pass STAGES the redacted rendition (what the reviewer previews is
  what ships) and records it on the manifest;
* t3 promote stamps ``BurnedInAnnotation=NO`` + CID 7050 ``113101`` onto the
  staged bytes and writes the verified-clean pointer on the ORIGINAL instance;
* t4 promote clones into a platform-owner public patient (never flips the
  owner's study in place) and marks the clone approved/clean-at-rest;
* a sha256 mismatch aborts the promote (never publish unreviewed bytes);
* pixel-gated components that could not be staged never ship;
* reject purges ONLY ``_contrib/``-prefixed staged keys.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import pydicom
import pytest

from bvphoenix.config import get_settings
from bvphoenix.db.models import ImagingStudy, Instance, Patient, Submission
from bvphoenix.services.pixel_deid import PixelDeidResult, PixelRisk
from bvphoenix.services.pixel_deid_eval import synthesize_case
from bvphoenix.services.public_contribution import checks as checks_mod
from bvphoenix.services.public_contribution import promotion, redaction
from bvphoenix.services.public_contribution.checks import PixelPhiCheck
from bvphoenix.services.review_queue import CheckContext, StagedComponent, StagedItem
from bvphoenix.services.review_queue.actor import SYSTEM_ACTOR

from .conftest import skip_if_no_db


class _StubStorage:
    """Dict-backed S3 stand-in recording uploads and deletes."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.uploaded: list[str] = []
        self.deleted: list[str] = []

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise FileNotFoundError(key) from None

    def upload_bytes(self, data: bytes, *, bucket: str, key: str):
        self.objects[key] = bytes(data) if not isinstance(data, bytes) else data
        self.uploaded.append(key)

    def delete_object(self, *, bucket: str, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)


def _scrubbed_us(seed: int = 1) -> bytes:
    """A header-scrubbed synthetic burned-in-PHI US instance (valid DICOM)."""
    from bvphoenix.services.deidentify import deidentify_dicom_bytes

    return deidentify_dicom_bytes(synthesize_case(seed=seed, size=(80, 120)).dicom)


def _fake_clean(out: bytes) -> object:
    def _clean(src: bytes, **_kw: object) -> PixelDeidResult:
        return PixelDeidResult(
            out_bytes=out,
            risk=PixelRisk("high", ("high_risk_modality:US",)),
            residual_suspect=True,
            redactions=[{"x": 1, "y": 2, "w": 30, "h": 10, "text": "ROSSI", "conf": 88.0}],
            detected_text=True,
        )

    return _clean


# --- staging during the check pass -------------------------------------------


@skip_if_no_db
@pytest.mark.asyncio
async def test_pixel_phi_check_stages_redaction(db_session, monkeypatch) -> None:
    raw = synthesize_case(seed=3, size=(80, 120)).dicom
    clean = _scrubbed_us(seed=3)
    storage = _StubStorage({"raw/us-0.dcm": raw})
    monkeypatch.setattr("bvphoenix.storage.get_s3_storage", lambda: storage)
    monkeypatch.setattr(redaction, "clean_pixel_data", _fake_clean(clean))

    iid = str(uuid.uuid4())
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="processing",
        manifest={
            "instances": [
                {
                    "instance_id": iid,
                    "name": "us-0.dcm",
                    "pixel_phi_risk": "high",
                    "s3_bucket": "b",
                    "s3_key": "raw/us-0.dcm",
                }
            ]
        },
    )
    db_session.add(sub)
    await db_session.commit()

    async def _read() -> bytes:
        return raw

    ctx = CheckContext(
        db=db_session,
        staged=StagedItem(
            item_id=sub.id,
            components=[
                StagedComponent(name="us-0.dcm", size_bytes=len(raw), content_type=None, read=_read)
            ],
            manifest=dict(sub.manifest),
        ),
    )
    res = await PixelPhiCheck().run(ctx)
    await db_session.commit()
    await db_session.refresh(sub)

    assert res.verdict == "fail"  # high risk always needs the human
    entry = sub.manifest["instances"][0]
    expected_key = redaction.staged_redacted_key(sub.id, iid)
    assert entry["staged_redacted_key"] == expected_key
    assert entry["staged_sha256"] == hashlib.sha256(clean).hexdigest()
    assert entry["risk_level"] == "high"
    assert entry["staged_redactions"] == [
        {"x": 1, "y": 2, "w": 30, "h": 10, "text": "ROSSI", "conf": 88.0}
    ]
    assert sub.staged_prefix == redaction.contrib_staged_prefix(sub.id)
    assert storage.objects[expected_key] == clean


@skip_if_no_db
@pytest.mark.asyncio
async def test_check_never_stages_header_deid_failure(db_session, monkeypatch) -> None:
    # An SR-style component whose header scrub raises must gain NO publishable
    # rendition — staged_reason recorded, no staged key, no upload.
    raw = synthesize_case(seed=4, size=(80, 120)).dicom
    storage = _StubStorage({"raw/sr-0.dcm": raw})
    monkeypatch.setattr("bvphoenix.storage.get_s3_storage", lambda: storage)

    def _boom(_src: bytes) -> bytes:
        from bvphoenix.services.deid.errors import RequiresReview

        raise RequiresReview("SR content")

    monkeypatch.setattr(redaction, "deidentify_dicom_bytes", _boom)

    iid = str(uuid.uuid4())
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="processing",
        manifest={
            "instances": [
                {"instance_id": iid, "name": "sr-0.dcm", "s3_bucket": "b", "s3_key": "raw/sr-0.dcm"}
            ]
        },
    )
    db_session.add(sub)
    await db_session.commit()

    async def _read() -> bytes:
        return raw

    ctx = CheckContext(
        db=db_session,
        staged=StagedItem(
            item_id=sub.id,
            components=[
                StagedComponent(name="sr-0.dcm", size_bytes=len(raw), content_type=None, read=_read)
            ],
            manifest=dict(sub.manifest),
        ),
    )
    res = await PixelPhiCheck().run(ctx)
    await db_session.commit()
    await db_session.refresh(sub)

    assert res.verdict == "fail"
    entry = sub.manifest["instances"][0]
    assert entry.get("staged_redacted_key") is None
    assert entry["staged_reason"].startswith("header_requires_review")
    assert storage.uploaded == []


# --- promote: t3 stamps the original instance --------------------------------


async def _make_t3_submission(db_session, make_user, make_study, storage) -> tuple:
    owner = await make_user(is_admin=True)
    study, series = await make_study(owner, modality="US", body_part="ABDOMEN")
    inst = Instance(
        id=uuid.uuid4(),
        series_id=series.id,
        sop_instance_uid=f"1.2.3.{uuid.uuid4().int}"[:64],
        s3_bucket="b",
        s3_key=f"patients/x/{uuid.uuid4()}.dcm",
        pixel_phi_risk="high",
        pixel_deid_status="unprocessed",
    )
    db_session.add(inst)
    await db_session.flush()

    staged = _scrubbed_us(seed=7)
    staged_key = redaction.staged_redacted_key("SUB", str(inst.id))
    storage.objects[staged_key] = staged
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t3",
        status="promoting",
        source_study_id=study.id,
        reviewed_by_subject_id=owner.subject_id,
        manifest={
            "instances": [
                {
                    "instance_id": str(inst.id),
                    "name": "us-0.dcm",
                    "s3_bucket": inst.s3_bucket,
                    "s3_key": inst.s3_key,
                    "risk_level": "high",
                    "staged_redacted_key": staged_key,
                    "staged_sha256": hashlib.sha256(staged).hexdigest(),
                    "staged_residual": True,
                    "staged_redactions": [{"x": 1, "y": 1, "w": 5, "h": 5}],
                }
            ]
        },
    )
    db_session.add(sub)
    await db_session.commit()
    return owner, study, inst, sub


@skip_if_no_db
@pytest.mark.asyncio
async def test_promote_t3_stamps_pointer_and_provenance(
    db_session, make_user, make_study, monkeypatch
) -> None:
    storage = _StubStorage()
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    _owner, study, inst, sub = await _make_t3_submission(db_session, make_user, make_study, storage)

    refs = await promotion.promote_submission(db_session, item=sub, actor=SYSTEM_ACTOR)
    await db_session.commit()
    await db_session.refresh(inst)
    await db_session.refresh(study)

    assert refs["clean_count"] == 1
    assert study.contribution_tier == "t3"
    assert inst.pixel_deid_status == "approved"
    clean_key = redaction.pixel_clean_key(str(inst.id))
    assert inst.pixel_clean_s3_key == clean_key
    assert inst.pixel_deid_method["method_codes"] == ["113101"]
    assert inst.pixel_deid_method["submission_id"] == str(sub.id)
    # The published blob carries the human-accept provenance stamp.
    ds = pydicom.dcmread(io.BytesIO(storage.objects[clean_key]))
    assert ds.BurnedInAnnotation == "NO"
    assert any(e.CodeValue == "113101" for e in ds.DeidentificationMethodCodeSequence)


@skip_if_no_db
@pytest.mark.asyncio
async def test_promote_aborts_on_sha_mismatch(
    db_session, make_user, make_study, monkeypatch
) -> None:
    storage = _StubStorage()
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    _owner, _study, inst, sub = await _make_t3_submission(
        db_session, make_user, make_study, storage
    )
    sub.manifest["instances"][0]["staged_sha256"] = "0" * 64
    with pytest.raises(promotion.PromotionIntegrityError):
        await promotion.promote_submission(db_session, item=sub, actor=SYSTEM_ACTOR)
    await db_session.rollback()
    await db_session.refresh(inst)
    assert inst.pixel_deid_status == "unprocessed"  # nothing stamped


# --- promote: t4 clones into the public OpenData namespace -------------------


@skip_if_no_db
@pytest.mark.asyncio
async def test_promote_t4_clones_into_public_patient(
    db_session, make_user, make_study, monkeypatch
) -> None:
    storage = _StubStorage()
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    owner = await make_user(is_admin=True)
    study, series = await make_study(owner, modality="US", body_part="ABDOMEN")
    inst = Instance(
        id=uuid.uuid4(),
        series_id=series.id,
        sop_instance_uid=f"1.2.3.{uuid.uuid4().int}"[:64],
        s3_bucket="b",
        s3_key=f"patients/x/{uuid.uuid4()}.dcm",
        pixel_phi_risk="high",
        pixel_deid_status="unprocessed",
    )
    db_session.add(inst)
    await db_session.flush()

    staged = _scrubbed_us(seed=9)
    staged_key = redaction.staged_redacted_key("SUB", str(inst.id))
    storage.objects[staged_key] = staged
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="promoting",
        source_study_id=study.id,
        reviewed_by_subject_id=owner.subject_id,
        manifest={
            "instances": [
                {
                    "instance_id": str(inst.id),
                    "name": "us-0.dcm",
                    "s3_bucket": inst.s3_bucket,
                    "s3_key": inst.s3_key,
                    "risk_level": "high",
                    "staged_redacted_key": staged_key,
                    "staged_sha256": hashlib.sha256(staged).hexdigest(),
                    "staged_residual": True,
                    "staged_redactions": [],
                },
                {
                    # Pixel-gated component that could not be staged: never ships.
                    "instance_id": str(uuid.uuid4()),
                    "name": "sr-0.dcm",
                    "s3_bucket": "b",
                    "s3_key": "raw/sr-0.dcm",
                    "risk_level": "high",
                    "staged_reason": "header_requires_review:SR",
                },
            ]
        },
    )
    db_session.add(sub)
    await db_session.commit()

    refs = await promotion.promote_submission(db_session, item=sub, actor=SYSTEM_ACTOR)
    await db_session.commit()
    await db_session.refresh(study)
    await db_session.refresh(sub)

    # The owner's original study is untouched (clone, not flip-in-place).
    assert study.is_public is False
    assert study.contribution_tier != "t4"

    assert refs["published"] == 1
    assert [s["reason"] for s in refs["skipped"]] == ["header_requires_review:SR"]
    assert sub.public_patient_id is not None
    public_patient = await db_session.get(Patient, sub.public_patient_id)
    from bvphoenix.services.permissions import platform_owner_subject_id

    assert public_patient.managed_by_subject_id == platform_owner_subject_id()

    (public_study_id,) = [uuid.UUID(s) for s in refs["public_study_ids"]]
    public_study = await db_session.get(ImagingStudy, public_study_id)
    assert public_study.is_public is True
    assert public_study.contribution_tier == "t4"
    assert public_study.patient_id == sub.public_patient_id
    assert public_study.deid_method_version == get_settings().deid_method_version
    assert public_study.id != study.id
    # t4 CHECK essentials: platform default license + our pipeline as the
    # de-identifying upstream collection.
    assert public_study.license_spdx == "CC-BY-4.0"
    assert public_study.source_collection == promotion.CONTRIB_SOURCE_COLLECTION

    # The clone's instances are human-approved and clean at rest (no pointer).
    from sqlalchemy import select

    from bvphoenix.db.models import Series as SeriesModel

    clone_series = (
        (
            await db_session.execute(
                select(SeriesModel).where(SeriesModel.study_id == public_study.id)
            )
        )
        .scalars()
        .all()
    )
    clone_instances = (
        (
            await db_session.execute(
                select(Instance).where(Instance.series_id.in_([s.id for s in clone_series]))
            )
        )
        .scalars()
        .all()
    )
    assert len(clone_instances) == 1
    ci = clone_instances[0]
    assert ci.pixel_deid_status == "approved"
    assert ci.pixel_clean_s3_key is None
    assert ci.pixel_deid_method["clean_at_rest"] is True
    # The clone's stored bytes ARE the stamped verified-clean rendition.
    ds = pydicom.dcmread(io.BytesIO(storage.objects[ci.s3_key]))
    assert ds.BurnedInAnnotation == "NO"


# --- reject: purge guards the prefix -----------------------------------------


@pytest.mark.asyncio
async def test_purge_deletes_only_contrib_prefixed_keys(monkeypatch) -> None:
    storage = _StubStorage(
        {
            "_contrib/s1/i1.dcm": b"staged",
            "patients/p/studies/s/series/x/instances/i.dcm": b"canonical",
        }
    )
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    item = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="rejected",
        manifest={
            "instances": [
                {"instance_id": "i1", "s3_key": "raw", "staged_redacted_key": "_contrib/s1/i1.dcm"},
                {
                    "instance_id": "i2",
                    "s3_key": "raw2",
                    # Malformed manifest aiming at a canonical key: refused.
                    "staged_redacted_key": "patients/p/studies/s/series/x/instances/i.dcm",
                },
            ]
        },
    )
    removed = await promotion.purge_submission_staged(item)
    assert removed == 1
    assert storage.deleted == ["_contrib/s1/i1.dcm"]
    assert "patients/p/studies/s/series/x/instances/i.dcm" in storage.objects


# --- review-hardening regressions (adversarial review) -----------------------


@skip_if_no_db
@pytest.mark.asyncio
async def test_promote_t4_transient_ingest_error_rolls_back(
    db_session, make_user, make_study, monkeypatch
) -> None:
    # A transient failure inside DicomIngestor.ingest_blob (S3/DB) must NOT be
    # swallowed into a per-instance skip: it propagates so the promote rolls
    # back and the item stays 'accepted' for the maintenance sweep to retry —
    # never a silently incomplete public clone.
    storage = _StubStorage()
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    owner = await make_user(is_admin=True)
    study, series = await make_study(owner, modality="US", body_part="ABDOMEN")
    inst = Instance(
        id=uuid.uuid4(),
        series_id=series.id,
        sop_instance_uid=f"1.2.3.{uuid.uuid4().int}"[:64],
        s3_bucket="b",
        s3_key=f"patients/x/{uuid.uuid4()}.dcm",
        pixel_phi_risk="high",
        pixel_deid_status="unprocessed",
    )
    db_session.add(inst)
    await db_session.flush()
    staged = _scrubbed_us(seed=21)
    staged_key = redaction.staged_redacted_key("SUB", str(inst.id))
    storage.objects[staged_key] = staged
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="promoting",
        source_study_id=study.id,
        reviewed_by_subject_id=owner.subject_id,
        manifest={
            "instances": [
                {
                    "instance_id": str(inst.id),
                    "name": "us-0.dcm",
                    "s3_bucket": inst.s3_bucket,
                    "s3_key": inst.s3_key,
                    "risk_level": "high",
                    "staged_redacted_key": staged_key,
                    "staged_sha256": hashlib.sha256(staged).hexdigest(),
                    "staged_residual": True,
                    "staged_redactions": [],
                }
            ]
        },
    )
    db_session.add(sub)
    await db_session.commit()

    class _Boom(RuntimeError):
        pass

    async def _boom_ingest(self, blob):
        raise _Boom("s3 timeout")

    monkeypatch.setattr("bvphoenix.services.dicom_ingest.DicomIngestor.ingest_blob", _boom_ingest)
    with pytest.raises(_Boom):
        await promotion.promote_submission(db_session, item=sub, actor=SYSTEM_ACTOR)
    await db_session.rollback()


@skip_if_no_db
@pytest.mark.asyncio
async def test_promote_t4_invalid_blob_skips_per_instance(
    db_session, make_user, make_study, monkeypatch
) -> None:
    # A genuinely invalid blob (InvalidDicomError) is a permanent per-instance
    # skip — the OTHER instances still publish.
    from pydicom.errors import InvalidDicomError

    storage = _StubStorage()
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    owner = await make_user(is_admin=True)
    study, _series = await make_study(owner, modality="US", body_part="ABDOMEN")

    good = _scrubbed_us(seed=31)
    bad = _scrubbed_us(seed=32)
    entries = []
    for name, blob in (("bad", bad), ("good", good)):
        iid = str(uuid.uuid4())
        key = redaction.staged_redacted_key("SUB", iid)
        storage.objects[key] = blob
        entries.append(
            {
                "instance_id": iid,
                "name": f"{name}.dcm",
                "s3_bucket": "b",
                "s3_key": f"raw/{name}.dcm",
                "risk_level": "high",
                "staged_redacted_key": key,
                "staged_sha256": hashlib.sha256(blob).hexdigest(),
                "staged_residual": True,
                "staged_redactions": [],
            }
        )
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="promoting",
        source_study_id=study.id,
        reviewed_by_subject_id=owner.subject_id,
        manifest={"instances": entries},
    )
    db_session.add(sub)
    await db_session.commit()

    real_ingest = __import__(
        "bvphoenix.services.dicom_ingest", fromlist=["DicomIngestor"]
    ).DicomIngestor.ingest_blob

    # Entries are ordered [bad, good]; the promote loader stamps the staged
    # bytes before ingest, so match by call order (first = bad) rather than by
    # byte identity.
    calls = {"n": 0}

    async def _selective(self, blob):
        calls["n"] += 1
        if calls["n"] == 1:
            raise InvalidDicomError("not a dicom")
        return await real_ingest(self, blob)

    monkeypatch.setattr("bvphoenix.services.dicom_ingest.DicomIngestor.ingest_blob", _selective)
    refs = await promotion.promote_submission(db_session, item=sub, actor=SYSTEM_ACTOR)
    await db_session.commit()
    assert refs["published"] == 1
    assert [s["reason"] for s in refs["skipped"]] == ["ingest:InvalidDicomError"]


@skip_if_no_db
@pytest.mark.asyncio
async def test_offer_rejects_duplicate_live_submission(db_session, make_user, make_study) -> None:
    # A study with a live/promoted submission at the same tier cannot be
    # re-offered (409): the t4 clone path would dedup the second submission's
    # reviewed bytes yet stamp its provenance — a false attestation.
    from fastapi import HTTPException

    from bvphoenix.api import contributions as api

    owner = await make_user(is_admin=True)
    study, _series = await make_study(owner, modality="US")
    db_session.add(
        Submission(
            id=uuid.uuid4(),
            target_tier="t4",
            status="promoted",
            source_study_id=study.id,
        )
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as ei:
        await api.offer_submission(
            body=api.CreateSubmissionIn(study_id=study.id, target_tier="t4"),
            db=db_session,
            user=owner,
        )
    assert ei.value.status_code == 409

    # A rejected prior submission does NOT block a fresh offer at the same tier.
    await db_session.execute(
        Submission.__table__.delete().where(Submission.source_study_id == study.id)
    )
    db_session.add(
        Submission(
            id=uuid.uuid4(),
            target_tier="t4",
            status="rejected",
            source_study_id=study.id,
        )
    )
    await db_session.commit()
    # create_submission enqueues via redis (best-effort, caught) — the guard
    # must let this through without a 409.
    out = await api.offer_submission(
        body=api.CreateSubmissionIn(study_id=study.id, target_tier="t4"),
        db=db_session,
        user=owner,
    )
    assert out.status in ("received", "processing")


@pytest.mark.asyncio
async def test_on_reject_hook_does_not_purge_in_transaction(monkeypatch) -> None:
    # Finding: the reject purge must NOT run inside the decision transaction (an
    # S3 delete can't be rolled back). The profile's on_reject hook leaves the
    # staged blobs alone; the endpoint purges post-commit.
    from bvphoenix.services.public_contribution.profile import PUBLIC_CONTRIBUTION_PROFILE

    storage = _StubStorage({"_contrib/s/i.dcm": b"staged"})
    monkeypatch.setattr(promotion, "get_s3_storage", lambda: storage)
    item = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="rejected",
        manifest={
            "instances": [
                {"instance_id": "i", "s3_key": "raw", "staged_redacted_key": "_contrib/s/i.dcm"}
            ]
        },
    )
    await PUBLIC_CONTRIBUTION_PROFILE.on_reject(None, item, SYSTEM_ACTOR, "no")  # type: ignore[arg-type]
    assert storage.deleted == []  # nothing deleted inside the transaction


@pytest.mark.asyncio
async def test_read_staged_falls_back_when_blob_purged(monkeypatch) -> None:
    # Reviewer preview of a terminal (purged) submission must fall back, not 500.
    from bvphoenix.api import contributions as api

    class _Missing(_StubStorage):
        def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
            raise FileNotFoundError(key)  # purged

    monkeypatch.setattr(api, "get_s3_storage", lambda: _Missing())
    got = await api._read_staged_or_none({"staged_redacted_key": "_contrib/s/i.dcm"})
    assert got is None
    # no staged key -> also None (on-the-fly path)
    assert await api._read_staged_or_none({"s3_key": "raw"}) is None


# --- checks: manifest-less unit path stays classification-only ---------------


@pytest.mark.asyncio
async def test_check_without_manifest_skips_staging(monkeypatch) -> None:
    # Unit-style context (db=None, bare manifest): classification only, no S3.
    called = {"stage": 0}

    def _no_stage(*_a, **_kw):  # pragma: no cover - must not run
        called["stage"] += 1

    monkeypatch.setattr(checks_mod, "stage_component_redaction", _no_stage)
    raw = synthesize_case(seed=11, size=(60, 80)).dicom

    async def _read() -> bytes:
        return raw

    ctx = CheckContext(
        db=None,  # type: ignore[arg-type]
        staged=StagedItem(
            item_id=uuid.uuid4(),
            components=[
                StagedComponent(name="c0.dcm", size_bytes=len(raw), content_type=None, read=_read)
            ],
            manifest={},
        ),
    )
    res = await PixelPhiCheck().run(ctx)
    assert res.verdict == "fail"
    assert called["stage"] == 0
