"""Patient REST surface, packaged from the monolithic api/patients.py.

Split on 2026-05-21: the original 5818-LOC file lives on as a
package, one child module per // ---- Section ---- in the original.
_shared.py holds the imports, Pydantic schemas and helpers used
across the children; each child defines its own APIRouter and
registers its slice of the 42 endpoints. __init__.py re-exposes
a single canonical ``router`` so main.py keeps a one-line wiring.
"""

from __future__ import annotations

from fastapi import APIRouter

from bvphoenix.api.patients import contacts as _section_contacts
from bvphoenix.api.patients import core as _section_core
from bvphoenix.api.patients import documents as _section_documents
from bvphoenix.api.patients import fascicolo as _section_fascicolo
from bvphoenix.api.patients import publish as _section_publish
from bvphoenix.api.patients import search as _section_search
from bvphoenix.api.patients import sharing as _section_sharing

# Re-export every symbol that callers used to ``from
# bvphoenix.api.patients import X``. The legacy entry points (
# PatientOut, _get_patient_or_404, _document_versioning_payload,
# the inline Pydantic schemas) live in _shared.
from bvphoenix.api.patients._shared import *  # noqa: F403

# Re-export route-handler symbols (and any private helpers
# the original monolithic file exposed) for legacy callers that
# imported them directly from bvphoenix.api.<pkg>.
from bvphoenix.api.patients.contacts import *  # noqa: F403
from bvphoenix.api.patients.core import *  # noqa: F403
from bvphoenix.api.patients.documents import *  # noqa: F403
from bvphoenix.api.patients.fascicolo import *  # noqa: F403
from bvphoenix.api.patients.publish import *  # noqa: F403
from bvphoenix.api.patients.search import *  # noqa: F403
from bvphoenix.api.patients.sharing import *  # noqa: F403

router = APIRouter()
router.include_router(_section_core.router)
router.include_router(_section_contacts.router)
router.include_router(_section_fascicolo.router)
router.include_router(_section_documents.router)
router.include_router(_section_sharing.router)
router.include_router(_section_search.router)
router.include_router(_section_publish.router)

__all__ = ["router"]
