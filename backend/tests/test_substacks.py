"""Sub-stack de-interleaving (``services.volumes.partition_substacks``).

The keystone fix for the Philips mDIXON bug: one SeriesInstanceUID that
interleaves Water / Fat / In-phase / Out-of-phase volumes at the SAME
z-positions must be split into four coherent, monotonic-unique stacks
before packing — otherwise the naive ``sort by z`` collapses the slice
spacing to ~0 and the MPR geometry turns to garbage.

These tests run against synthetic pydicom datasets (no pixels, no S3) so
they stay fast and DB-free.
"""

from __future__ import annotations

import pydicom

from bvphoenix.services.volumes import list_substacks, partition_substacks

_IOP_AXIAL = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _slice(
    *,
    z: float,
    instance_number: int,
    image_type: list[str] | None = None,
    echo_time: float | None = None,
    iop: list[float] | None = None,
) -> pydicom.Dataset:
    ds = pydicom.Dataset()
    ds.SeriesInstanceUID = "1.2.3.SERIES"
    ds.SOPInstanceUID = f"1.2.3.{instance_number}"
    ds.InstanceNumber = instance_number
    ds.ImageOrientationPatient = iop if iop is not None else list(_IOP_AXIAL)
    ds.ImagePositionPatient = [0.0, 0.0, z]
    ds.Rows = 4
    ds.Columns = 4
    if image_type is not None:
        ds.ImageType = image_type
    if echo_time is not None:
        ds.EchoTime = echo_time
    return ds


def _mdixon(n_z: int = 6) -> list[pydicom.Dataset]:
    """A 4-contrast mDIXON: W/F/IP/OP, each spanning the same ``n_z`` axial
    z-positions, interleaved by instance number the way Philips stores them."""
    contrasts = [
        (["ORIGINAL", "PRIMARY", "W"], 4.6),
        (["ORIGINAL", "PRIMARY", "F"], 4.6),
        (["ORIGINAL", "PRIMARY", "IP"], 4.6),
        (["ORIGINAL", "PRIMARY", "OP"], 2.3),
    ]
    out: list[pydicom.Dataset] = []
    inst = 1
    for zi in range(n_z):
        for image_type, te in contrasts:
            out.append(
                _slice(z=float(zi) * 3.0, instance_number=inst, image_type=image_type, echo_time=te)
            )
            inst += 1
    return out


def _positions_along_z(datasets: list[pydicom.Dataset]) -> list[float]:
    return [float(ds.ImagePositionPatient[2]) for ds in datasets]


def test_mdixon_splits_into_four_monotonic_unique_stacks() -> None:
    n_z = 8
    stacks = partition_substacks(_mdixon(n_z))
    assert len(stacks) == 4
    for s in stacks:
        # Each stack covers every z-level exactly once.
        assert len(s.datasets) == n_z
        zs = sorted(_positions_along_z(s.datasets))
        assert zs == sorted(set(zs)), "stack must be monotonic-unique in z"


def test_mdixon_primary_is_water_at_index_zero() -> None:
    stacks = partition_substacks(_mdixon())
    by_index = {s.stack_index: s for s in stacks}
    assert by_index[0].image_type == "W"
    assert by_index[0].label == "Water"
    # All four contrasts present and uniquely labelled.
    tokens = {s.image_type for s in stacks}
    assert tokens == {"W", "F", "IP", "OP"}
    labels = {s.label for s in stacks}
    assert labels == {"Water", "Fat", "In-phase", "Out-of-phase"}


def test_stack_indices_are_contiguous_and_deterministic() -> None:
    a = partition_substacks(_mdixon())
    b = partition_substacks(_mdixon())
    assert [s.stack_index for s in a] == list(range(4))
    # Same input → same (stack_index, image_type) mapping across re-packs.
    assert {s.stack_index: s.image_type for s in a} == {s.stack_index: s.image_type for s in b}


def test_single_coherent_series_returns_one_main_stack() -> None:
    # A plain axial series: one contrast, monotonic-unique z.
    datasets = [_slice(z=float(i) * 2.0, instance_number=i + 1) for i in range(10)]
    stacks = partition_substacks(datasets)
    assert len(stacks) == 1
    assert stacks[0].stack_index == 0
    assert stacks[0].label == "main"
    # Fast path keeps ALL datasets (so the downstream pack is byte-identical).
    assert len(stacks[0].datasets) == 10


def test_geometric_fallback_splits_unlabelled_duplicate_z() -> None:
    # Two interleaved stacks that the TAGS do NOT separate (no ImageType,
    # same echo / orientation) — only the repeated z-positions reveal them.
    # Layer 2 must still split into two monotonic-unique stacks.
    datasets: list[pydicom.Dataset] = []
    inst = 1
    for zi in range(5):
        for _copy in range(2):
            datasets.append(_slice(z=float(zi) * 4.0, instance_number=inst))
            inst += 1
    stacks = partition_substacks(datasets)
    assert len(stacks) == 2
    for s in stacks:
        assert len(s.datasets) == 5
        zs = sorted(_positions_along_z(s.datasets))
        assert zs == sorted(set(zs))


def test_list_substacks_shape() -> None:
    rows = list_substacks(_mdixon(n_z=3))
    assert len(rows) == 4
    # (stack_index, label, image_type, instance_count)
    assert rows[0][0] == 0
    assert all(len(r) == 4 for r in rows)
    assert {r[3] for r in rows} == {3}


def test_empty_input_returns_no_stacks() -> None:
    assert partition_substacks([]) == []
