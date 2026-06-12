"""Gate semantics of ``require_admin_or_scoped_agent``.

The embeddings-admin surface (api/embeddings_admin.py) is the first
consumer: humans must be platform admins, agent tokens additionally
need the operator-granted scope AND an admin owner. These call the dep
directly with stubs (no HTTP server, no DB) following the pattern of
``test_share_agent_scope.py`` to avoid the asyncpg event-loop flakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bvphoenix.auth import require_admin_or_scoped_agent

_SCOPE = "admin:embeddings"


def _request(*, is_agent: bool, scopes: list[str] | None = None) -> SimpleNamespace:
    state = SimpleNamespace(is_agent=is_agent, agent_scope=scopes or [])
    return SimpleNamespace(state=state)


def _user(*, is_admin: bool) -> SimpleNamespace:
    return SimpleNamespace(is_admin=is_admin)


@pytest.mark.asyncio
async def test_human_admin_passes() -> None:
    dep = require_admin_or_scoped_agent(_SCOPE)
    user = _user(is_admin=True)
    assert await dep(request=_request(is_agent=False), user=user) is user


@pytest.mark.asyncio
async def test_human_non_admin_refused() -> None:
    dep = require_admin_or_scoped_agent(_SCOPE)
    with pytest.raises(HTTPException) as exc:
        await dep(request=_request(is_agent=False), user=_user(is_admin=False))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_agent_with_scope_and_admin_owner_passes() -> None:
    dep = require_admin_or_scoped_agent(_SCOPE)
    user = _user(is_admin=True)
    req = _request(is_agent=True, scopes=[_SCOPE])
    assert await dep(request=req, user=user) is user


@pytest.mark.asyncio
async def test_agent_without_scope_refused() -> None:
    # Owner is admin but the operator never granted the scope: the
    # carve-out must stay opt-in per assistant.
    dep = require_admin_or_scoped_agent(_SCOPE)
    req = _request(is_agent=True, scopes=["patients:read"])
    with pytest.raises(HTTPException) as exc:
        await dep(request=req, user=_user(is_admin=True))
    assert exc.value.status_code == 403
    assert _SCOPE in str(exc.value.detail)


@pytest.mark.asyncio
async def test_agent_with_scope_but_non_admin_owner_refused() -> None:
    # The scope grant alone must never elevate: a non-admin owner's
    # assistant stays out even when the scope string is present.
    dep = require_admin_or_scoped_agent(_SCOPE)
    req = _request(is_agent=True, scopes=[_SCOPE])
    with pytest.raises(HTTPException) as exc:
        await dep(request=req, user=_user(is_admin=False))
    assert exc.value.status_code == 403
