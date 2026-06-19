"""F6.3: deidentify_reindex_study — registration + scrub + skip paths.

Unit-level. A full integration run would need Postgres + MinIO + arq;
these tests stub boto3 / the DB session so the task's control flow is
exercised without the infra.
"""

from __future__ import annotations

import uuid
from io import BytesIO
from typing import Any

import pytest

from bvworkers.tasks import deidentify_reindex as mod
from bvworkers.tasks.registry import FUNCTIONS


def test_task_is_registered() -> None:
    assert mod.deidentify_reindex_study in FUNCTIONS


# --- fakes ---------------------------------------------------------------


class _Row:
    """Stand-in for ``(await db.execute(...))``. Supports both ``first()``
    (study lookup) and ``all()`` (instance / derivative / series lookups)."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        if isinstance(self._value, list):
            return self._value[0] if self._value else None
        return self._value

    def all(self) -> list[Any]:
        if self._value is None:
            return []
        if isinstance(self._value, list):
            return self._value
        return [self._value]


class _ScriptedSession:
    """Drives the task through its phases by returning scripted rows
    for each successive ``db.execute`` call. The task orders its
    queries as: study row, instance rows (t3/t4 only), derivative
    rows, DELETE derivatives, series rows.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.executes = 0

    async def execute(self, *_: Any, **__: Any) -> _Row:
        self.executes += 1
        payload = self._script.pop(0) if self._script else None
        return _Row(payload)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _Engine:
    async def dispose(self) -> None:
        return None


class _StubS3:
    """Captures get_object / put_object / delete_object calls so tests
    can assert the task touched the expected blobs.

    Kwarg names mirror the boto3 convention (CamelCase) so the task's
    call sites do not need test-only renames; the N803 naming check is
    silenced module-wide below.

    Idempotency is modelled by returning two different body flavours:
    ``_RAW_DIRTY`` for instances that should be re-scrubbed,
    ``_RAW_CLEAN`` for ones that are already clean. ``_fake_scrub``
    collapses the dirty variant to the clean one so the
    unchanged-vs-scrubbed accounting is observable in the task's
    return payload.
    """

    def __init__(self, bodies: dict[tuple[str, str], bytes]) -> None:
        self._bodies = bodies
        self.puts: list[tuple[str, str, bytes]] = []
        self.deletes: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        data = self._bodies[(Bucket, Key)]
        return {"Body": BytesIO(data)}

    # boto3 uses CamelCase kwargs; silence the naming lint for this call.
    def put_object(self, **kw: object) -> None:
        self.puts.append(
            (
                str(kw["Bucket"]),
                str(kw["Key"]),
                bytes(kw["Body"]),  # type: ignore[arg-type]
            )
        )

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.deletes.append((Bucket, Key))


_RAW_DIRTY = b"DICOM-RAW-WITH-PHI"
_RAW_CLEAN = b"DICOM-RAW-SCRUBBED"


def _fake_scrub(src: bytes) -> bytes:
    # Mirror the backend's deid function from the worker's POV: any
    # "dirty" payload collapses to a canonical clean form; already-
    # clean bytes pass through unchanged (idempotent).
    return _RAW_CLEAN if src == _RAW_DIRTY else src


def _wire_monkeypatches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: _ScriptedSession,
    s3: _StubS3 | None,
) -> None:
    monkeypatch.setattr(mod, "create_async_engine", lambda *a, **kw: _Engine())
    monkeypatch.setattr(mod, "AsyncSession", lambda _engine: session)
    if s3 is None:
        monkeypatch.setattr(
            mod,
            "boto3",
            type(
                "B",
                (),
                {"client": staticmethod(lambda *a, **kw: _StubS3({}))},
            ),
        )
    else:
        monkeypatch.setattr(
            mod,
            "boto3",
            type("B", (), {"client": staticmethod(lambda *a, **kw: s3)}),
        )
    monkeypatch.setattr(mod, "_scrub_bytes", _fake_scrub)


# --- tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _ScriptedSession(script=[None])
    _wire_monkeypatches(monkeypatch, session=session, s3=None)
    out = await mod.deidentify_reindex_study({"redis": None}, str(uuid.uuid4()))
    assert out["status"] == "not_found"


@pytest.mark.asyncio
async def test_t2_skips_scrub_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid.uuid4()
    # Script: study row (t2), no derivatives, one series.
    session = _ScriptedSession(
        script=[
            (sid, "t2", None),  # study lookup
            [],  # derivative rows
            [(uuid.uuid4(),)],  # series rows
        ]
    )
    s3 = _StubS3({})
    _wire_monkeypatches(monkeypatch, session=session, s3=s3)

    out = await mod.deidentify_reindex_study({"redis": None}, str(sid))
    assert out["status"] == "reindexed"
    assert out["tier"] == "t2"
    assert out["instances_scrubbed"] == 0
    assert out["instances_unchanged"] == 0
    # Private tiers never touch the raw bucket.
    assert s3.puts == []


