"""Google Drive-style access levels mapped to permission verbs.

Each level is a named bundle of permissions. Download is a separate
toggle that can be added to any level — a Viewer with download can
see and download, a Viewer without download can only see in the
browser.

Usage:
    perms = level_to_permissions("editor", download=True)
    level = permissions_to_level(perms)
"""

from __future__ import annotations

VIEWER_PERMS = frozenset({"read:metadata", "read:pixels", "read:annotations"})

EDITOR_PERMS = VIEWER_PERMS | frozenset({"write:annotations", "write:report", "run:llm"})

MANAGER_PERMS = EDITOR_PERMS | frozenset({"share", "share:delegate", "transfer:ownership"})

DOWNLOAD_PERMS = frozenset({"download:dicom", "download:derivative"})

LEVELS: dict[str, frozenset[str]] = {
    "viewer": VIEWER_PERMS,
    "editor": EDITOR_PERMS,
    "manager": MANAGER_PERMS,
}


def level_to_permissions(level: str, *, download: bool = False) -> list[str]:
    """Convert an access level name to a list of permission verbs."""
    base = LEVELS.get(level, VIEWER_PERMS)
    if download:
        base = base | DOWNLOAD_PERMS
    return sorted(base)


def permissions_to_level(permissions: list[str] | frozenset[str]) -> str:
    """Determine the effective access level from a set of permission verbs."""
    pset = frozenset(permissions)
    if pset >= MANAGER_PERMS:
        return "manager"
    if pset >= EDITOR_PERMS:
        return "editor"
    if pset >= VIEWER_PERMS:
        return "viewer"
    return "custom"


def has_download(permissions: list[str] | frozenset[str]) -> bool:
    return frozenset(permissions) >= DOWNLOAD_PERMS
