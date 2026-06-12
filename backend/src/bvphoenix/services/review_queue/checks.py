"""Auto-check plugin contract + ordered runner with verdict aggregation.

A :class:`ReviewCheck` inspects a staged item (its component manifest +
lazily-readable blobs) and returns a :class:`CheckResult`. The runner
executes the profile's checks **in declared order**, aggregates the
per-check verdicts into the item's ``auto_checks`` JSONB (keyed by
check name, so a re-run overwrites in place — idempotent by
construction) and computes the worst-of ``auto_verdict``.

Verdict semantics, weakest to strongest:

* ``pass``  — nothing to report (also used by pure routing signals).
* ``warn``  — informational finding (e.g. duplicate of a known blob).
* ``fail``  — content the profile considers unacceptable, but a human
  reviewer can still overrule by accepting or rejecting.
* ``block`` — hard fail (malware, traversal-laced archive): the item
  transitions to ``blocked`` and *cannot* be accepted as-is (the state
  machine has no ``blocked -> accepted`` edge).
* ``error`` — the check itself could not run (scanner unreachable).
  Aggregates as strongly as ``fail`` — an unscanned item is never
  treated as clean — but does not block: the run can be repeated.

Common plugins live in :mod:`~bvphoenix.services.review_queue.plugins`;
profile-specific ones (SPF/DKIM sender verify, PS3.15 de-id, CSAM
screening, ...) live with their consumer.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

CHECK_VERDICTS: tuple[str, ...] = ("pass", "warn", "fail", "block", "error")

# Aggregation order. ``error`` outranks ``fail`` so a scanner outage is
# visible in ``auto_verdict`` even when another check already failed;
# ``block`` stays the only verdict that flips the state machine edge.
_VERDICT_RANK: dict[str, int] = {"pass": 0, "warn": 1, "fail": 2, "error": 3, "block": 4}


@dataclass(frozen=True, slots=True)
class StagedComponent:
    """One file/part of a staged item (an email attachment, an uploaded
    file, ...). ``read`` is an async thunk so checks that only look at
    the manifest never pay the blob fetch, and large components are
    streamed out of the store at most once per check."""

    name: str
    size_bytes: int
    content_type: str | None
    read: Callable[[], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class StagedItem:
    """Store-agnostic view of a staged item handed to the checks.

    Built by the profile's ``load_staged`` accessor; ``manifest`` is the
    profile-specific envelope (email headers, declared licence, ...) —
    common plugins must not assume any particular shape beyond it being
    JSON-serialisable.
    """

    item_id: uuid.UUID
    components: Sequence[StagedComponent]
    manifest: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckContext:
    """Everything a check may need. ``db`` is the session of the
    surrounding engine transaction — checks that query consumer tables
    (dedup) use it; pure-content checks ignore it."""

    db: AsyncSession
    staged: StagedItem


@dataclass(frozen=True, slots=True)
class CheckResult:
    verdict: str
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in CHECK_VERDICTS:
            raise ValueError(f"unknown check verdict {self.verdict!r}")


@runtime_checkable
class ReviewCheck(Protocol):
    """A named auto-check. Implementations must be safe to re-run on the
    same item (idempotent): results are keyed by ``name`` and a re-run
    replaces the previous entry."""

    name: str

    async def run(self, ctx: CheckContext) -> CheckResult: ...


def aggregate_verdicts(verdicts: Sequence[str]) -> str:
    """Worst-of aggregation over :data:`CHECK_VERDICTS`."""
    if not verdicts:
        return "pass"
    return max(verdicts, key=lambda v: _VERDICT_RANK[v])


async def run_checks(
    ctx: CheckContext,
    checks: Sequence[ReviewCheck],
    *,
    previous: dict | None = None,
) -> tuple[dict, str]:
    """Execute ``checks`` in order; return ``(auto_checks, auto_verdict)``.

    ``previous`` is the item's current ``auto_checks`` so entries from
    checks no longer in the profile are preserved (visible history) while
    re-run checks overwrite their own slot. A check that raises is
    recorded as ``error`` with the exception text — the engine still
    completes the pass, and a later re-run can heal it.
    """
    names = [c.name for c in checks]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate check names in profile: {names}")

    merged: dict = dict((previous or {}).get("checks", {}))
    for check in checks:
        started = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            result = await check.run(ctx)
        except Exception as exc:
            result = CheckResult(verdict="error", details={"exception": str(exc)})
        merged[check.name] = {
            "verdict": result.verdict,
            "details": result.details,
            "ran_at": started,
        }

    verdict = aggregate_verdicts([entry["verdict"] for entry in merged.values()])
    return {"version": 1, "checks": merged}, verdict


__all__ = [
    "CHECK_VERDICTS",
    "CheckContext",
    "CheckResult",
    "ReviewCheck",
    "StagedComponent",
    "StagedItem",
    "aggregate_verdicts",
    "run_checks",
]
