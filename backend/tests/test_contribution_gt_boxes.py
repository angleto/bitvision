"""M6c review-UI box-labeling: the contribution GT-box endpoints.

A reviewer draws the ground-truth burned-in-PHI boxes on a staged instance; the
answer key those boxes form is what the automatic pixel redaction's recall is
scored against. These tests exercise the endpoints directly (no HTTP layer),
stubbing S3 with a synthetic burned-in-PHI DICOM.
"""

from __future__ import annotations

import uuid

import pytest

from bvphoenix.api import contributions as api
from bvphoenix.db.models import Submission
from bvphoenix.services import pixel_deid
from bvphoenix.services.pixel_deid import PixelDeidResult, PixelRisk
from bvphoenix.services.pixel_deid_eval import synthesize_case

from .conftest import skip_if_no_db

_INSTANCE = "1.2.3.4.5"


class _StubStorage:
    def __init__(self, blob: bytes) -> None:
        self._blob = blob

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        return self._blob


async def _make_submission(db_session) -> Submission:
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="needs_review",
        manifest={
            "instances": [
                {
                    "instance_id": _INSTANCE,
                    "name": "us-0.dcm",
                    "pixel_phi_risk": "high",
                    "s3_bucket": "bvphoenix-datasets",
                    "s3_key": "pixel-deid/staged/us-0.dcm",
                }
            ]
        },
    )
    db_session.add(sub)
    await db_session.commit()
    await db_session.refresh(sub)
    return sub


@skip_if_no_db
@pytest.mark.asyncio
async def test_render_original_returns_native_png(db_session, make_user, monkeypatch) -> None:
    owner = await make_user(is_admin=True)
    case = synthesize_case(seed=1, size=(200, 300))  # (h, w)
    monkeypatch.setattr(api, "get_s3_storage", lambda: _StubStorage(case.dicom))
    sub = await _make_submission(db_session)

    resp = await api.render_instance(
        submission_id=sub.id, instance_id=_INSTANCE, db=db_session, user=owner, variant="original"
    )
    assert resp.media_type == "image/png"
    assert resp.body[:8] == b"\x89PNG\r\n\x1a\n"
    # Native resolution: width=columns=300, height=rows=200.
    assert resp.headers["x-image-width"] == "300"
    assert resp.headers["x-image-height"] == "200"
    assert resp.headers["cache-control"] == "no-store"


@skip_if_no_db
@pytest.mark.asyncio
async def test_save_get_gt_boxes_roundtrip_and_etag_bump(
    db_session, make_user, monkeypatch
) -> None:
    owner = await make_user(is_admin=True)
    case = synthesize_case(seed=2, size=(200, 300))
    monkeypatch.setattr(api, "get_s3_storage", lambda: _StubStorage(case.dicom))
    sub = await _make_submission(db_session)
    old_etag = sub.etag

    body = api.SaveGtBoxesIn(
        boxes=[
            api.GtBoxIn(x=10, y=10, w=80, h=20, text="ROSSI MARIO", category="name"),
            api.GtBoxIn(
                x=10, y=40, w=120, h=20, text="RSSMRA80A01H501U", category="codice_fiscale"
            ),
        ]
    )
    out = await api.save_gt_boxes(
        submission_id=sub.id,
        instance_id=_INSTANCE,
        body=body,
        db=db_session,
        user=owner,
        if_match=str(old_etag),
    )
    assert len(out.boxes) == 2
    assert out.etag != old_etag  # write bumps the optimistic-concurrency token

    got = await api.get_gt_boxes(
        submission_id=sub.id, instance_id=_INSTANCE, db=db_session, user=owner
    )
    assert [(b.x, b.y, b.w, b.h, b.category) for b in got.boxes] == [
        (10, 10, 80, 20, "name"),
        (10, 40, 120, 20, "codice_fiscale"),
    ]


@skip_if_no_db
@pytest.mark.asyncio
async def test_save_clamps_out_of_bounds_and_coerces_unknown_category(
    db_session, make_user, monkeypatch
) -> None:
    owner = await make_user(is_admin=True)
    case = synthesize_case(seed=3, size=(100, 100))  # 100x100
    monkeypatch.setattr(api, "get_s3_storage", lambda: _StubStorage(case.dicom))
    sub = await _make_submission(db_session)

    body = api.SaveGtBoxesIn(
        boxes=[api.GtBoxIn(x=80, y=80, w=999, h=999, text="x", category="bogus")]
    )
    out = await api.save_gt_boxes(
        submission_id=sub.id,
        instance_id=_INSTANCE,
        body=body,
        db=db_session,
        user=owner,
        if_match=str(sub.etag),
    )
    (b,) = out.boxes
    assert b.x == 80 and b.y == 80
    assert b.x + b.w == 100 and b.y + b.h == 100  # clamped to the 100x100 image
    assert b.category == "unknown"  # invalid category coerced


@skip_if_no_db
@pytest.mark.asyncio
async def test_save_requires_if_match(db_session, make_user, monkeypatch) -> None:
    owner = await make_user(is_admin=True)
    case = synthesize_case(seed=4, size=(100, 100))
    monkeypatch.setattr(api, "get_s3_storage", lambda: _StubStorage(case.dicom))
    sub = await _make_submission(db_session)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await api.save_gt_boxes(
            submission_id=sub.id,
            instance_id=_INSTANCE,
            body=api.SaveGtBoxesIn(boxes=[]),
            db=db_session,
            user=owner,
            if_match=None,
        )
    assert ei.value.status_code == 428


@skip_if_no_db
@pytest.mark.asyncio
async def test_gt_score_recall_against_deterministic_redaction(
    db_session, make_user, monkeypatch
) -> None:
    owner = await make_user(is_admin=True)
    case = synthesize_case(seed=5, size=(200, 300))
    monkeypatch.setattr(api, "get_s3_storage", lambda: _StubStorage(case.dicom))
    sub = await _make_submission(db_session)

    # Save two GT boxes.
    gt = [
        api.GtBoxIn(x=10, y=10, w=100, h=20, text="a", category="name"),
        api.GtBoxIn(x=10, y=40, w=100, h=20, text="b", category="date"),
    ]
    await api.save_gt_boxes(
        submission_id=sub.id,
        instance_id=_INSTANCE,
        body=api.SaveGtBoxesIn(boxes=gt),
        db=db_session,
        user=owner,
        if_match=str(sub.etag),
    )

    # Deterministic auto-redaction that covers ONLY the first GT box → recall 0.5.
    def _fake_clean(src: bytes, **_kw: object) -> PixelDeidResult:
        return PixelDeidResult(
            out_bytes=src,
            risk=PixelRisk("high", ("high_risk_modality:US",)),
            residual_suspect=True,
            redactions=[{"x": 10, "y": 10, "w": 100, "h": 20, "text": "a", "conf": 90.0}],
        )

    monkeypatch.setattr(api, "clean_pixel_data", _fake_clean)
    monkeypatch.setattr(pixel_deid, "clean_pixel_data", _fake_clean)

    score = await api.gt_score(
        submission_id=sub.id, instance_id=_INSTANCE, db=db_session, user=owner
    )
    assert score.total == 2
    assert score.covered == 1
    assert score.recall == pytest.approx(0.5)
    assert "b" in score.missed
    assert score.risk_level == "high"
