"""The registration safety-net decorator must surface worker crashes.

The follow-up viewer polls the REGISTRATION row, not the Job. If
``register_series`` crashes before it can mark the registration terminal
(OOM on two large CT volumes, a partial deploy, an import error), the row
must still be flipped to ``failed`` so the viewer shows a real error
instead of hanging until its own poll ceiling. These tests pin that
contract WITHOUT a database: the raw UPDATE helper is stubbed and we only
assert the decorator's control flow (called on crash, skipped on success,
original exception re-raised, second positional arg treated as a
registration id).
"""

from __future__ import annotations

import pytest

from bvworkers import job_safety


@pytest.mark.asyncio
async def test_safety_net_flips_registration_on_crash(monkeypatch):
    calls: list[dict] = []

    async def _fake_mark(registration_id, *, code, message, db_url_env="BVP_DATABASE_URL_SYNC"):
        calls.append({"id": registration_id, "code": code, "message": message})
        return True

    monkeypatch.setattr(job_safety, "mark_registration_failed_raw", _fake_mark)

    @job_safety.with_registration_safety_net("register_series")
    async def boom(ctx, registration_id, *args):
        raise RuntimeError("simulated OOM")

    with pytest.raises(RuntimeError, match="simulated OOM"):
        await boom({}, "11111111-1111-1111-1111-111111111111", "fixed", "moving")

    assert len(calls) == 1
    assert calls[0]["id"] == "11111111-1111-1111-1111-111111111111"
    # Code is derived from the task name so the UI/ops can tell where it died.
    assert calls[0]["code"] == "register_series_unhandled"
    # The original exception type + message are preserved for the user.
    assert "RuntimeError" in calls[0]["message"]
    assert "simulated OOM" in calls[0]["message"]


@pytest.mark.asyncio
async def test_safety_net_noop_on_success(monkeypatch):
    calls: list[dict] = []

    async def _fake_mark(registration_id, *, code, message, db_url_env="BVP_DATABASE_URL_SYNC"):
        calls.append({"id": registration_id})
        return True

    monkeypatch.setattr(job_safety, "mark_registration_failed_raw", _fake_mark)

    @job_safety.with_registration_safety_net("register_series")
    async def ok(ctx, registration_id, *args):
        return "done"

    result = await ok({}, "22222222-2222-2222-2222-222222222222")
    assert result == "done"
    assert calls == []  # a clean run never touches the failure path


@pytest.mark.asyncio
async def test_mark_registration_failed_raw_bad_id_returns_false():
    # Invalid UUID is handled gracefully (returns False, never raises) so the
    # decorator's outer guard cannot itself blow up the worker.
    assert (
        await job_safety.mark_registration_failed_raw("not-a-uuid", code="x", message="y") is False
    )
