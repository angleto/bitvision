"""Exceptions for the in-house PS3.15 de-identification engine."""

from __future__ import annotations


class DeidConfigError(RuntimeError):
    """The de-identification engine is misconfigured (e.g. missing salt in prod)."""


class DeidVerificationError(RuntimeError):
    """The post-scrub verification pass found residual PHI / a disallowed tag.

    Raised by ``deid.verify.assert_clean``. Callers on egress paths already
    treat a scrub exception as "withhold this instance", so a verification
    failure fails closed (no PHI served) rather than leaking.
    """


class RequiresReview(RuntimeError):
    """The dataset can't be de-identified with confidence by the header engine
    alone (e.g. an SR / encapsulated document whose free text isn't guaranteed
    scrubbed) and must be routed to human review (the M1 quarantine) instead of
    served. Treated like a withhold on egress paths."""
