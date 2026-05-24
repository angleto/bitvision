"""Studies REST surface, packaged from the monolithic api/studies.py."""

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.studies import bulk as _section_bulk
from bvphoenix.api.studies import core as _section_core
from bvphoenix.api.studies import metadata as _section_metadata
from bvphoenix.api.studies import pet as _section_pet
from bvphoenix.api.studies import registrations as _section_registrations
from bvphoenix.api.studies import roi_stats as _section_roi_stats
from bvphoenix.api.studies import segmentations as _section_segmentations
from bvphoenix.api.studies._shared import *  # noqa: F403

# Re-export route-handler symbols (and any private helpers
# the original monolithic file exposed) for legacy callers that
# imported them directly from bvphoenix.api.<pkg>.
from bvphoenix.api.studies.bulk import *  # noqa: F403
from bvphoenix.api.studies.core import *  # noqa: F403
from bvphoenix.api.studies.metadata import *  # noqa: F403
from bvphoenix.api.studies.pet import *  # noqa: F403
from bvphoenix.api.studies.registrations import *  # noqa: F403
from bvphoenix.api.studies.roi_stats import *  # noqa: F403
from bvphoenix.api.studies.segmentations import *  # noqa: F403

router = APIRouter()
router.include_router(_section_core.router)
router.include_router(_section_metadata.router)
router.include_router(_section_pet.router)
router.include_router(_section_segmentations.router)
router.include_router(_section_registrations.router)
router.include_router(_section_roi_stats.router)
router.include_router(_section_bulk.router)

__all__ = ["router"]
