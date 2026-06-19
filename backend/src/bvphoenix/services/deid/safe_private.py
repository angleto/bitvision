"""Safe-private allowlist for the PS3.15 Retain Safe Private Option (E.3.10).

Default policy: NO private (odd-group) element is retained — the executor
removes them all. When the Retain Safe Private Option is enabled, elements whose
``(PrivateCreator, group, element-byte)`` is on the curated allowlist are kept
(vendor tags vetted to carry geometry / reconstruction parameters, never PHI).

The allowlist is versioned and starts EMPTY in v1, so enabling the option
retains nothing until vetted entries are added — fail-safe by construction.
"""

from __future__ import annotations

import pydicom
from pydicom.tag import Tag

# version -> {(private_creator, group, element_low_byte)}.
# Empty in v1: enabling Retain Safe Private keeps nothing until populated.
_SAFE_PRIVATE: dict[str, frozenset[tuple[str, int, int]]] = {"v1": frozenset()}


def is_safe_private(elem: pydicom.DataElement, ds: pydicom.Dataset, *, version: str) -> bool:
    """Return True iff this private element is on the safe allowlist.

    Resolves the element's PrivateCreator (the (gggg,00xx) block owner) and
    checks ``(creator, group, element_low_byte)`` against the versioned set.
    """
    allow = _SAFE_PRIVATE.get(version, frozenset())
    if not allow:
        return False
    tag = elem.tag
    block = (tag.element & 0xFF00) >> 8
    creator_tag = Tag(tag.group, block)
    creator_el = ds.get(creator_tag)
    creator = str(creator_el.value).strip() if creator_el is not None else ""
    return (creator, tag.group, tag.element & 0x00FF) in allow


__all__ = ["is_safe_private"]
