"""Unit tests for the configurable per-subject storage quota."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from bvphoenix.services.storage_quota import (
    DEFAULT_QUOTA_GB,
    GB_IN_BYTES,
    KEY_ALLOW_USER_OVERRIDE,
    KEY_DEFAULT_QUOTA_GB,
    KEY_USER_QUOTA_PREFIX,
    check_storage_quota,
    get_storage_usage,
    resolve_quota_gb,
)


class _StubDB:
    """Minimal async DB stub: ``execute(stmt)`` returns a result whose
    ``scalar_one_or_none()`` / ``first()`` shapes are pre-loaded.

    The resolver issues SELECTs against ``app_settings``; the usage
    query hits a CTE we mock out with a ``first() -> (bytes,)`` row.
    """

    def __init__(
        self,
        settings: dict[str, object] | None = None,
        usage_bytes: int = 0,
        user_quota_bytes: int | None = None,
    ) -> None:
        self.settings_map = settings or {}
        self.usage_bytes = usage_bytes
        # Mirrors ``users.storage_quota_bytes`` — the per-user override the
        # admin dashboard writes and quota.check_quota_or_raise honors;
        # resolve_quota_gb now honors it too (the reconcile).
        self.user_quota_bytes = user_quota_bytes
        self.calls: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt).strip()
        # Heuristic dispatch: SELECT against app_settings goes through
        # the resolver path; the ``users.storage_quota_bytes`` override
        # query returns a bare scalar; a text() CTE with ``owned_patients``
        # is the usage query. Everything else returns "no row".
        if "app_settings" in sql.lower():
            try:
                compiled_params = stmt.compile().params
            except Exception:
                compiled_params = {}
            key = compiled_params.get("key_1") or compiled_params.get("param_1")
            self.calls.append(f"setting:{key}")
            return _AppSettingResult(self.settings_map.get(str(key)))
        if "storage_quota_bytes" in sql.lower():
            self.calls.append("user_override")
            return _ScalarResult(self.user_quota_bytes)
        if "owned_patients" in sql.lower() or "instances i" in sql.lower():
            self.calls.append("usage_query")
            return _UsageResult(self.usage_bytes)
        return _AppSettingResult(None)


class _AppSettingResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        if self._value is None:
            return None
        return SimpleNamespace(value=self._value)


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _UsageResult:
    def __init__(self, total: int) -> None:
        self._total = total

    def first(self) -> tuple[int]:
        return (self._total,)


# ---------------------------------------------------------------------------
# resolve_quota_gb
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_quota_when_no_settings() -> None:
    db = _StubDB(settings={})
    gb, is_default = await resolve_quota_gb(db, subject_id=uuid.uuid4())
    assert gb == DEFAULT_QUOTA_GB
    assert is_default is True


@pytest.mark.asyncio
async def test_workspace_default_setting_wins_over_hardcoded_default() -> None:
    db = _StubDB(settings={KEY_DEFAULT_QUOTA_GB: 20})
    gb, is_default = await resolve_quota_gb(db, subject_id=uuid.uuid4())
    assert gb == 20.0
    assert is_default is True


@pytest.mark.asyncio
async def test_user_override_wins_when_allowed() -> None:
    sid = uuid.uuid4()
    db = _StubDB(
        settings={
            KEY_DEFAULT_QUOTA_GB: 5,
            f"{KEY_USER_QUOTA_PREFIX}{sid}": 100,
        }
    )
    gb, is_default = await resolve_quota_gb(db, subject_id=sid)
    assert gb == 100.0
    assert is_default is False


@pytest.mark.asyncio
async def test_user_override_disabled_falls_through() -> None:
    sid = uuid.uuid4()
    db = _StubDB(
        settings={
            KEY_DEFAULT_QUOTA_GB: 5,
            KEY_ALLOW_USER_OVERRIDE: False,
            f"{KEY_USER_QUOTA_PREFIX}{sid}": 100,
        }
    )
    gb, is_default = await resolve_quota_gb(db, subject_id=sid)
    assert gb == 5.0
    assert is_default is True


@pytest.mark.asyncio
async def test_malformed_value_falls_through_to_default() -> None:
    sid = uuid.uuid4()
    db = _StubDB(
        settings={
            f"{KEY_USER_QUOTA_PREFIX}{sid}": "not-a-number",
        }
    )
    gb, is_default = await resolve_quota_gb(db, subject_id=sid)
    assert gb == DEFAULT_QUOTA_GB
    assert is_default is True


@pytest.mark.asyncio
async def test_user_storage_quota_bytes_override_is_honored() -> None:
    """The reconcile: users.storage_quota_bytes (the admin-UI per-user value
    already honored by quota.check_quota_or_raise) is now the effective
    per-user quota for the hard-cap gate too, so a quota raised in the UI
    actually lifts the upload gate instead of leaving it on DEFAULT_QUOTA_GB.
    """
    sid = uuid.uuid4()
    db = _StubDB(settings={}, user_quota_bytes=200 * GB_IN_BYTES)
    gb, is_default = await resolve_quota_gb(db, subject_id=sid)
    assert gb == 200.0
    assert is_default is False


@pytest.mark.asyncio
async def test_per_user_app_setting_wins_over_user_column() -> None:
    """The explicit per-user app_setting stays the highest-precedence ops
    override; the users.storage_quota_bytes column is the next layer."""
    sid = uuid.uuid4()
    db = _StubDB(
        settings={f"{KEY_USER_QUOTA_PREFIX}{sid}": 100},
        user_quota_bytes=200 * GB_IN_BYTES,
    )
    gb, is_default = await resolve_quota_gb(db, subject_id=sid)
    assert gb == 100.0
    assert is_default is False


@pytest.mark.asyncio
async def test_user_column_honored_even_when_app_override_disabled() -> None:
    """``allow_user_override`` only gates the app_setting layer; the canonical
    users.storage_quota_bytes override is honored regardless, mirroring
    quota.check_quota_or_raise so the two gates never diverge on the override.
    """
    sid = uuid.uuid4()
    db = _StubDB(
        settings={KEY_DEFAULT_QUOTA_GB: 5, KEY_ALLOW_USER_OVERRIDE: False},
        user_quota_bytes=200 * GB_IN_BYTES,
    )
    gb, is_default = await resolve_quota_gb(db, subject_id=sid)
    assert gb == 200.0
    assert is_default is False


# ---------------------------------------------------------------------------
# get_storage_usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_combines_quota_and_bytes() -> None:
    sid = uuid.uuid4()
    db = _StubDB(
        settings={KEY_DEFAULT_QUOTA_GB: 10},
        usage_bytes=2 * GB_IN_BYTES,
    )
    usage = await get_storage_usage(db, subject_id=sid)
    assert usage.bytes_used == 2 * GB_IN_BYTES
    assert usage.bytes_quota == 10 * GB_IN_BYTES
    assert usage.quota_gb == 10.0
    assert usage.is_workspace_default is True


# ---------------------------------------------------------------------------
# check_storage_quota
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_allows_when_under_quota() -> None:
    db = _StubDB(usage_bytes=1 * GB_IN_BYTES)
    usage = await check_storage_quota(db, subject_id=uuid.uuid4(), additional_bytes=1 * GB_IN_BYTES)
    assert usage.bytes_used == 1 * GB_IN_BYTES


@pytest.mark.asyncio
async def test_check_raises_413_when_over_quota() -> None:
    sid = uuid.uuid4()
    db = _StubDB(usage_bytes=4 * GB_IN_BYTES)
    with pytest.raises(HTTPException) as exc:
        await check_storage_quota(db, subject_id=sid, additional_bytes=2 * GB_IN_BYTES)
    assert exc.value.status_code == 413
    detail = exc.value.detail
    assert detail["code"] == "storage_quota_exceeded"
    assert detail["bytes_used"] == 4 * GB_IN_BYTES
    assert detail["bytes_quota"] == int(DEFAULT_QUOTA_GB * GB_IN_BYTES)


@pytest.mark.asyncio
async def test_check_with_zero_additional_passes_when_at_quota() -> None:
    """At-quota state allows ``additional_bytes=0`` introspection
    (e.g. usage GET) but blocks any new upload (additional > 0)."""
    sid = uuid.uuid4()
    db = _StubDB(usage_bytes=int(DEFAULT_QUOTA_GB * GB_IN_BYTES))
    # additional=0 → projected == quota → not strictly greater → OK.
    usage = await check_storage_quota(db, subject_id=sid, additional_bytes=0)
    assert usage.bytes_used == int(DEFAULT_QUOTA_GB * GB_IN_BYTES)


@pytest.mark.asyncio
async def test_check_uses_user_override_when_set() -> None:
    """A user with a 50 GB override can upload above the 5 GB workspace
    default without tripping the gate."""
    sid = uuid.uuid4()
    db = _StubDB(
        settings={
            KEY_DEFAULT_QUOTA_GB: 5,
            f"{KEY_USER_QUOTA_PREFIX}{sid}": 50,
        },
        usage_bytes=10 * GB_IN_BYTES,
    )
    usage = await check_storage_quota(db, subject_id=sid, additional_bytes=1 * GB_IN_BYTES)
    assert usage.bytes_quota == 50 * GB_IN_BYTES
    assert usage.is_workspace_default is False
