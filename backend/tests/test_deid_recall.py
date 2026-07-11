"""WP4: curated-corpus export (labelled GT -> answer_key) + persistent recall.

Exercises the export round-trip (``submissions.gt_boxes`` -> on-disk
``load_public_corpus`` format), the recall aggregation, and the admin
recall-runs endpoint (PHI-bearing ``missed`` withheld unless requested).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bvphoenix.api import contributions as api
from bvphoenix.cli.deid_recall import evaluate_corpus
from bvphoenix.cli.export_deid_corpus import export_labeled_corpus
from bvphoenix.config import get_settings
from bvphoenix.db.models import DeidRecallRun, Submission
from bvphoenix.services.pixel_deid import PixelDeidResult, PixelRisk
from bvphoenix.services.pixel_deid_eval import load_public_corpus, synthesize_case

from .conftest import skip_if_no_db


class _StubStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def get_object_bytes(self, *, bucket: str, key: str) -> bytes:
        return self._objects[key]


@skip_if_no_db
@pytest.mark.asyncio
async def test_export_roundtrips_to_load_public_corpus(db_session, tmp_path) -> None:
    case = synthesize_case(seed=1, size=(120, 160))
    iid = str(uuid.uuid4())
    boxes = [
        {"x": g.x, "y": g.y, "w": g.w, "h": g.h, "text": g.text, "category": g.category}
        for g in case.gt
    ]
    sub = Submission(
        id=uuid.uuid4(),
        target_tier="t4",
        status="needs_review",
        manifest={
            "instances": [
                {"instance_id": iid, "name": "us.dcm", "s3_bucket": "b", "s3_key": "raw/us.dcm"}
            ]
        },
        gt_boxes={iid: boxes},
    )
    db_session.add(sub)
    await db_session.commit()

    storage = _StubStorage({"raw/us.dcm": case.dicom})
    sync_engine = create_engine(get_settings().database_url_sync, future=True)
    with Session(sync_engine) as sdb:
        stats = export_labeled_corpus(sdb, storage, tmp_path, frame0_extract=False)
    assert stats["instances"] == 1

    # The export must read back through the SAME loader the recall gate uses.
    loaded = {c.dicom: c.gt for c in load_public_corpus(tmp_path)}
    assert len(loaded) == 1
    gt = next(iter(loaded.values()))
    assert {(g.x, g.y, g.w, g.h) for g in gt} == {(b["x"], b["y"], b["w"], b["h"]) for b in boxes}

    await db_session.execute(Submission.__table__.delete().where(Submission.id == sub.id))
    await db_session.commit()


def test_evaluate_corpus_aggregates(monkeypatch, tmp_path) -> None:
    # Two synthetic cases; a stub redactor covers the first GT box of each -> the
    # aggregate recall reflects covered/total across the whole corpus.
    import json

    from bvphoenix.cli import deid_recall as mod

    answer = {}
    for i, seed in enumerate((1, 2)):
        case = synthesize_case(seed=seed, size=(120, 160))
        name = f"c{i}.dcm"
        (tmp_path / name).write_bytes(case.dicom)
        answer[name] = [
            {"x": g.x, "y": g.y, "w": g.w, "h": g.h, "text": g.text, "category": g.category}
            for g in case.gt
        ]
    (tmp_path / "answer_key.json").write_text(json.dumps(answer))

    def _stub_clean(dicom: bytes, **_kw: object) -> PixelDeidResult:
        # Cover exactly the first GT box of whichever case this is.
        first = next(
            b
            for name, boxes in answer.items()
            for b in boxes  # first box overall is fine
        )
        return PixelDeidResult(
            out_bytes=dicom,
            risk=PixelRisk("high", ()),
            residual_suspect=True,
            redactions=[
                {
                    "x": first["x"],
                    "y": first["y"],
                    "w": first["w"],
                    "h": first["h"],
                    "text": "x",
                    "conf": 90.0,
                }
            ],
        )

    monkeypatch.setattr(mod, "clean_pixel_data", _stub_clean)
    totals, _missed = evaluate_corpus(tmp_path, coverage=0.8)
    assert totals["cases"] == 2
    assert totals["total"] == sum(len(v) for v in answer.values())
    assert 0.0 <= totals["recall"] <= 1.0
    assert totals["covered"] >= 1


@skip_if_no_db
@pytest.mark.asyncio
async def test_recall_runs_endpoint_withholds_missed(db_session, make_user) -> None:
    admin = await make_user(is_admin=True)
    run = DeidRecallRun(
        id=uuid.uuid4(),
        corpus_kind="curated",
        corpus_version="v1",
        corpus_hash="deadbeef",
        engine={"deid_method_version": "phoenix-deid-3"},
        coverage=0.8,
        recall=0.95,
        covered=19,
        total=20,
        cases=10,
        missed={"sample": [{"text": "RESIDUAL PHI"}]},
    )
    db_session.add(run)
    await db_session.commit()

    default = await api.list_recall_runs(db=db_session, user=admin, corpus_kind="curated")
    assert default and default[0].recall == pytest.approx(0.95)
    assert default[0].missed is None  # PHI withheld by default

    with_missed = await api.list_recall_runs(
        db=db_session, user=admin, corpus_kind="curated", include_missed=True
    )
    assert with_missed[0].missed == {"sample": [{"text": "RESIDUAL PHI"}]}

    await db_session.execute(DeidRecallRun.__table__.delete().where(DeidRecallRun.id == run.id))
    await db_session.commit()
