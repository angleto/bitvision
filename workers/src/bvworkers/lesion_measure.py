"""Measure a lesion from a binary mask (pure SimpleITK, no DB / S3).

Used by the lesion-propagation pipeline to re-measure a lesion on the
follow-up grid, and reusable for any mask→measurement step. The mask is a
SimpleITK image carrying real geometry (spacing / origin / direction), so
the measurements are in true physical units:

* ``volume_ml`` — the foreground physical size (mm³ / 1000). This is the
  primary growth metric (the user asked to compare *volumes*).
* ``longest_diameter_mm`` — the Feret (max caliper) diameter, a 3D proxy
  for the RECIST longest diameter.
* ``bbox_lps`` — the world-space (LPS) axis-aligned bounding box, computed
  over all 8 corners so it is correct for any image direction.
"""

from __future__ import annotations

from typing import Any


def refine_mask_on_image(
    seed_mask: Any,
    image: Any,
    *,
    dilate_mm: float = 8.0,
    threshold: float | None = None,
) -> Any:
    """Re-segment a lesion on ``image`` (the follow-up) using ``seed_mask``
    (the baseline mask warped into the follow-up frame) as the seed.

    The measurement is then computed on the FOLLOW-UP's real voxels, not
    the warped baseline — so it captures genuine growth/shrinkage. Method:
    intensity-threshold within a dilation of the seed, keep the connected
    component that overlaps the seed. ``threshold`` defaults to the seed's
    own ``mean - std`` intensity (works for a high-contrast lesion such as
    a lung nodule; pass an explicit value, or use a learned segmenter, for
    soft-tissue). Returns the warped seed unchanged if nothing is found.
    """
    import numpy as np
    import SimpleITK as sitk  # noqa: N813

    seed = sitk.Cast(seed_mask > 0, sitk.sitkUInt8)
    img = sitk.Cast(image, sitk.sitkFloat32)

    seed_arr = sitk.GetArrayFromImage(seed).astype(bool)
    if not seed_arr.any():
        return seed
    if threshold is None:
        vals = sitk.GetArrayFromImage(img)[seed_arr]
        threshold = float(vals.mean() - vals.std())

    radius = [max(1, round(dilate_mm / s)) for s in seed.GetSpacing()]
    region = sitk.BinaryDilate(seed, radius)
    candidate = sitk.And(
        sitk.BinaryThreshold(
            img, lowerThreshold=threshold, upperThreshold=1e30, insideValue=1, outsideValue=0
        ),
        region,
    )
    cc = sitk.ConnectedComponent(candidate)
    cc_arr = sitk.GetArrayFromImage(cc)
    labels_in_seed = cc_arr[seed_arr]
    labels_in_seed = labels_in_seed[labels_in_seed > 0]
    if labels_in_seed.size == 0:
        return seed
    values, counts = np.unique(labels_in_seed, return_counts=True)
    keep = int(values[counts.argmax()])
    refined = sitk.GetImageFromArray((cc_arr == keep).astype(np.uint8))
    refined.CopyInformation(seed)
    return refined


def measure_mask(mask: Any) -> dict[str, Any]:
    """Measure the foreground of a binary/label ``mask`` image. Returns
    ``volume_ml``, ``longest_diameter_mm``, ``n_voxels`` and ``bbox_lps``
    (``None`` / zeros when the mask is empty)."""
    import numpy as np
    import SimpleITK as sitk  # noqa: N813

    label = sitk.Cast(mask > 0, sitk.sitkUInt8)
    stats = sitk.LabelShapeStatisticsImageFilter()
    stats.ComputeFeretDiameterOn()
    stats.Execute(label)
    if 1 not in stats.GetLabels():
        return {
            "volume_ml": 0.0,
            "longest_diameter_mm": 0.0,
            "n_voxels": 0,
            "bbox_lps": None,
        }

    volume_mm3 = float(stats.GetPhysicalSize(1))
    feret = float(stats.GetFeretDiameter(1))
    n_voxels = int(stats.GetNumberOfPixels(1))

    # Index bounding box → all 8 physical corners → world-space AABB.
    x0, y0, z0, sx, sy, sz = stats.GetBoundingBox(1)
    x1, y1, z1 = x0 + sx - 1, y0 + sy - 1, z0 + sz - 1
    corners = [
        label.TransformIndexToPhysicalPoint((int(x), int(y), int(z)))
        for x in (x0, x1)
        for y in (y0, y1)
        for z in (z0, z1)
    ]
    pts = np.array(corners)
    return {
        "volume_ml": volume_mm3 / 1000.0,
        "longest_diameter_mm": feret,
        "n_voxels": n_voxels,
        "bbox_lps": {"min": pts.min(axis=0).tolist(), "max": pts.max(axis=0).tolist()},
    }
