"use client";

// Single builder for a Cornerstone ``IImageVolume`` from the backend's
// packed Float32 ``volume.raw`` payload. Centralised here so the MPR
// layout, the MIP/3D viewport, and the alternate cornerstone route all
// produce an IDENTICAL volume for the same ``volumeId`` (they share the
// Cornerstone cache, so a divergent origin/direction/FoR between two
// builders would make crosshair-jump and fusion layering inconsistent
// depending on which mounted first).
//
// Geometry: ``data.origin`` / ``data.direction`` carry the REAL DICOM
// patient-space frame, recovered from the ``X-Volume-*`` response headers
// (the blob's 32-byte binary header is frozen and carries none of it).
// When present the volume is built in true LPS space so MPR reformats,
// world-space measurements and the on-image orientation markers are
// driven by data, not a fabricated identity frame. Legacy packs (no
// geometry) fall back to the identity frame, exactly as before.

import * as cs from "@cornerstonejs/core";

import type { VolumeData } from "@/components/VolumeViewer";

/** Identity i/j/k axes — used when a pack predates the geometry headers. */
export const IDENTITY_DIRECTION: [
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
] = [1, 0, 0, 0, 1, 0, 0, 0, 1];

/**
 * Build (and cache) a Cornerstone local volume.
 *
 * @param frameOfReferenceUID overrides the FrameOfReferenceUID to build
 *   with. Cornerstone only layers two volumes on one viewport when they
 *   share a FoR, so the caller passes the resolved FoR (the volume's real
 *   FoR for a solo series or a genuinely co-registered fusion pair, or a
 *   synthetic shared id to force an explicitly-requested overlay of an
 *   un-co-registered pair). When omitted, the volume's own real FoR is
 *   used, falling back to ``volumeId``.
 */
export function buildLocalVolume(
  volumeId: string,
  data: VolumeData,
  frameOfReferenceUID?: string,
): cs.Types.IImageVolume {
  const hasGeometry = Array.isArray(data.origin) && Array.isArray(data.direction);
  const origin: [number, number, number] = hasGeometry
    ? (data.origin as [number, number, number])
    : [0, 0, 0];
  const direction = hasGeometry
    ? (data.direction as typeof IDENTITY_DIRECTION)
    : IDENTITY_DIRECTION;
  // ImageOrientationPatient = row + column cosines = first 6 of direction.
  const iop: [number, number, number, number, number, number] = [
    direction[0],
    direction[1],
    direction[2],
    direction[3],
    direction[4],
    direction[5],
  ];
  const frameOfReference = frameOfReferenceUID ?? data.frameOfReferenceUid ?? volumeId;
  return cs.volumeLoader.createLocalVolume(volumeId, {
    metadata: {
      BitsAllocated: 32,
      BitsStored: 32,
      SamplesPerPixel: 1,
      HighBit: 31,
      PhotometricInterpretation: "MONOCHROME2",
      PixelRepresentation: 0,
      Modality: "OT",
      ImagePositionPatient: origin,
      ImageOrientationPatient: iop,
      PixelSpacing: [data.spacing[0], data.spacing[1]],
      Columns: data.dimensions[0],
      Rows: data.dimensions[1],
      FrameOfReferenceUID: frameOfReference,
      voiLut: [{ windowCenter: 0, windowWidth: 1 }],
      VOILUTFunction: "LINEAR",
    } as unknown as cs.Types.Metadata,
    dimensions: [data.dimensions[0], data.dimensions[1], data.dimensions[2]],
    spacing: [data.spacing[0], data.spacing[1], data.spacing[2]],
    origin,
    direction,
    scalarData: data.scalars,
  });
}
