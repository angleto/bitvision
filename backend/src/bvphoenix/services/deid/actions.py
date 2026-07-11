"""Execute the resolved PS3.15 action map over a dataset, in place.

Per element: private tags are removed (unless safe-listed); the full-table
tag→action map wins; then the repeating-group mask rules (curve data 50xx,
overlay data/comments 60xx — these have NO pydicom keyword at runtime, which is
exactly why matching is by tag, not keyword); otherwise a VR-based default
applies (UID-VR → remap except structural UIDs, date/time-VR → shift, un-tabled
PN → remove, free-narrative text → clean, everything else → keep). Sequences
are recursed so nested PHI (e.g. RequestAttributesSequence) is scrubbed
identically. Any element that throws on transform is removed rather than left
with its original value.
"""

from __future__ import annotations

from pydicom.dataset import Dataset

from bvphoenix.services.deid import safe_private
from bvphoenix.services.deid.operators import DeidOperators
from bvphoenix.services.deid.profile_table import (
    KEEP_UID_KEYWORDS,
    Action,
    ProfileOptions,
    ResolvedActions,
)

_DATE_VRS = frozenset({"DA", "DT", "TM"})
# Free-narrative text VRs: an un-tabled one may carry incidental PHI, so the
# protective default is to CLEAN it (regex/name scrub), not keep it. Short-string
# VRs (LO/SH) are left KEEP — they overwhelmingly carry codes/labels/protocol
# names, and the identifying ones are covered explicitly by the table.
_FREETEXT_CLEAN_VRS = frozenset({"ST", "LT", "UT"})


def scrub_dataset(
    ds: Dataset,
    *,
    actions: ResolvedActions,
    operators: DeidOperators,
    options: ProfileOptions,
) -> None:
    for elem in list(ds):
        tag = elem.tag

        # Private (odd-group) elements — incl. their PrivateCreator — are
        # removed unless the Retain Safe Private Option keeps this exact one.
        if tag.is_private:
            keep = options.retain_safe_private and safe_private.is_safe_private(
                elem, ds, version=options.safe_private_version
            )
            if not keep:
                del ds[tag]
                continue
            if elem.VR == "SQ" and elem.value is not None:
                for item in elem.value:
                    scrub_dataset(item, actions=actions, operators=operators, options=options)
            continue

        # Deprecated group-length elements (gggg,0000) carry no value of use.
        if tag.element == 0x0000:
            del ds[tag]
            continue

        action = actions.by_tag.get(int(tag))
        if action is None:
            action = actions.repeater_action(tag.group, tag.element)
        if action is None:
            action = _vr_default(elem.VR, elem.keyword or "")

        if action == Action.REMOVE:
            del ds[tag]
            continue

        if elem.VR == "SQ":
            # Kept sequence → recurse so nested identifiers are scrubbed too.
            if elem.value is not None:
                for item in elem.value:
                    scrub_dataset(item, actions=actions, operators=operators, options=options)
            continue

        try:
            _apply_scalar(elem, action, operators)
        except Exception:
            # Fail-safe: an element that can't take its transform is dropped,
            # never left with the original (possibly identifying) value.
            del ds[tag]


def _vr_default(vr: str, kw: str) -> Action:
    if vr == "UI":
        return Action.KEEP if kw in KEEP_UID_KEYWORDS else Action.UID
    if vr in _DATE_VRS:
        return Action.DATE
    if vr == "PN":
        # Any un-tabled person name is identifying (operator, consulting
        # physician, observer, ...). The table covers the known PN tags; remove
        # anything else rather than keep it.
        return Action.REMOVE
    if vr in _FREETEXT_CLEAN_VRS:
        return Action.CLEAN
    return Action.KEEP


def _apply_scalar(elem, action: Action, operators: DeidOperators) -> None:
    if action == Action.KEEP:
        return
    if action == Action.EMPTY:
        elem.value = ""
    elif action == Action.PSEUDONYM:
        elem.value = operators.pseudonym(elem.value)
    elif action == Action.UID:
        elem.value = operators.remap_uid(elem.value)
    elif action == Action.DATE:
        elem.value = operators.shift_date(elem.value, elem.VR)
    elif action == Action.CLEAN:
        elem.value = operators.clean_text(elem.value)
    elif action == Action.AGE:
        elem.value = operators.cap_age(elem.value)


__all__ = ["scrub_dataset"]
