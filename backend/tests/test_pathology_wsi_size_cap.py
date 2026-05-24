"""WSI file-size cap regression test.

OpenSlide memory-maps the input file at ``open()`` time. A 50 GiB
SVS handed to ``import_pathology_slide`` without a pre-check used to
OOM the worker before any application-level validation ran. The cap
sits at the top of the import path and rejects oversized inputs with
a ``ValueError`` carrying the actual / allowed sizes.

These tests assert the cap fires by stubbing only the ``stat`` call —
no actual giant file is created on disk.
"""

from __future__ import annotations

import uuid

import pytest

from bvphoenix.config import get_settings
from bvphoenix.services.pathology_import import PathologyImportSource, import_pathology_slide


class _FakePath:
    """Stand-in for pathlib.Path that lies about its size + existence
    without touching the filesystem. Only the attributes the cap
    consults are implemented."""

    def __init__(self, *, size: int, suffix: str = ".svs") -> None:
        self._size = size
        self.suffix = suffix

    def exists(self) -> bool:
        return True

    def stat(self) -> object:
        size = self._size

        class _Stat:
            st_size = size

        return _Stat()


def test_wsi_cap_rejects_oversized_file() -> None:
    settings = get_settings()
    huge = settings.wsi_max_bytes + 1
    src = PathologyImportSource(
        path=_FakePath(size=huge),  # type: ignore[arg-type]
        owner_subject_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
    )

    with pytest.raises(ValueError, match="too large"):
        import_pathology_slide(
            session=None,  # type: ignore[arg-type]
            storage=None,  # type: ignore[arg-type]
            bucket="ignored",
            source=src,
        )


def test_wsi_cap_setting_has_reasonable_default() -> None:
    s = get_settings()
    # 30 GiB. Generous enough for a 100k×100k 40x SVS (~5-15 GiB);
    # tight enough that a stray 100 GiB upload is rejected.
    assert s.wsi_max_bytes >= 5 * 1024**3
    assert s.wsi_max_bytes <= 200 * 1024**3
