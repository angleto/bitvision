"""Build-info endpoint.

``GET /api/version`` returns the running build's version, git SHA and
build date. Public (no auth) by design: it's the same information the
frontend renders on ``/settings`` and the values are also embedded in
the container image label set, so there's nothing to hide. The values
are baked into the image at build time via Docker ``--build-arg``;
outside CI they default to empty strings and the response reports
``version='dev'`` so a local dev instance is unambiguously not a
released build.
"""

from __future__ import annotations

import platform
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from bvphoenix.config import Settings, get_settings

router = APIRouter(tags=["infra"])


class VersionOut(BaseModel):
    """Wire shape for ``GET /api/version``."""

    service: str
    version: str
    git_sha: str
    git_sha_short: str
    build_date: str
    python_version: str


@router.get("/version", response_model=VersionOut)
def get_version(settings: Annotated[Settings, Depends(get_settings)]) -> VersionOut:
    sha = settings.app_git_sha or ""
    return VersionOut(
        service="bitvision-phoenix-backend",
        version=settings.app_version or "dev",
        git_sha=sha,
        git_sha_short=sha[:7] if sha else "",
        build_date=settings.app_build_date or "",
        python_version=platform.python_version(),
    )
