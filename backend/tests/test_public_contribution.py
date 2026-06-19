"""M1: public-contribution review profile — checks + human-only decision gate.

The safety-critical assertions: high-risk burned-in pixels and SR/encapsulated
content route to mandatory human review; unparseable blobs block; the publish
decision is refused for agent actors and requires a reason.
"""

from __future__ import annotations

import uuid
from io import BytesIO

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

from bvphoenix.services.public_contribution.checks import (
    CsamScreenCheck,
    HeaderDeidCheck,
    PixelPhiCheck,
)
from bvphoenix.services.review_queue import (
    CheckContext,
    DecisionPolicy,
    ReviewActor,
    ReviewDecisionError,
    StagedComponent,
    StagedItem,
)

_CT = "1.2.840.10008.5.1.4.1.1.2"
_US = "1.2.840.10008.5.1.4.1.1.6.1"
_SR = "1.2.840.10008.5.1.4.1.1.88.11"


def _dcm(sop: str, **attrs: object) -> bytes:
    ds = Dataset()
    ds.PatientName = attrs.pop("PatientName", "Rossi^Mario")
    ds.PatientID = "MRN-1"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = UID(sop)
    for k, v in attrs.items():
        setattr(ds, k, v)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = UID(sop)
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    fm.ImplementationClassUID = generate_uid()
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def _ctx(*blobs: bytes) -> CheckContext:
    components = []
    for i, blob in enumerate(blobs):

        def _reader(b: bytes = blob):
            async def _read() -> bytes:
                return b

            return _read

        components.append(
            StagedComponent(
                name=f"c{i}.dcm",
                size_bytes=len(blob),
                content_type="application/dicom",
                read=_reader(),
            )
        )
    return CheckContext(
        db=None,  # type: ignore[arg-type]  # these checks don't touch the db
        staged=StagedItem(item_id=uuid.uuid4(), components=components, manifest={}),
    )


# --- checks -----------------------------------------------------------------


async def test_pixel_phi_check_flags_high_risk_ultrasound():
    res = await PixelPhiCheck().run(_ctx(_dcm(_US, Modality="US")))
    assert res.verdict == "fail"  # -> needs_review


async def test_pixel_phi_check_passes_ct_chest():
    res = await PixelPhiCheck().run(
        _ctx(_dcm(_CT, Modality="CT", ImageType=["ORIGINAL", "PRIMARY"], BodyPartExamined="CHEST"))
    )
    assert res.verdict == "pass"


async def test_header_deid_check_passes_clean_ct():
    res = await HeaderDeidCheck().run(_ctx(_dcm(_CT, Modality="CT", PatientName="Rossi^Mario")))
    assert res.verdict == "pass"


async def test_header_deid_check_fails_sr():
    res = await HeaderDeidCheck().run(_ctx(_dcm(_SR, Modality="SR")))
    assert res.verdict == "fail"  # SR routed to review


async def test_header_deid_check_blocks_unparseable():
    res = await HeaderDeidCheck().run(_ctx(b"this is not a DICOM file"))
    assert res.verdict == "block"


async def test_csam_null_screen_passes():
    res = await CsamScreenCheck().run(_ctx(_dcm(_CT, Modality="CT")))
    assert res.verdict == "pass"


# --- human-only decision gate ----------------------------------------------


async def test_gate_refuses_agent_actor():
    policy = DecisionPolicy(gate="human_only", require_reason=True)
    agent = ReviewActor(kind="agent", agent_assistant_id=uuid.uuid4())
    with pytest.raises(ReviewDecisionError):
        await policy.authorize(None, agent, object(), decision="accepted", reason="ok")  # type: ignore[arg-type]


async def test_gate_requires_reason():
    policy = DecisionPolicy(gate="human_only", require_reason=True)
    human = ReviewActor(kind="human", subject_id=uuid.uuid4())
    with pytest.raises(ReviewDecisionError):
        await policy.authorize(None, human, object(), decision="accepted", reason="")  # type: ignore[arg-type]


async def test_gate_allows_human_with_reason():
    policy = DecisionPolicy(gate="human_only", require_reason=True)
    human = ReviewActor(kind="human", subject_id=uuid.uuid4())
    # No raise = authorised (no can_decide hook in this bare policy).
    await policy.authorize(None, human, object(), decision="accepted", reason="approved")  # type: ignore[arg-type]
