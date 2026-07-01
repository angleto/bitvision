"""On-write coarse text embedding (services.text_embedding + API composers).

Flow task 84220e21. ``enqueue_text_embed`` is the single on-write entry
point that fans a coarse embed job out to EVERY routed, active text model in
the registry, so a document / patient / report_content / finding lands in
the MiniLM store today and the BGE-M3 store the moment it is activated — no
call-site change. These tests pin: the fan-out, the best-effort contract
(blank text / no models / failures never raise), the per-target text
composers, and that the *real* registry drives the fan-out.
"""

from __future__ import annotations

import uuid

from bvphoenix.services import text_embedding as te
from bvphoenix.services.text_models import TextModelSpec
from tests.conftest import skip_if_no_db


class _FakeRedis:
    """Records enqueue_job calls; asserts the pool is always closed."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple]] = []
        self.closed = False

    async def enqueue_job(self, task_name: str, *args) -> None:
        self.jobs.append((task_name, args))

    async def close(self) -> None:
        self.closed = True


def _two_specs() -> dict[str, TextModelSpec]:
    return {
        "minilm-multi-v1": TextModelSpec(
            model_id="minilm-multi-v1",
            arq_task="embed_text_ml",
            store_table="text_embeddings",
            dim=384,
        ),
        "bge-m3-v1": TextModelSpec(
            model_id="bge-m3-v1",
            arq_task="embed_bge_m3_all",
            store_table="text_embeddings_bge_m3",
            dim=1024,
        ),
    }


async def test_enqueue_fans_out_over_every_active_model(monkeypatch):
    specs = _two_specs()

    async def fake_load(_db):
        return specs

    fake = _FakeRedis()

    async def fake_create_pool(_settings):
        return fake

    monkeypatch.setattr(te, "load_text_model_specs", fake_load)
    monkeypatch.setattr("arq.create_pool", fake_create_pool)

    tid = uuid.uuid4()
    await te.enqueue_text_embed(object(), target_kind="patient", target_id=tid, text="Mario Rossi")

    # One job per active model, each carrying the same coarse target tuple.
    assert set(fake.jobs) == {
        ("embed_text_ml", ("patient", str(tid), "Mario Rossi")),
        ("embed_bge_m3_all", ("patient", str(tid), "Mario Rossi")),
    }
    assert fake.closed is True


async def test_enqueue_blank_text_is_noop(monkeypatch):
    touched = False

    async def fake_load(_db):
        nonlocal touched
        touched = True
        return _two_specs()

    monkeypatch.setattr(te, "load_text_model_specs", fake_load)
    # Whitespace-only must short-circuit before even hitting the registry.
    await te.enqueue_text_embed(
        object(), target_kind="document", target_id=uuid.uuid4(), text="  \n "
    )
    assert touched is False


async def test_enqueue_no_active_models_opens_no_pool(monkeypatch):
    async def fake_load(_db):
        return {}

    def boom(*_a, **_k):
        raise AssertionError("opened an arq pool with no routed models")

    monkeypatch.setattr(te, "load_text_model_specs", fake_load)
    monkeypatch.setattr("arq.create_pool", boom)
    # Must return cleanly without opening a pool.
    await te.enqueue_text_embed(object(), target_kind="finding", target_id=uuid.uuid4(), text="x")


async def test_enqueue_swallows_failures(monkeypatch):
    async def fake_load(_db):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(te, "load_text_model_specs", fake_load)
    # Best-effort contract: a failure here must never propagate to the write.
    await te.enqueue_text_embed(object(), target_kind="finding", target_id=uuid.uuid4(), text="x")


def test_patient_embed_text_composition():
    class _P:
        display_name = "Mario Rossi"
        notes = "diabete tipo 2"

    class _PartialP:
        display_name = "Mario Rossi"
        notes = None

    class _BlankP:
        display_name = None
        notes = None

    assert te.patient_embed_text(_P()) == "Mario Rossi\n\ndiabete tipo 2"
    assert te.patient_embed_text(_PartialP()) == "Mario Rossi"
    assert te.patient_embed_text(_BlankP()) == ""  # -> helper no-ops


def test_report_content_embed_text_composition():
    class _RC:
        title = "TC torace"
        narrative_md = "nessuna lesione focale"
        findings_md = None
        recommendations_md = "controllo a 6 mesi"

    assert (
        te.report_content_embed_text(_RC())
        == "TC torace\n\nnessuna lesione focale\n\ncontrollo a 6 mesi"
    )


@skip_if_no_db
async def test_enqueue_uses_real_registry(db_session, monkeypatch):
    """The fan-out is driven by the live registry, not a hard-coded task.

    The test DB has minilm-multi-v1 + bge-m3-v1 active and routed (and the
    unrouted biomedclip-text-v1, which must be skipped). Both routed models
    must receive the coarse job, each with the identical target tuple.
    """
    fake = _FakeRedis()

    async def fake_create_pool(_settings):
        return fake

    monkeypatch.setattr("arq.create_pool", fake_create_pool)

    tid = uuid.uuid4()
    await te.enqueue_text_embed(
        db_session, target_kind="finding", target_id=tid, text="lesione epatica"
    )

    tasks = {t for (t, _args) in fake.jobs}
    assert "embed_text_ml" in tasks  # minilm-multi-v1 active + routed
    assert "embed_bge_m3_all" in tasks  # bge-m3-v1 active + routed
    assert all(args == ("finding", str(tid), "lesione epatica") for _t, args in fake.jobs)
    assert fake.closed is True


def test_study_embed_text_composition() -> None:
    # description + de-duped modalities + de-duped series body parts (0ece383b).
    assert (
        te.study_embed_text(
            study_description="TC torace",
            modalities=["CT", "CT"],
            body_parts=["CHEST", "CHEST", "ABDOMEN"],
        )
        == "TC torace; CT; CHEST, ABDOMEN"
    )
    # All-blank composes to empty (the embed helper then no-ops).
    assert te.study_embed_text(study_description=None, modalities=[], body_parts=None) == ""
    # Only structural metadata (no description) still yields a vector-worthy string.
    assert (
        te.study_embed_text(study_description=None, modalities=["MR"], body_parts=["BRAIN"])
        == "MR; BRAIN"
    )
