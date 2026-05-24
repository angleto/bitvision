"""Trivial task used to verify the worker is alive."""

from bvworkers import __version__


async def ping(ctx: dict) -> dict[str, str]:  # type: ignore[type-arg]
    return {"pong": "ok", "version": __version__}
