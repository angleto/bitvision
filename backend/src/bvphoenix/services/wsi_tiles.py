"""On-the-fly Deep Zoom tiling for pathology whole-slide images.

The gigapixel WSI viewer. OpenSeadragon in the browser requests a DZI
descriptor plus a pyramid of 256 px JPEG tiles; this module produces them
from the source slide on demand.

Why on-the-fly and not pre-generated to S3
------------------------------------------
A single 40x slide at 256 px tiles has O((100000/256)^2) ≈ 150k tiles on
the base level alone; pre-generating full pyramids for thousands of public
slides would mean hundreds of millions of tiny S3 objects — operationally
unworkable and expensive. Instead we open the slide with OpenSlide and
serve tiles via ``openslide.deepzoom.DeepZoomGenerator`` per request. The
only cost moved to request time is the first tile of a slide: the source
file must be local for OpenSlide to memory-map it, so a small bounded LRU
cache downloads it from S3 once and reuses the open handle for every
subsequent tile.

Storage isolation holds: tiles stream back through the API; the S3 bucket
and source key never cross the backend boundary. The cache directory is
ephemeral scratch (bounded by ``BVP_WSI_TILE_CACHE_SLIDES`` open slides),
not durable state — a cold pod simply re-downloads on the next request.

Thread-safety: libopenslide ``read_region`` is safe to call concurrently
from multiple threads on the same handle, so tile reads run lock-free; the
global lock guards only the LRU dict mutation.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import openslide
from openslide.deepzoom import DeepZoomGenerator

from bvphoenix.db.models import PathologySlide
from bvphoenix.storage import get_s3_storage

# OpenSeadragon's default tile size is 256; OpenSlide DeepZoom builds that
# from a 254 px content tile + 1 px overlap on each interior edge.
_TILE_SIZE = 254
_TILE_OVERLAP = 1
_TILE_QUALITY = 80

_CACHE_MAX_SLIDES = max(1, int(os.environ.get("BVP_WSI_TILE_CACHE_SLIDES", "4")))
_CACHE_DIR = Path(
    os.environ.get("BVP_WSI_TILE_CACHE_DIR", "")
    or (Path(os.environ.get("TMPDIR", "/tmp")) / "bvphoenix-wsi-cache")
)


@dataclass
class _Entry:
    osr: openslide.OpenSlide
    dz: DeepZoomGenerator
    path: Path


_cache: OrderedDict[str, _Entry] = OrderedDict()
_cache_lock = threading.Lock()


def _source_ext(slide: PathologySlide) -> str:
    suffix = Path(slide.s3_source_key or "").suffix.lower()
    return suffix or ".bin"


def _download_source(slide: PathologySlide, dest: Path) -> None:
    """Stream the source slide from S3 to ``dest`` (keeps RSS bounded)."""
    storage = get_s3_storage()
    iterator, _length, _ctype = storage.iter_object(bucket=slide.s3_bucket, key=slide.s3_source_key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as fh:
        for chunk in iterator:
            fh.write(chunk)
    tmp.replace(dest)


def _close_entry(entry: _Entry) -> None:
    try:
        entry.osr.close()
    except Exception:
        pass
    try:
        entry.path.unlink(missing_ok=True)
    except Exception:
        pass


def _load_entry(slide: PathologySlide) -> _Entry:
    path = _CACHE_DIR / f"{slide.id}{_source_ext(slide)}"
    if not path.exists():
        _download_source(slide, path)
    osr = openslide.OpenSlide(str(path))
    dz = DeepZoomGenerator(osr, tile_size=_TILE_SIZE, overlap=_TILE_OVERLAP, limit_bounds=True)
    return _Entry(osr=osr, dz=dz, path=path)


def _get_entry(slide: PathologySlide) -> _Entry:
    sid = str(slide.id)
    with _cache_lock:
        ent = _cache.get(sid)
        if ent is not None:
            _cache.move_to_end(sid)
            return ent
    # Load outside the global lock — the S3 download is slow and would
    # otherwise serialise every other slide's tile requests.
    loaded = _load_entry(slide)
    with _cache_lock:
        existing = _cache.get(sid)
        if existing is not None:
            # Another request loaded it concurrently; drop our duplicate.
            _close_entry(loaded)
            _cache.move_to_end(sid)
            return existing
        _cache[sid] = loaded
        while len(_cache) > _CACHE_MAX_SLIDES:
            _evicted_id, evicted = _cache.popitem(last=False)
            _close_entry(evicted)
    return loaded


def get_dzi_xml(slide: PathologySlide) -> str:
    """Return the Deep Zoom Image (.dzi) XML descriptor for ``slide``."""
    entry = _get_entry(slide)
    return entry.dz.get_dzi("jpeg")


def get_tile_jpeg(slide: PathologySlide, *, level: int, col: int, row: int) -> bytes:
    """Return one JPEG tile, or raise ``KeyError`` for an out-of-range address."""
    entry = _get_entry(slide)
    if level < 0 or level >= entry.dz.level_count:
        raise KeyError(f"level {level} out of range (0..{entry.dz.level_count - 1})")
    try:
        tile = entry.dz.get_tile(level, (col, row))
    except (ValueError, IndexError) as exc:
        raise KeyError(f"tile {level}/{col}_{row} out of range") from exc
    if tile.mode != "RGB":
        tile = tile.convert("RGB")
    buf = BytesIO()
    tile.save(buf, format="JPEG", quality=_TILE_QUALITY)
    return buf.getvalue()
