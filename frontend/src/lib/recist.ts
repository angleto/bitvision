// RECIST 1.1 target-lesion orchestration + pure display helpers.
//
// The viewer draws a bidirectional measurement (long + short axis) on a
// target lesion at each timepoint. `persistTargetTimepoint` turns one such
// measurement into the persisted chain our RECIST math reads:
//
//     Marker (geometry) ──▶ Finding (measured, typed) ──▶ LesionTrackPoint
//
// It persists steps 1–2 (marker → finding-with-geometry-ref); the caller adds
// the LesionTrackPoint (it owns the baseline-vs-followup branch). The pure
// helpers below (rationale, basis parsing, target-threshold) carry the
// medical logic the card renders and are unit-tested without React.

import type {
  Finding,
  FindingGeometryRole,
  FindingLaterality,
  Marker,
  MarkerKind,
  ResponseCategory,
} from "./api";

/** Finding type slug for a RECIST target: parenchymal lesion vs lymph node.
 *  Drives the backend's nodal-short-axis summation. */
export type RecistLesionType = "lesion" | "lymph_node";

/** One completed bidirectional measurement, as emitted by the MPR layout. */
export interface BidirectionalMeasurement {
  longAxisMm?: number;
  shortAxisMm?: number;
  worldPoints?: Array<[number, number, number]>;
  frameOfReferenceUID?: string;
}

export interface PersistTargetArgs {
  patientId: string;
  studyId: string;
  seriesId?: string | null;
  frameOfReferenceUID?: string | null;
  measurement: BidirectionalMeasurement;
  lesionType: RecistLesionType;
  anatomy?: string | null;
  laterality?: FindingLaterality | null;
  idempotencyKey?: string;
}

export interface PersistTargetResult {
  markerId: string;
  findingId: string;
}

/** The slice of the API client `persistTargetTimepoint` needs — narrowed so
 *  it can be exercised with a mock in unit tests. */
export interface RecistPersistenceApi {
  markers: {
    create: (
      patientId: string,
      input: {
        target_kind: "study" | "series" | "instance";
        target_id: string;
        kind: MarkerKind;
        geometry?: Record<string, unknown> | null;
        body?: string | null;
        computed?: Record<string, unknown> | null;
      },
    ) => Promise<Marker>;
    remove: (markerId: string) => Promise<void>;
  };
  findings: {
    create: (
      patientId: string,
      input: {
        study_id: string;
        series_id?: string | null;
        frame_of_reference_uid?: string | null;
        type: string;
        anatomy?: string | null;
        laterality?: FindingLaterality | null;
        longest_diameter_mm?: number | null;
        short_axis_mm?: number | null;
        status?: "candidate" | "confirmed" | "retracted";
        geometry_refs?: Array<{ marker_id?: string; role: FindingGeometryRole }>;
      },
      opts?: { idempotencyKey?: string },
    ) => Promise<Finding>;
  };
}

/** Persist one target-lesion timepoint: marker (geometry source of truth) →
 *  finding (measured, typed, linked to the marker). On a finding-create
 *  failure the orphan marker is rolled back (best effort) so a partial chain
 *  never lingers. Returns the persisted ids for the caller to attach a
 *  LesionTrackPoint. */
export async function persistTargetTimepoint(
  api: RecistPersistenceApi,
  args: PersistTargetArgs,
): Promise<PersistTargetResult> {
  const {
    patientId,
    studyId,
    seriesId,
    frameOfReferenceUID,
    measurement,
    lesionType,
    anatomy,
    laterality,
    idempotencyKey,
  } = args;

  const longAxisMm = measurement.longAxisMm ?? null;
  const shortAxisMm = measurement.shortAxisMm ?? null;
  const forUid = frameOfReferenceUID ?? measurement.frameOfReferenceUID ?? null;

  const marker = await api.markers.create(patientId, {
    target_kind: "study",
    target_id: studyId,
    kind: "measurement.distance",
    geometry: {
      axis: "bidirectional",
      world_points: measurement.worldPoints ?? [],
      frame_of_reference_uid: forUid,
    },
    computed: {
      long_axis_mm: longAxisMm,
      short_axis_mm: shortAxisMm,
      unit: "mm",
      recist_role: "target",
    },
  });

  try {
    const finding = await api.findings.create(
      patientId,
      {
        study_id: studyId,
        series_id: seriesId ?? null,
        frame_of_reference_uid: forUid,
        type: lesionType,
        anatomy: anatomy ?? null,
        laterality: laterality ?? null,
        longest_diameter_mm: longAxisMm,
        short_axis_mm: shortAxisMm,
        status: "confirmed",
        geometry_refs: [{ marker_id: marker.id, role: "measurement" }],
      },
      { idempotencyKey },
    );
    return { markerId: marker.id, findingId: finding.id };
  } catch (e) {
    // Roll back the orphan marker so we never leave a half-written chain.
    try {
      await api.markers.remove(marker.id);
    } catch {
      /* swallow — surface the original finding error */
    }
    throw e;
  }
}

