"""k-anonymity enforcement for training-license aggregates (F10.2).

Design: before a licensed
dataset is assembled, every quasi-identifier bucket in the included
studies must contain at least ``k_min`` rows. A bucket that does not
meet the threshold is either expanded (more studies of the same
kind) or the whole assembly is rejected. This module enforces the
check; the assembler decides what to do with the failure.

The quasi-identifier tuple used here is ``(modality, body_part)``.
This is **lossy**: a rigorous k-anon pass for medical imaging would
also bucket by age range and sex (and possibly anatomy sub-region).
We keep it narrow for F10.2 because:

* ``deidentify.py`` already blanks Patient Age / Sex / DOB before a
  T3 study is served, so those fields are not reliably available at
  assembly time.
* A ≥5 count on (modality, body_part) is a meaningful signal — it
  means at least five independent exams of the same kind. Richer
  quasi-identifiers are a tightening, not a loosening, of the
  threshold, so extending the bucket tuple later only *removes*
  datasets that slipped through the earlier version. Documented as
  follow-up work.

Shapes
------

The module is orthogonal to the ORM. Callers build a list of
:class:`StudyFingerprint` objects from whatever source (live ImagingStudy
rows, a manifest JSON from a replay, etc.) and the service counts
and asserts. This keeps the test surface small and the logic
trivial to unit-test without a DB.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_K_MIN: int = 5


@dataclass(frozen=True, slots=True)
class StudyFingerprint:
    """Projection of the fields we quasi-identify on.

    The ORM ``ImagingStudy`` row conveniently exposes ``modalities`` (list of
    upper-case codes) and ``study_description`` (free text). The
    assembler is responsible for picking which field on the ImagingStudy
    represents "primary modality" / "body part"; we keep the names
    opaque here so the logic is reusable."""

    modality: str
    body_part: str


class KAnonymityError(RuntimeError):
    """Raised when a bucket falls below ``k_min``.

    Carries the worst offender so the caller can explain which axis
    failed: "modality=CT body_part=lung had only 2 studies, minimum
    is 5"."""

    def __init__(self, *, k_min: int, worst_bucket: tuple[str, str], worst_size: int) -> None:
        super().__init__(
            f"k-anonymity violated: bucket {worst_bucket!r} has size {worst_size} < k_min={k_min}"
        )
        self.k_min = k_min
        self.worst_bucket = worst_bucket
        self.worst_size = worst_size


def _normalise(value: str | None) -> str:
    """Canonical form for bucket keys: lower-case, stripped, empty
    becomes the sentinel ``"unknown"`` so missing data is *not* an
    invisible bucket — it shows up explicitly in the distribution."""
    if value is None:
        return "unknown"
    v = value.strip().lower()
    return v if v else "unknown"


def _as_fingerprint(study: object) -> StudyFingerprint:
    """Coerce anything that duck-types into ``modalities`` (list) +
    ``study_description`` into a :class:`StudyFingerprint`."""
    if isinstance(study, StudyFingerprint):
        return study
    modalities = getattr(study, "modalities", None) or []
    modality = modalities[0] if modalities else "unknown"
    desc = getattr(study, "study_description", None) or ""
    # body_part heuristic: first whitespace-split token of the
    # description, mapped to its normalised form. This is the same
    # lossy step the comment in the docstring warned about.
    tokens = desc.strip().split()
    body_part = tokens[0] if tokens else "unknown"
    return StudyFingerprint(modality=_normalise(modality), body_part=_normalise(body_part))


def compute_buckets(
    studies: Iterable[object],
) -> Counter[tuple[str, str]]:
    """Count how many studies fall into each quasi-identifier bucket."""
    counter: Counter[tuple[str, str]] = Counter()
    for s in studies:
        fp = _as_fingerprint(s)
        counter[(fp.modality, fp.body_part)] += 1
    return counter


def min_bucket_size(buckets: Counter[tuple[str, str]]) -> int:
    """Smallest bucket size, or 0 when there are no buckets."""
    if not buckets:
        return 0
    return min(buckets.values())


def enforce(studies: Iterable[object], *, k_min: int = DEFAULT_K_MIN) -> Counter[tuple[str, str]]:
    """Assert every bucket is ≥ ``k_min``.

    Returns the bucket distribution on success (useful for the caller
    to persist into ``licensed_datasets.k_anon`` / audit). Raises
    :class:`KAnonymityError` on the worst bucket when violated.
    Empty input raises as well — an empty aggregate is not a
    "satisfies k-anon by vacuity"; it is a bug upstream.
    """
    buckets = compute_buckets(studies)
    if not buckets:
        raise KAnonymityError(
            k_min=k_min,
            worst_bucket=("empty", "empty"),
            worst_size=0,
        )
    worst_bucket, worst_size = buckets.most_common()[-1]
    if worst_size < k_min:
        raise KAnonymityError(k_min=k_min, worst_bucket=worst_bucket, worst_size=worst_size)
    return buckets


__all__ = [
    "DEFAULT_K_MIN",
    "KAnonymityError",
    "StudyFingerprint",
    "compute_buckets",
    "enforce",
    "min_bucket_size",
]
