// Deterministic ordering of primary + fusion volumes for the
// Cornerstone3D viewer.
//
// Background: the fusion viewer used to push ``primary`` first and
// ``fusion`` second into ``volumeInputs`` regardless of modality.
// When the user opened a PET as ``primary`` and added a CT as
// ``fusion``, that ordering put the PET at pos[0] (rendered as the
// monochrome base, no PET LUT) and the CT at pos[1] with the PET
// colormap applied — i.e. PET visually in foreground, CT in
// background, the symptom users hit when they had to "reload to
// fix it". This module pins the rule "anatomical (CT/MR/...) is
// always the base, functional (PT/NM) is always the overlay" so
// the visual order doesn't depend on the order the user opened
// the volumes in.

/** Modalities that should always be rendered as the colored
 *  overlay (with the PET colormap + soft-shoulder opacity). The
 *  rest are anatomical and become the background grayscale base. */
export const FUNCTIONAL_MODALITIES = new Set(["PT", "NM"]);

/** True when this DICOM modality string represents functional
 *  imaging (PET / SPECT). Case-insensitive; ``null/undefined``
 *  is treated as anatomical (the safer default for fusions where
 *  modality wasn't yet resolved). */
export function isFunctionalModality(modality: string | null | undefined): boolean {
  if (!modality) return false;
  return FUNCTIONAL_MODALITIES.has(modality.toUpperCase());
}

export interface VolumeRef {
  volumeId: string;
  modality: string | null | undefined;
}

/** Result of the ordering decision: which volume is the
 *  anatomical base (rendered first, full opacity, no colormap)
 *  and which is the functional overlay (rendered on top with the
 *  PET LUT). Returns ``null`` for the overlay when no fusion
 *  was provided. */
export interface ResolvedOrder {
  baseVolumeId: string;
  overlayVolumeId: string | null;
  /** When ``true`` the user's "primary" was actually the functional
   *  volume and the resolver swapped the two. The viewer can use
   *  this to log a debug breadcrumb without re-deriving the
   *  decision. */
  swapped: boolean;
}

/** Decide the rendering order of primary + fusion based on
 *  modality. Rules:
 *  - No fusion → primary is the only volume (overlay = null).
 *  - primary functional + fusion anatomical → swap (CT base, PT overlay).
 *  - all other cases → keep primary as base, fusion as overlay
 *    (covers the canonical CT-primary + PET-fusion case AND the
 *    rare same-modality fusion which has no "right" answer).
 *
 *  The function is pure and deterministic so it can be exercised
 *  by a unit test without touching Cornerstone. */
export function resolveFusionOrder(primary: VolumeRef, fusion: VolumeRef | null): ResolvedOrder {
  if (!fusion) {
    return { baseVolumeId: primary.volumeId, overlayVolumeId: null, swapped: false };
  }
  const primaryFunctional = isFunctionalModality(primary.modality);
  const fusionFunctional = isFunctionalModality(fusion.modality);
  if (primaryFunctional && !fusionFunctional) {
    return {
      baseVolumeId: fusion.volumeId,
      overlayVolumeId: primary.volumeId,
      swapped: true,
    };
  }
  return {
    baseVolumeId: primary.volumeId,
    overlayVolumeId: fusion.volumeId,
    swapped: false,
  };
}
