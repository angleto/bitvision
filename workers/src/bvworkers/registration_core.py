"""Pure SimpleITK registration primitives (no DB / S3).

Extracted from ``tasks/registration.py`` so the recipe is unit-testable on
synthetic phantoms and reusable by the lesion-propagation pipeline. Two
responsibilities:

* ``build_sitk_image_from_packed`` — turn a packed ``volume_f32``
  derivative (the 32-byte ``HEADER_STRUCT`` + Float32 payload) plus its
  stored ``Derivative.geometry`` into a SimpleITK image positioned in
  **true patient space (LPS)**. This is the fix for the historical bug
  where the worker re-stacked DICOM into index space (identity origin /
  direction), so the transform was not LPS→LPS and ignored gantry tilt /
  orientation / slice ordering.

* ``register_pair`` — the rigid (Mattes MI + gradient descent) recipe,
  optionally followed by a demons non-rigid step. Transform maps the
  moving image onto the fixed image, both in their real physical frames.

SimpleITK uses the LPS convention, the same as DICOM ImagePositionPatient /
ImageOrientationPatient, so the geometry maps across directly.
"""

from __future__ import annotations

from typing import Any


def build_sitk_image_from_packed(packed: bytes, geometry: dict | None) -> Any:
    """Build a SimpleITK image from a packed ``volume_f32`` blob + geometry.

    ``packed`` is ``HEADER_STRUCT`` (cols, rows, nz, sx, sy, sz, vmin, vmax)
    followed by the Float32 voxels in (nz, ny, nx) order. ``geometry`` is
    ``Derivative.geometry`` = ``{origin:[3], direction:[9], frame_of_reference_uid}``
    where ``direction`` is the row / column / slice unit cosines (the
    image x / y / z axes). When geometry is missing the image keeps an
    identity frame (index space) — a degraded but non-crashing fallback.
    """
    import numpy as np
    import SimpleITK as sitk  # noqa: N813 — community-standard alias
    from bvphoenix.services.volumes import HEADER_STRUCT

    nx, ny, nz, sx, sy, sz, _vmin, _vmax = HEADER_STRUCT.unpack_from(packed, 0)
    arr = np.frombuffer(packed, dtype=np.float32, offset=HEADER_STRUCT.size).reshape(
        int(nz), int(ny), int(nx)
    )
    # GetImageFromArray reads (z, y, x); copy because frombuffer is read-only.
    img = sitk.GetImageFromArray(np.ascontiguousarray(arr))
    img.SetSpacing((float(sx), float(sy), float(sz)))

    if geometry:
        origin = geometry.get("origin")
        direction = geometry.get("direction")
        if origin and len(origin) == 3:
            img.SetOrigin((float(origin[0]), float(origin[1]), float(origin[2])))
        if direction and len(direction) == 9:
            d = [float(x) for x in direction]
            # geometry.direction = [row(3), col(3), slice(3)] = the cosines
            # of image axes x, y, z. SimpleITK's direction is the row-major
            # 3x3 whose column j is the cosine of image axis j, i.e.
            # [[Rx,Cx,Sx],[Ry,Cy,Sy],[Rz,Cz,Sz]].
            img.SetDirection(
                (
                    d[0],
                    d[3],
                    d[6],
                    d[1],
                    d[4],
                    d[7],
                    d[2],
                    d[5],
                    d[8],
                )
            )
    return img


def warp_mask(mask: Any, transform: Any, reference: Any) -> Any:
    """Resample a label ``mask`` (in the moving/baseline frame) onto the
    ``reference`` grid (the fixed/follow-up frame) using ``transform`` from
    ``register_pair(fixed=follow_up, moving=baseline)``. Nearest-neighbour
    so labels stay integral. The result is the baseline lesion placed in the
    follow-up frame — the seed for re-measuring the follow-up."""
    import SimpleITK as sitk  # noqa: N813

    return sitk.Resample(
        mask, reference, transform, sitk.sitkNearestNeighbor, 0.0, mask.GetPixelID()
    )


