"""Document study-link kind vocabulary + legacy translation shim.

Migration 0089 renamed ``link_kind = 'report_of'`` to
``'primary_report'`` and added ``'addendum'`` / ``'second_opinion'`` to
the enum. The DB check constraint accepts only the new vocabulary.

To keep MCP clients on the legacy enum working for one release, this
module exposes:

* ``CANONICAL_KINDS`` — the new enum values (what the DB stores).
* ``LEGACY_TO_CANONICAL`` — translation table consumed by route /
  tool handlers. Today the only entry is ``report_of`` →
  ``primary_report``; once the deprecation window ends the table can
  shrink and the shim becomes a no-op.
* ``coerce_link_kind(value)`` — central translator. Logs a deprecation
  warning when a legacy alias is rewritten so call-site removal is
  tracked.

The shim must NOT be used to gate writes: validation of the resulting
canonical value belongs to the route, which rejects unknown values
explicitly. The shim only widens the *input* surface, never the
storage surface.
"""

from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger(__name__)

# Aligned with the v0089 check constraint
# ``ck_document_study_links_kind``. Keep in sync if a future migration
# expands the enum.
CANONICAL_KINDS: Final[tuple[str, ...]] = (
    "primary_report",
    "addendum",
    "second_opinion",
    "extracted_from",
    "cites",
    "mentions",
)

# Legacy aliases supported for one release after 0089. The values map
# 1-to-1 onto the canonical vocabulary; the shim simply rewrites at
# request boundary.
LEGACY_TO_CANONICAL: Final[dict[str, str]] = {
    "report_of": "primary_report",
}


def coerce_link_kind(value: str) -> str:
    """Translate a legacy ``link_kind`` to its canonical equivalent.

    A canonical value passes through unchanged; an unknown value is
    returned unchanged so the route's strict validator can reject it
    with a clear error. The shim emits a deprecation warning for each
    legacy hit so call sites still using the old vocabulary are
    visible in the logs.
    """
    if value in CANONICAL_KINDS:
        return value
    canonical = LEGACY_TO_CANONICAL.get(value)
    if canonical is None:
        return value
    logger.warning(
        "document_study_link.link_kind legacy alias %r used; rewriting to %r. "
        "Update the caller to send the canonical value before the next release.",
        value,
        canonical,
    )
    return canonical


__all__ = ["CANONICAL_KINDS", "LEGACY_TO_CANONICAL", "coerce_link_kind"]
