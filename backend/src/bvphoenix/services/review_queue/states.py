"""Review-queue state machine — explicit transitions, no jumps.

Same pattern as the report-content lifecycle
(``api/report_contents.py`` ``_LIGHT_TRANSITIONS`` /
``_HEAVY_TRANSITIONS``): the full transition table is a module-level
dict of explicit ``from -> {to}`` edges, and every status change goes
through :func:`validate_transition`. There is deliberately no
"force" escape hatch — an unforeseen path means the table (and the
contract with both consumers) must be amended, not bypassed.

Lifecycle::

    received -> processing -> needs_review -+-> accepted -> promoting -> promoted
                     ^   \\-> blocked  ------+-> rejected
                     |________|  (re-run after a fix)

``expired`` (retention elapsed before a decision) and ``failed``
(promotion hook gave up after retries) are terminal alongside
``promoted`` and ``rejected``. ``blocked`` is the hard-fail outcome of
the auto-checks: a blocked item can be re-processed (after the
offending component is fixed upstream) or rejected, but never accepted
as-is — the missing ``blocked -> accepted`` edge IS the gate.
"""

from __future__ import annotations

REVIEW_STATUSES: tuple[str, ...] = (
    "received",
    "processing",
    "needs_review",
    "blocked",
    "accepted",
    "promoting",
    "promoted",
    "rejected",
    "expired",
    "failed",
)

REVIEW_TERMINAL_STATUSES: frozenset[str] = frozenset({"promoted", "rejected", "expired", "failed"})

REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"processing", "expired", "failed"}),
    "processing": frozenset({"needs_review", "blocked", "expired", "failed"}),
    # ``needs_review -> processing`` is the idempotent re-run path: the
    # auto-checks can be executed again (e.g. after a ClamAV signature
    # update) without inventing a parallel "recheck" state.
    "needs_review": frozenset({"processing", "accepted", "rejected", "expired"}),
    "blocked": frozenset({"processing", "rejected", "expired"}),
    "accepted": frozenset({"promoting"}),
    "promoting": frozenset({"promoted", "failed"}),
    "promoted": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
    "failed": frozenset(),
}


class ReviewTransitionError(ValueError):
    """Raised on an inadmissible status transition.

    Carries the offending edge so API layers can map it onto a 409
    with a structured detail instead of re-parsing the message.
    """

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        admissible = sorted(REVIEW_TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"cannot transition review item from {current!r} to {requested!r}; "
            f"admissible: {admissible or 'none (terminal)'}"
        )


def validate_transition(current: str, requested: str) -> None:
    """Raise :class:`ReviewTransitionError` unless ``current -> requested``
    is an explicit edge of :data:`REVIEW_TRANSITIONS`."""
    if current not in REVIEW_TRANSITIONS:
        raise ReviewTransitionError(current, requested)
    if requested not in REVIEW_TRANSITIONS[current]:
        raise ReviewTransitionError(current, requested)


__all__ = [
    "REVIEW_STATUSES",
    "REVIEW_TERMINAL_STATUSES",
    "REVIEW_TRANSITIONS",
    "ReviewTransitionError",
    "validate_transition",
]
