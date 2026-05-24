"""Unit tests for the slice LRU disk cache (Sprint 5b)."""

from __future__ import annotations

import pytest

from bvphoenix.services.slice_cache import SliceDiskCache


def test_make_key_is_deterministic(tmp_path) -> None:
    cache = SliceDiskCache(root=tmp_path, cap_bytes=1024 * 1024)
    a = cache.make_key(
        series_id="s1",
        plane="axial",
        idx=10,
        wc_delta=0,
        ww_delta=0,
        max_side=512,
        content_hash="abc",
    )
    b = cache.make_key(
        series_id="s1",
        plane="axial",
        idx=10,
        wc_delta=0,
        ww_delta=0,
        max_side=512,
        content_hash="abc",
    )
    assert a == b
    other = cache.make_key(
        series_id="s1",
        plane="coronal",  # plane differs
        idx=10,
        wc_delta=0,
        ww_delta=0,
        max_side=512,
        content_hash="abc",
    )
    assert other != a


@pytest.mark.asyncio
async def test_put_then_get_roundtrip(tmp_path) -> None:
    cache = SliceDiskCache(root=tmp_path, cap_bytes=1024 * 1024)
    key = cache.make_key(
        series_id="s1",
        plane="axial",
        idx=0,
        wc_delta=0,
        ww_delta=0,
        max_side=512,
        content_hash="abc",
    )
    payload = b"\xff\xd8\xff" + b"x" * 1024  # fake JPEG
    await cache.put(key, payload)
    assert await cache.get(key) == payload
    # The on-disk entry survives a second cache instance reading the
    # index file.
    fresh = SliceDiskCache(root=tmp_path, cap_bytes=1024 * 1024)
    assert await fresh.get(key) == payload


@pytest.mark.asyncio
async def test_eviction_drops_lru_when_over_cap(tmp_path) -> None:
    # Tight cap: 4 KB. After we put 5x 1 KB blobs, the oldest must
    # have been evicted to keep usage under 80% of the cap.
    cache = SliceDiskCache(root=tmp_path, cap_bytes=4 * 1024)
    keys = []
    for i in range(5):
        key = cache.make_key(
            series_id="s1",
            plane="axial",
            idx=i,
            wc_delta=0,
            ww_delta=0,
            max_side=512,
            content_hash="abc",
        )
        await cache.put(key, b"x" * 1024)
        keys.append(key)
    # 5 KB written, cap 4 KB → at least one eviction.
    assert cache.entries < 5
    # Most recent must still be present.
    assert await cache.get(keys[-1]) == b"x" * 1024
