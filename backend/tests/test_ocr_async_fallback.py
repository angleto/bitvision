"""Unit tests for the OCR sync→async graceful fallback.

The synchronous ``POST /patients/:p/documents/:d/text?inline=true``
endpoint used to leak ``RuntimeError`` from ``services.ocr.run_ocr``
when the pipeline could not produce text (Tesseract binary missing,
traineddata for the requested language unavailable, scan too low-res
to OCR). Returning 500 to the agent forced the LLM to either retry
in a tight loop or surface a noisy error to the user.

The fix degrades that branch into the existing async pipeline: the
worker has retry, dead-letter, and a structured safety net, so a
failed inline run is handed off rather than escalated to the caller.

These tests cover:

* ``_enqueue_ocr_async`` returns 202 + ``X-Job-Id`` on the happy path
  (Redis enqueue succeeds).
* ``_enqueue_ocr_async`` raises 503 when Redis enqueue fails after
  the dedup row was committed (the worker has nothing to pick up).
* The exported helper is wired in such a way that the inline branch
  in ``run_document_text`` calls it through ``except RuntimeError``
  (smoke check on the function body, not a full integration test).
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bvphoenix.api import patients as patients_api
from bvphoenix.api.patients import _enqueue_ocr_async, run_document_text


def _stub_doc() -> Any:
    return SimpleNamespace(id=uuid.UUID("11111111-1111-1111-1111-111111111111"))


def _stub_user() -> Any:
    return SimpleNamespace(subject_id=uuid.UUID("22222222-2222-2222-2222-222222222222"))


def _stub_body(*, force: bool = False, language: str | None = None) -> Any:
    return SimpleNamespace(force=force, language=language, inline=True)


def _stub_idem() -> Any:
    """Idempotency context that just returns the captured payload + status
    so we can read the status_code + extra_headers off the response."""
    captured: dict[str, Any] = {}

    def _capture(payload: dict[str, Any], *, status_code: int, extra_headers: dict | None = None):
        captured["payload"] = payload
        captured["status_code"] = status_code
        captured["extra_headers"] = extra_headers or {}
        return SimpleNamespace(**captured)

    return SimpleNamespace(capture=_capture, replay=None, _captured=captured)


def _stub_settings() -> Any:
    return SimpleNamespace(redis_url="redis://stub", s3_bucket_raw="stub-bucket")


@pytest.mark.asyncio
async def test_enqueue_ocr_async_returns_202_with_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: dedup miss + redis enqueue succeeds → 202 + X-Job-Id."""
    job_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    enqueue_result = SimpleNamespace(
        deduped=False,
        job=SimpleNamespace(id=job_id),
    )

    fake_redis = SimpleNamespace(
        enqueue_job=AsyncMock(return_value=SimpleNamespace(job_id="arq-handle-abc")),
        close=AsyncMock(),
    )

    monkeypatch.setattr(
        "bvphoenix.services.jobs.enqueue_or_get",
        AsyncMock(return_value=enqueue_result),
    )
    monkeypatch.setattr(
        "bvphoenix.services.jobs.set_arq_job_id",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "bvphoenix.services.jobs.mark_failed",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "bvphoenix.services.arq_redis.redis_settings",
        lambda url: SimpleNamespace(host="stub", port=0),
    )
    monkeypatch.setattr(
        "arq.create_pool",
        AsyncMock(return_value=fake_redis),
    )

    db = MagicMock()
    db.commit = AsyncMock()
    idem = _stub_idem()

    response = await _enqueue_ocr_async(
        db=db,
        user=_stub_user(),
        doc=_stub_doc(),
        target_file_id=None,
        body=_stub_body(),
        idem=idem,
        settings=_stub_settings(),
    )

    assert response.status_code == 202
    assert response.extra_headers == {"X-Job-Id": str(job_id)}
    assert response.payload["engine"] == "pending"
    assert response.payload["text"] == ""
    fake_redis.enqueue_job.assert_awaited_once_with("run_document_ocr", str(job_id))


@pytest.mark.asyncio
async def test_enqueue_ocr_async_503_when_redis_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis pool blows up after the dedup row landed → 503 + mark_failed."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    job_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    enqueue_result = SimpleNamespace(
        deduped=False,
        job=SimpleNamespace(id=job_id),
    )
    mark_failed_mock = AsyncMock()

    monkeypatch.setattr(
        "bvphoenix.services.jobs.enqueue_or_get",
        AsyncMock(return_value=enqueue_result),
    )
    monkeypatch.setattr(
        "bvphoenix.services.jobs.set_arq_job_id",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "bvphoenix.services.jobs.mark_failed",
        mark_failed_mock,
    )
    monkeypatch.setattr(
        "bvphoenix.services.arq_redis.redis_settings",
        lambda url: SimpleNamespace(host="stub", port=0),
    )
    monkeypatch.setattr(
        "arq.create_pool",
        AsyncMock(side_effect=ConnectionError("redis is down")),
    )

    db = MagicMock()
    db.commit = AsyncMock()
    idem = _stub_idem()

    with pytest.raises(StarletteHTTPException) as exc_info:
        await _enqueue_ocr_async(
            db=db,
            user=_stub_user(),
            doc=_stub_doc(),
            target_file_id=None,
            body=_stub_body(),
            idem=idem,
            settings=_stub_settings(),
        )

    # ``problem(503, ...)`` → HTTPException with status_code 503.
    assert exc_info.value.status_code == 503
    mark_failed_mock.assert_awaited_once()
    # Sanity: the failure error code matches what the catch branch emits.
    err_kwargs = mark_failed_mock.await_args.kwargs
    assert err_kwargs["error"]["code"] == "enqueue_failed"


def test_inline_branch_falls_back_via_runtime_error() -> None:
    """The inline OCR branch must catch ``RuntimeError`` and call the
    async helper. We assert the source contains the key elements rather
    than spinning up a full DB-backed integration test — the helper is
    covered above and the wiring is what we want to lock in.
    """
    src = inspect.getsource(run_document_text)
    # The fallback delegates to the same helper.
    assert "_enqueue_ocr_async(" in src
    # The handler catches RuntimeError specifically (deterministic
    # 422 / 404 paths must NOT be swallowed by this branch).
    assert "except RuntimeError" in src
    # Structured warning so ops can correlate to the worker job that
    # ends up doing the actual extraction.
    assert "ocr_inline_fallback" in src


def test_helper_is_module_private_and_async() -> None:
    """Smoke contract: the helper exists, is private, and is awaitable."""
    helper = patients_api._enqueue_ocr_async
    assert inspect.iscoroutinefunction(helper)
    assert helper.__name__.startswith("_")