def warp_bbox_lps(bbox_lps: dict, transform: Any) -> dict:
    """Map a world-space (LPS) AABB from the baseline frame into the
    follow-up frame. ``transform`` maps follow-up→baseline (the
    ``register_pair`` output), so its inverse maps baseline→follow-up; all
    8 corners are transformed and re-bounded (correct for any rotation).
    Requires an invertible transform (use the rigid kind)."""
    import numpy as np

    inv = transform.GetInverse()
    mn, mx = bbox_lps["min"], bbox_lps["max"]
    corners = [
        (float(x), float(y), float(z))
        for x in (mn[0], mx[0])
        for y in (mn[1], mx[1])
        for z in (mn[2], mx[2])
    ]
    pts = np.array([inv.TransformPoint(c) for c in corners])
    return {"min": pts.min(axis=0).tolist(), "max": pts.max(axis=0).tolist()}


def rigid_register(fixed: Any, moving: Any) -> tuple[Any, dict[str, Any]]:
    """Rigid (6-DOF Euler) registration: Mattes mutual information +
    regular-step gradient descent, geometry-centred init. Returns the
    transform mapping ``moving`` onto ``fixed`` and a metrics dict."""
    import SimpleITK as sitk  # noqa: N813

    fixed_f = sitk.Cast(fixed, sitk.sitkFloat32)
    moving_f = sitk.Cast(moving, sitk.sitkFloat32)

    initial = sitk.CenteredTransformInitializer(
        fixed_f,
        moving_f,
        sitk.Euler3DTransform(),
        sitk.CenteredTransformInitializerFilter.GEOMETRY,
    )
    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsMattesMutualInformation(numberOfHistogramBins=32)
    reg.SetMetricSamplingStrategy(reg.RANDOM)
    # Fixed seed → reproducible registrations (same inputs, same transform).
    reg.SetMetricSamplingPercentage(0.20, seed=42)
    reg.SetInterpolator(sitk.sitkLinear)
    reg.SetOptimizerAsRegularStepGradientDescent(
        learningRate=2.0,
        minStep=1e-4,
        numberOfIterations=200,
    )
    # Scale the parameter space by physical shift so rotation (radians) and
    # translation (mm) are comparably conditioned — markedly more robust.
    reg.SetOptimizerScalesFromPhysicalShift()
    reg.SetInitialTransform(initial, inPlace=False)
    transform = reg.Execute(fixed_f, moving_f)
    meta = {
        "rigid_metric": float(reg.GetMetricValue()),
        "rigid_iterations": int(reg.GetOptimizerIteration()),
        "rigid_metric_name": "MattesMutualInformation",
    }
    return transform, meta


def register_pair(fixed: Any, moving: Any, kind: str) -> tuple[Any, dict[str, Any]]:
    """Register ``moving`` onto ``fixed``. ``kind='rigid'`` returns the
    Euler transform; ``kind='demons'`` resamples by the rigid pre-step then
    runs FastSymmetricForcesDemons and returns a CompositeTransform. Rigid
    is the right default for tumour measurement — a deformable field can
    warp away the very size change being measured.

    Raises ``ValueError`` for an unknown kind; SimpleITK ``RuntimeError``
    propagates so the caller can record a failure.
    """
    import SimpleITK as sitk  # noqa: N813

    if kind not in ("rigid", "demons"):
        raise ValueError(f"unsupported registration kind: {kind!r}")

    fixed_f = sitk.Cast(fixed, sitk.sitkFloat32)
    moving_f = sitk.Cast(moving, sitk.sitkFloat32)

    rigid_transform, meta = rigid_register(fixed_f, moving_f)
    result_meta: dict[str, Any] = {"kind": kind, **meta}
    final: Any = rigid_transform

    if kind == "demons":
        # Resample moving onto the fixed grid via the rigid step so demons
        # sees two voxel-aligned volumes.
        moving_resampled = sitk.Resample(
            moving_f, fixed_f, rigid_transform, sitk.sitkLinear, 0.0, moving_f.GetPixelID()
        )
        demons = sitk.FastSymmetricForcesDemonsRegistrationFilter()
        demons.SetNumberOfIterations(50)
        demons.SetStandardDeviations(1.5)
        displacement_field = demons.Execute(fixed_f, moving_resampled)
        displacement_transform = sitk.DisplacementFieldTransform(displacement_field)
        final = sitk.CompositeTransform([rigid_transform, displacement_transform])
        result_meta.update(
            {
                "demons_metric": float(demons.GetMetric()),
                "demons_iterations": int(demons.GetElapsedIterations()),
                "demons_metric_name": "MeanSquaredDifference",
                "demons_filter": "FastSymmetricForcesDemons",
                "demons_std_deviations": 1.5,
            }
        )
    return final, result_meta
