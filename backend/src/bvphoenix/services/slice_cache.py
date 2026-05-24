"""LRU disk cache for rendered series slices (Sprint 5b).

Cache key components mirror the request shape: ``(series_id,
plane, idx, ww_delta, wc_delta, max_side, content_hash)``. The
``content_hash`` is a SHA-256 of the ordered S3 keys of the source
instances; bumping a single instance's S3 key (re-ingestion,
de-id pass) invalidates the cache without an explicit purge.

Layout::

    <cache_root>/
        index.jsonl   # one row per entry: ``{key, path, size, ts}``
        slices/
            <hash>.jpg

Eviction: when the total bytes-on-disk crosses
``BVP_SLICE_CACHE_BYTES_CAP`` (default 10 GB), the least-recently-used
entries are deleted until usage drops below 80% of the cap. The index
file is read at startup, kept in memory as a dict, and rewritten
whenever an entry is inserted or evicted (atomic via tmp + rename).

Thread-safety: the cache uses an ``asyncio.Lock`` so concurrent
requests never race the index. The lock is process-local — multi
worker scenarios share the disk but each worker has its own LRU
counters; a future iteration can move the index to Redis if the
process count grows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from bvphoenix.config import get_settings


@dataclass(slots=True)
class _Entry:
    key: str
    path: Path
    size: int
    ts: float


class SliceDiskCache:
    """Disk-backed LRU cache for windowed JPEG slices."""

    def __init__(self, *, root: Path | None = None, cap_bytes: int | None = None) -> None:
        settings = get_settings()
        cfg_root = getattr(settings, "slice_cache_root", "") or "/tmp/bvp-slice-cache"
        cfg_cap = int(getattr(settings, "slice_cache_bytes_cap", 0)) or 10 * 1024**3
        self._root = root or Path(cfg_root)
        self._cap = cap_bytes if cap_bytes is not None else cfg_cap
        self._slices_dir = self._root / "slices"
        self._index_path = self._root / "index.jsonl"
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    @staticmethod
    def make_key(
        *,
        series_id: str,
        plane: str,
        idx: int,
        wc_delta: float,
        ww_delta: float,
        max_side: int,
        content_hash: str,
    ) -> str:
        raw = f"{series_id}|{plane}|{idx}|{wc_delta:.4f}|{ww_delta:.4f}|{max_side}|{content_hash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            self._slices_dir.mkdir(parents=True, exist_ok=True)
            if self._index_path.exists():
                with self._index_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        key = obj["key"]
                        path = self._slices_dir / f"{key}.jpg"
                        if not path.exists():
                            continue
                        self._entries[key] = _Entry(
                            key=key,
                            path=path,
                            size=int(obj.get("size", path.stat().st_size)),
                            ts=float(obj.get("ts", time.time())),
                        )
            self._loaded = True

    def _rewrite_index(self) -> None:
        tmp = self._index_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for entry in self._entries.values():
                f.write(
                    json.dumps(
                        {
                            "key": entry.key,
                            "size": entry.size,
                            "ts": entry.ts,
                        }
                    )
                    + "\n"
                )
        os.replace(tmp, self._index_path)

    def _bytes_on_disk(self) -> int:
        return sum(e.size for e in self._entries.values())

    def _evict_until_under(self, target_bytes: int) -> int:
        """Drop LRU entries until total bytes drop below ``target_bytes``.

        Returns the number of entries dropped.
        """
        if self._bytes_on_disk() <= target_bytes:
            return 0
        by_age = sorted(self._entries.values(), key=lambda e: e.ts)
        dropped = 0
        for entry in by_age:
            if self._bytes_on_disk() <= target_bytes:
                break
            import contextlib as _ctx

            with _ctx.suppress(FileNotFoundError):
                entry.path.unlink()
            self._entries.pop(entry.key, None)
            dropped += 1
        return dropped

    async def get(self, key: str) -> bytes | None:
        await self._ensure_loaded()
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or not entry.path.exists():
                if entry is not None:
                    self._entries.pop(key, None)
                return None
            entry.ts = time.time()
            try:
                data = entry.path.read_bytes()
            except FileNotFoundError:
                self._entries.pop(key, None)
                return None
            return data

    async def put(self, key: str, data: bytes) -> Path:
        await self._ensure_loaded()
        async with self._lock:
            self._slices_dir.mkdir(parents=True, exist_ok=True)
            path = self._slices_dir / f"{key}.jpg"
            path.write_bytes(data)
            self._entries[key] = _Entry(
                key=key,
                path=path,
                size=len(data),
                ts=time.time(),
            )
            # Eviction kick-in at 80% of the cap.
            if self._bytes_on_disk() > self._cap:
                self._evict_until_under(int(self._cap * 0.8))
            self._rewrite_index()
            return path

    @property
    def cap_bytes(self) -> int:
        return self._cap

    @property
    def entries(self) -> int:
        return len(self._entries)


_SHARED: SliceDiskCache | None = None


def get_slice_cache() -> SliceDiskCache:
    global _SHARED
    if _SHARED is None:
        _SHARED = SliceDiskCache()
    return _SHARED


__all__ = ["SliceDiskCache", "get_slice_cache"]
