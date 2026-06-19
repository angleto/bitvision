"""Hand freed heap pages back to the OS after large transient allocations.

The backend unpacks whole DICOM volumes (100-500 MB float32 arrays) per
ROI / wash-out request. glibc keeps the freed pages on its per-arena free
lists instead of returning them to the kernel, so the resident set climbs
request-after-request and never falls back to baseline. Measured in prod
(2026-06-19): a fresh backend pod idles at ~200 MB, but a pod that has
served viewer traffic sits at ~1.6 GB against the 2 GB limit, and the next
volume unpack OOMKills it (exit 137) — surfacing to the client as 502s.

Two levers, used together:

* ``MALLOC_ARENA_MAX=2`` (set in infra/dockerfiles/backend.Dockerfile)
  bounds the number of arenas, so freed chunks pile up in 2 pools instead
  of one-per-thread.
* ``malloc_trim(0)`` (here) actively returns the free top-of-heap pages to
  the kernel after we drop the references to a packed volume.

``release_memory()`` is a no-op on non-glibc platforms (macOS dev), so it
is safe to call unconditionally.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc

_libc: ctypes.CDLL | None = None
_loaded = False
_have_malloc_trim = False


def _ensure_loaded() -> None:
    global _libc, _loaded, _have_malloc_trim
    if _loaded:
        return
    _loaded = True
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=False)
        _have_malloc_trim = hasattr(_libc, "malloc_trim")
    except OSError:
        _libc = None
        _have_malloc_trim = False


def release_memory(*, collect: bool = True) -> None:
    """Free Python garbage and return glibc's freed heap pages to the OS.

    Call right after dropping the last reference to a packed volume or a
    large numpy array. Cheap (sub-millisecond on a trimmed heap) and safe
    to call on every request. ``collect`` runs ``gc.collect()`` first so
    arrays held only by reference cycles are actually freed before the
    trim; pass ``collect=False`` in hot inner loops where the caller has
    already ``del``'d every reference.
    """
    if collect:
        gc.collect()
    _ensure_loaded()
    if _have_malloc_trim and _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception:
            # Best-effort: a failed trim must never break the request.
            pass
