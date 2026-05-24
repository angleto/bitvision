"""Smoke test for the ping task."""

from bvworkers.tasks.ping import ping


async def test_ping_returns_pong() -> None:
    result = await ping({})
    assert result["pong"] == "ok"