@pytest.mark.asyncio
async def test_t3_scrubs_dirty_and_leaves_clean_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    dirty_key = "raw/dirty.dcm"
    clean_key = "raw/clean.dcm"
    s3 = _StubS3(
        bodies={
            ("raw-bucket", dirty_key): _RAW_DIRTY,
            ("raw-bucket", clean_key): _RAW_CLEAN,
        }
    )
    session = _ScriptedSession(
        script=[
            (sid, "t3", None),  # study lookup
            [  # instance rows
                ("raw-bucket", dirty_key),
                ("raw-bucket", clean_key),
            ],
            [],  # derivative rows
            [(uuid.uuid4(),)],  # series rows
        ]
    )
    _wire_monkeypatches(monkeypatch, session=session, s3=s3)

    out = await mod.deidentify_reindex_study({"redis": None}, str(sid))

    assert out["status"] == "reindexed"
    assert out["tier"] == "t3"
    assert out["instances_scrubbed"] == 1
    assert out["instances_unchanged"] == 1
    assert out["instances_errored"] == 0
    # Exactly one PUT — the clean one was a no-op.
    assert s3.puts == [("raw-bucket", dirty_key, _RAW_CLEAN)]


@pytest.mark.asyncio
async def test_t4_scrubs_all_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid.uuid4()
    s3 = _StubS3(
        bodies={
            ("raw-bucket", "a.dcm"): _RAW_DIRTY,
            ("raw-bucket", "b.dcm"): _RAW_DIRTY,
        }
    )
    session = _ScriptedSession(
        script=[
            (sid, "t4", None),
            [("raw-bucket", "a.dcm"), ("raw-bucket", "b.dcm")],
            [],
            [],
        ]
    )
    _wire_monkeypatches(monkeypatch, session=session, s3=s3)

    out = await mod.deidentify_reindex_study({"redis": None}, str(sid))
    assert out["instances_scrubbed"] == 2
    assert len(s3.puts) == 2


@pytest.mark.asyncio
async def test_scrub_error_is_counted_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed DICOM (or an S3 hiccup) on one instance must not
    abort the whole task — the other instances still need to run."""
    sid = uuid.uuid4()

    class _FlakyS3(_StubS3):
        def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
            if Key == "broken.dcm":
                raise RuntimeError("s3 404")
            return super().get_object(Bucket=Bucket, Key=Key)

    s3 = _FlakyS3(
        bodies={
            ("raw-bucket", "ok.dcm"): _RAW_DIRTY,
            # "broken.dcm" is intentionally absent; get_object raises.
        }
    )
    session = _ScriptedSession(
        script=[
            (sid, "t3", None),
            [("raw-bucket", "ok.dcm"), ("raw-bucket", "broken.dcm")],
            [],
            [],
        ]
    )
    _wire_monkeypatches(monkeypatch, session=session, s3=s3)

    out = await mod.deidentify_reindex_study({"redis": None}, str(sid))
    assert out["instances_scrubbed"] == 1
    assert out["instances_errored"] == 1


@pytest.mark.asyncio
async def test_derivatives_are_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid.uuid4()
    s3 = _StubS3(bodies={})
    session = _ScriptedSession(
        script=[
            (sid, "t2", None),  # study
            [  # derivatives
                (uuid.uuid4(), "deriv-bucket", "thumb/a.png"),
                (uuid.uuid4(), "deriv-bucket", "mpr/b.nii.gz"),
            ],
            [],  # series
        ]
    )
    _wire_monkeypatches(monkeypatch, session=session, s3=s3)

    out = await mod.deidentify_reindex_study({"redis": None}, str(sid))
    assert out["derivatives_deleted"] == 2
    assert s3.deletes == [
        ("deriv-bucket", "thumb/a.png"),
        ("deriv-bucket", "mpr/b.nii.gz"),
    ]


@pytest.mark.asyncio
async def test_already_stamped_study_skips_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """A study already de-identified at the CURRENT engine version must not be
    re-scrubbed — DB-stamp idempotency, never file-content trust. (Regression
    for the removed forgeable-tag short-circuit.)"""
    from bvphoenix.config import get_settings as _bvp_settings

    sid = uuid.uuid4()
    s3 = _StubS3(bodies={("raw-bucket", "a.dcm"): _RAW_DIRTY})
    session = _ScriptedSession(
        script=[
            # study row stamped at the current version → scrub phase skipped
            (sid, "t3", _bvp_settings().deid_method_version),
            [],  # derivative rows
            [],  # series rows
        ]
    )
    _wire_monkeypatches(monkeypatch, session=session, s3=s3)

    out = await mod.deidentify_reindex_study({"redis": None}, str(sid))
    assert out["instances_scrubbed"] == 0
    assert out["instances_unchanged"] == 0
    assert s3.puts == []  # no re-mutation of already-scrubbed bytes