// ---------------------------------------------------------------------------
// Pure display helpers (RECIST 1.1, Eisenhauer et al., EJC 2009).
// ---------------------------------------------------------------------------

/** One target lesion in the assessment basis. */
export interface RecistLesion {
  track_id: string;
  label: string;
  baseline_mm: number | null;
  current_mm: number | null;
  delta_mm: number | null;
  is_nodal: boolean;
  anatomy: string | null;
}

export interface RecistCaps {
  n_targets: number;
  max_targets: number;
  over_limit: boolean;
  per_organ: Record<string, number>;
  per_organ_over_limit: string[];
}

export interface RecistBasis {
  n_target_lesions: number;
  ne_reason: string | null;
  has_baseline: boolean;
  has_current: boolean;
  caps: RecistCaps | null;
  lesions: RecistLesion[];
}

/** Defensively read the assessment's ``basis`` JSON (typed as an opaque
 *  record on the wire) into the shape the card renders. */
export function readRecistBasis(basis: Record<string, unknown> | null | undefined): RecistBasis {
  const b = (basis ?? {}) as Record<string, unknown>;
  const lesionsRaw = Array.isArray(b.lesions) ? (b.lesions as Record<string, unknown>[]) : [];
  const capsRaw = b.caps as Record<string, unknown> | undefined;
  return {
    n_target_lesions: typeof b.n_target_lesions === "number" ? b.n_target_lesions : 0,
    ne_reason: typeof b.ne_reason === "string" ? b.ne_reason : null,
    has_baseline: b.has_baseline === true,
    has_current: b.has_current === true,
    caps: capsRaw
      ? {
          n_targets: Number(capsRaw.n_targets ?? 0),
          max_targets: Number(capsRaw.max_targets ?? 5),
          over_limit: capsRaw.over_limit === true,
          per_organ: (capsRaw.per_organ as Record<string, number>) ?? {},
          per_organ_over_limit: Array.isArray(capsRaw.per_organ_over_limit)
            ? (capsRaw.per_organ_over_limit as string[])
            : [],
        }
      : null,
    lesions: lesionsRaw.map((l) => ({
      track_id: String(l.track_id ?? ""),
      label: String(l.label ?? ""),
      baseline_mm: typeof l.baseline_mm === "number" ? l.baseline_mm : null,
      current_mm: typeof l.current_mm === "number" ? l.current_mm : null,
      delta_mm: typeof l.delta_mm === "number" ? l.delta_mm : null,
      is_nodal: l.is_nodal === true,
      anatomy: typeof l.anatomy === "string" ? l.anatomy : null,
    })),
  };
}

export interface RecistRationaleContext {
  targetSumMm: number | null;
  baselineSumMm: number | null;
  nadirSumMm: number | null;
  pctChange: number | null; // vs baseline
  newLesions: boolean;
}

/** The i18n key + percentage that explains why a category was assigned, so
 *  the card shows the RECIST rationale rather than a bare badge. The key is
 *  resolved under the ``response.rationale`` namespace; ``pct`` is the
 *  relevant percentage (vs baseline for PR/SD, vs nadir for PD). */
export function recistRationale(
  category: ResponseCategory,
  ctx: RecistRationaleContext,
): { key: string; pct: number | null } {
  switch (category) {
    case "CR":
      return { key: "CR", pct: null };
    case "PR":
      return { key: "PR", pct: ctx.pctChange };
    case "SD":
      return { key: "SD", pct: ctx.pctChange };
    case "PD": {
      if (ctx.newLesions) return { key: "PD_new", pct: null };
      const nadirPct =
        ctx.nadirSumMm != null && ctx.nadirSumMm > 0 && ctx.targetSumMm != null
          ? ((ctx.targetSumMm - ctx.nadirSumMm) / ctx.nadirSumMm) * 100
          : null;
      return { key: "PD", pct: nadirPct };
    }
    default:
      return { key: "NE", pct: null };
  }
}

/** RECIST 1.1 target-eligibility threshold: parenchymal lesions need a
 *  longest diameter >=10 mm, lymph nodes a short axis >=15 mm. Returns true
 *  when the drawn measurement is *below* threshold (the panel warns, but does
 *  not block — the radiologist may still track it). */
export function isBelowTargetThreshold(
  lesionType: RecistLesionType,
  longAxisMm: number | undefined,
  shortAxisMm: number | undefined,
): boolean {
  if (lesionType === "lymph_node") {
    return shortAxisMm === undefined || shortAxisMm < 15;
  }
  return longAxisMm === undefined || longAxisMm < 10;
}
