"""M1: public-contribution review profile — checks + human-only decision gate.

The safety-critical assertions: high-risk burned-in pixels and SR/encapsulated
content route to mandatory human review; unparseable blobs block; the publish
decision is refused for agent actors and requires a reason.
"""

from __future__ import annotations

import uuid
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

import bvphoenix.config as config_mod
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


def _low_risk_ct(body: str = "HEAD", rows: int = 40, cols: int = 40, fill: int = 100) -> bytes:
    """A head/face CT with real pixels: classify_pixel_risk == 'low' and a
    decodable image so the de-facer can be exercised by PixelPhiCheck."""
    ds = Dataset()
    ds.Modality = "CT"
    ds.BodyPartExamined = body
    ds.SOPClassUID = UID(_CT)
    ds.SOPInstanceUID = generate_uid()
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((rows, cols), fill, dtype=np.uint8).tobytes()
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = UID(_CT)
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    return buf.getvalue()


def _deid_settings(**over: object) -> SimpleNamespace:
    """Minimal settings stand-in for get_defacer() resolution in the checks."""
    base = {"face_deid_enabled": False, "face_deid_mode": "null"}
    base.update(over)
    return SimpleNamespace(**base)


# --- checks -----------------------------------------------------------------


async def test_pixel_phi_check_flags_high_risk_ultrasound():
    res = await PixelPhiCheck().run(_ctx(_dcm(_US, Modality="US")))
    assert res.verdict == "fail"  # -> needs_review


async def test_pixel_phi_check_passes_ct_chest():
    res = await PixelPhiCheck().run(
        _ctx(_dcm(_CT, Modality="CT", ImageType=["ORIGINAL", "PRIMARY"], BodyPartExamined="CHEST"))
    )
    assert res.verdict == "pass"


# --- M6d: face-risk (recognizable-visual-feature) gate, toggled by de-facing ---
# (_low_risk_ct builds a head/face CT -> classify_pixel_risk == 'low'.)


async def test_pixel_phi_check_passes_face_ct_when_defacing_off(monkeypatch):
    # De-facing disabled (default): face-risk ships as today (pass) — no regression.
    monkeypatch.setattr(config_mod, "get_settings", lambda: _deid_settings(face_deid_enabled=False))
    res = await PixelPhiCheck().run(_ctx(_low_risk_ct()))
    assert res.verdict == "pass"
    assert res.details["components"]["c0.dcm"]["risk"] == "low"


async def test_pixel_phi_check_flags_face_ct_when_defacing_null(monkeypatch):
    # De-facing enabled, null mode: nothing masked, but the instance is routed to
    # human review (never auto-passed) and the reviewer sees it was not de-faced.
    monkeypatch.setattr(config_mod, "get_settings", lambda: _deid_settings(face_deid_enabled=True))
    res = await PixelPhiCheck().run(_ctx(_low_risk_ct()))
    assert res.verdict == "fail"  # -> needs_review
    entry = res.details["components"]["c0.dcm"]
    assert entry["face_deid_applied"] is False
    assert entry["face_deid_reason"] == "null_defacer_no_op"


async def test_pixel_phi_check_flags_face_ct_when_defacing_heuristic(monkeypatch):
    # Heuristic mode masks the anterior band but the verdict is STILL fail: the
    # heuristic is not validated de-facing and a human must confirm before RVF=NO.
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: _deid_settings(face_deid_enabled=True, face_deid_mode="heuristic"),
    )
    res = await PixelPhiCheck().run(_ctx(_low_risk_ct()))
    assert res.verdict == "fail"
    entry = res.details["components"]["c0.dcm"]
    assert entry["face_deid_applied"] is True
    assert entry["face_deid_reason"] == "heuristic_anterior_band"


async def test_pixel_phi_check_flags_face_ct_roi_refused(monkeypatch):
    # An ROI-bearing region (orbit) the heuristic refuses to mask still routes to
    # review — never auto-passed — recording why it was not de-faced.
    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda: _deid_settings(face_deid_enabled=True, face_deid_mode="heuristic"),
    )
    res = await PixelPhiCheck().run(_ctx(_low_risk_ct(body="ORBIT")))
    assert res.verdict == "fail"
    entry = res.details["components"]["c0.dcm"]
    assert entry["face_deid_applied"] is False
    assert entry["face_deid_reason"].startswith("face_is_roi")


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
