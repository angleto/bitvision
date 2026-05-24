"""Tests for the DICOM series splitter."""

from __future__ import annotations

from bvphoenix.services.series_splitter import InstanceInfo, split_series


def _inst(
    *,
    sop_uid: str = "1",
    series_uid: str = "S1",
    orientation: str = "1.00,0.00,0.00,0.00,1.00,0.00",
    echo: int | None = None,
    temporal: int | None = None,
    acq: int | None = None,
    frame_ref: str = "FR1",
    inst_num: int = 1,
    z: float = 0.0,
) -> InstanceInfo:
    return InstanceInfo(
        path_or_key=f"test/{sop_uid}",
        sop_instance_uid=sop_uid,
        series_instance_uid=series_uid,
        instance_number=inst_num,
        orientation_key=orientation,
        echo_number=echo,
        temporal_position=temporal,
        acquisition_number=acq,
        frame_of_reference_uid=frame_ref,
        slice_location=z,
        image_position_z=z,
    )


def test_no_split_when_homogeneous() -> None:
    instances = [_inst(sop_uid=f"sop{i}", z=float(i)) for i in range(10)]
    result = split_series(instances)
    assert len(result) == 1
    assert result[0].reason == "no split needed"
    assert len(result[0].instances) == 10


def test_split_by_orientation() -> None:
    axial = [
        _inst(sop_uid=f"ax{i}", orientation="1.00,0.00,0.00,0.00,1.00,0.00", z=float(i))
        for i in range(5)
    ]
    sagittal = [
        _inst(sop_uid=f"sag{i}", orientation="0.00,1.00,0.00,0.00,0.00,-1.00", z=float(i))
        for i in range(3)
    ]
    result = split_series(axial + sagittal)
    assert len(result) == 2
    assert any("different orientation" in s.reason for s in result)


def test_split_by_echo_number() -> None:
    echo1 = [_inst(sop_uid=f"e1_{i}", echo=1, z=float(i)) for i in range(10)]
    echo2 = [_inst(sop_uid=f"e2_{i}", echo=2, z=float(i)) for i in range(10)]
    result = split_series(echo1 + echo2)
    assert len(result) == 2
    assert any("echo" in s.reason for s in result)


def test_split_by_temporal_position() -> None:
    t1 = [_inst(sop_uid=f"t1_{i}", temporal=1, z=float(i)) for i in range(5)]
    t2 = [_inst(sop_uid=f"t2_{i}", temporal=2, z=float(i)) for i in range(5)]
    t3 = [_inst(sop_uid=f"t3_{i}", temporal=3, z=float(i)) for i in range(5)]
    result = split_series(t1 + t2 + t3)
    assert len(result) == 3


def test_split_by_acquisition_number() -> None:
    a1 = [_inst(sop_uid=f"a1_{i}", acq=1, z=float(i)) for i in range(8)]
    a2 = [_inst(sop_uid=f"a2_{i}", acq=2, z=float(i)) for i in range(4)]
    result = split_series(a1 + a2)
    assert len(result) == 2
    assert result[0].instances[0].acquisition_number == 1  # largest group first


def test_split_by_frame_of_reference() -> None:
    fr1 = [_inst(sop_uid=f"fr1_{i}", frame_ref="FR-A", z=float(i)) for i in range(5)]
    fr2 = [_inst(sop_uid=f"fr2_{i}", frame_ref="FR-B", z=float(i)) for i in range(5)]
    result = split_series(fr1 + fr2)
    assert len(result) == 2
    assert any("frame of reference" in s.reason for s in result)


def test_compound_split() -> None:
    """Multiple criteria active at once."""
    instances = [
        _inst(sop_uid="a", echo=1, acq=1),
        _inst(sop_uid="b", echo=1, acq=2),
        _inst(sop_uid="c", echo=2, acq=1),
        _inst(sop_uid="d", echo=2, acq=2),
    ]
    result = split_series(instances)
    assert len(result) == 4


def test_empty_input() -> None:
    result = split_series([])
    assert len(result) == 1
    assert len(result[0].instances) == 0


def test_instances_sorted_by_z() -> None:
    instances = [
        _inst(sop_uid="z3", z=30.0, inst_num=3),
        _inst(sop_uid="z1", z=10.0, inst_num=1),
        _inst(sop_uid="z2", z=20.0, inst_num=2),
    ]
    result = split_series(instances)
    assert [i.sop_instance_uid for i in result[0].instances] == ["z1", "z2", "z3"]
