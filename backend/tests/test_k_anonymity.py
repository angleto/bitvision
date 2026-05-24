"""F10.2: k-anonymity unit tests.

The service is orthogonal to the ORM — we exercise it with
``StudyFingerprint`` values directly plus a couple of duck-typed
stand-ins to prove the coercion path."""

from __future__ import annotations

import pytest

from bvphoenix.services.k_anonymity import (
    DEFAULT_K_MIN,
    KAnonymityError,
    StudyFingerprint,
    compute_buckets,
    enforce,
    min_bucket_size,
)


def _fp(modality: str, body_part: str) -> StudyFingerprint:
    return StudyFingerprint(modality=modality.lower(), body_part=body_part.lower())


def test_default_k_min_is_5() -> None:
    assert DEFAULT_K_MIN == 5


def test_compute_buckets_counts_per_tuple() -> None:
    studies = [
        _fp("CT", "lung"),
        _fp("CT", "lung"),
        _fp("CT", "brain"),
        _fp("MR", "brain"),
    ]
    buckets = compute_buckets(studies)
    assert buckets[("ct", "lung")] == 2
    assert buckets[("ct", "brain")] == 1
    assert buckets[("mr", "brain")] == 1


def test_enforce_passes_when_every_bucket_meets_threshold() -> None:
    studies = [_fp("CT", "lung")] * 5 + [_fp("MR", "brain")] * 6
    buckets = enforce(studies, k_min=5)
    assert buckets[("ct", "lung")] == 5
    assert buckets[("mr", "brain")] == 6


def test_enforce_raises_on_smallest_bucket() -> None:
    studies = [_fp("CT", "lung")] * 10 + [_fp("MR", "rarefind")] * 3
    with pytest.raises(KAnonymityError) as exc:
        enforce(studies, k_min=5)
    assert exc.value.worst_bucket == ("mr", "rarefind")
    assert exc.value.worst_size == 3
    assert exc.value.k_min == 5


def test_enforce_raises_on_empty_input() -> None:
    with pytest.raises(KAnonymityError):
        enforce([], k_min=5)


def test_min_bucket_size_on_empty_is_zero() -> None:
    assert min_bucket_size(compute_buckets([])) == 0


def test_fingerprint_coerces_duck_typed_study() -> None:
    class _Study:
        def __init__(self) -> None:
            self.modalities = ["ct"]
            self.study_description = "Chest CT, contrast"

    studies = [_Study() for _ in range(5)]
    buckets = enforce(studies, k_min=5)
    # modality lower-cased, body_part taken from first token of description
    assert buckets[("ct", "chest")] == 5


def test_missing_fields_become_unknown_bucket() -> None:
    class _Study:
        def __init__(self) -> None:
            self.modalities: list[str] = []
            self.study_description = None

    studies = [_Study() for _ in range(5)]
    buckets = enforce(studies, k_min=5)
    assert buckets[("unknown", "unknown")] == 5


def test_k_min_can_be_relaxed_for_research_scenarios() -> None:
    """The module takes ``k_min`` as a parameter — a research-license
    deal that the DUC accepts at a lower threshold is still possible,
    as long as the policy trail records the choice."""
    studies = [_fp("CT", "lung")] * 3
    # k_min=3 → passes.
    enforce(studies, k_min=3)
    # k_min=5 → fails.
    with pytest.raises(KAnonymityError):
        enforce(studies, k_min=5)
