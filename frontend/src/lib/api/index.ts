// Auto-split from lib/api.ts on 2026-05-21. Public barrel:
// callers continue to ``import { ... } from "@/lib/api"`` and
// pick up both the primitives in ./core and the domain endpoints
// below.

export * from "./core";
import {
  API_BASE_URL,
  ApiError,
  type Paginated,
  type QSValue,
  type SearchFacets,
  type SearchParams,
  type StudyListParams,
  _markAuthExpired,
  absoluteApiUrl,
  authedDownload,
  getStoredToken,
  qs,
  request,
  setStoredToken,
} from "./core";
// ``mintDownloadUrl``, ``triggerDownload`` and ``downloadJobResult``
// are defined in this file (they depend on ``request`` from core, but
// also on domain types so they stay in the domain barrel).

// -------- auth --------

export interface Me {
  subject_id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  email_verified: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
}

export interface RegisterResponse {
  subject_id: string;
  email: string;
  email_verification_required: boolean;
  access_token: string | null;
  token_type: "bearer";
}

/**
 * Mint a single-use signed download token (5 min, scope-bound) and
 * append it as ``?dt=...`` to ``baseUrl``. Used by every native
 * anchor-click download in the UI: doc download, doc-file download,
 * Job result ZIP. The browser navigates to the resulting URL via a
 * synthetic anchor, the backend validates+consumes the token in
 * Redis and proxy-streams the bytes — no fetch + Blob in the
 * browser, no 2 GiB cap, no RAM pressure on multi-GiB DVD ISOs.
 *
 * The persistent JWT never appears in URLs / referrers / proxy logs;
 * the token is opaque, scope-bound, and atomically consumed.
 */
export async function mintDownloadUrl(
  baseUrl: string,
  scope: { resource_kind: string; resource_id: string; child_id?: string },
): Promise<string> {
  const tokenResp = await request<{ token: string; expires_in: number }>(
    "/api/auth/download-token",
    { method: "POST", json: scope },
  );
  const sep = baseUrl.includes("?") ? "&" : "?";
  return `${baseUrl}${sep}dt=${encodeURIComponent(tokenResp.token)}`;
}

/**
 * Click a synthetic anchor at ``url``. Browser handles the actual
 * download (streaming-to-disk, resumability, cancellation). The
 * ``download`` attribute is a fallback filename hint; the backend's
 * Content-Disposition header is authoritative when present.
 */
export function triggerDownload(url: string, suggestedFilename?: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = suggestedFilename ?? "";
  // ``noopener noreferrer``: the URL carries a single-use ``?dt=``
  // download token. Without ``noreferrer`` the browser would send
  // the full URL (including the token) as the HTTP Referer to any
  // page the user navigates to next, leaking the capability into
  // proxy logs / 3rd-party trackers / next-page Analytics. The
  // token is single-use against a single resource, but Referer
  // leakage is still a defence-in-depth bug.
  a.rel = "noopener noreferrer";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/**
 * Download a Job's stored result via the backend proxy + signed
 * token. Mirrors the doc-download flow — a Bearer-less anchor click
 * would 401 since the browser can't carry an Authorization header on
 * top-level navigation.
 */
export async function downloadJobResult(jobId: string, suggestedFilename?: string): Promise<void> {
  const url = await mintDownloadUrl(`${API_BASE_URL}/api/jobs/${jobId}/result_download`, {
    resource_kind: "job_result",
    resource_id: jobId,
  });
  triggerDownload(url, suggestedFilename);
}

export interface SystemFeatures {
  /** True when a real LLM provider is configured server-side. When
   * false, the FE should disable / hide actions that depend on it
   * (care-phase classifier, fascicolo summary, agent flows) and
   * surface a "feature temporarily unavailable" hint. */
  llm_classifier: boolean;
}

export const systemApi = {
  features: () => request<SystemFeatures>("/api/system/features"),
};

export const authApi = {
  register: (email: string, password: string, displayName: string) =>
    request<RegisterResponse>("/api/auth/register", {
      method: "POST",
      json: { email, password, display_name: displayName },
    }),
  login: (email: string, password: string) =>
    request<TokenResponse>("/api/auth/login", {
      method: "POST",
      json: { email, password },
    }),
  loginMfa: (email: string, password: string, totpCode: string) =>
    request<TokenResponse>("/api/auth/login-mfa", {
      method: "POST",
      json: { email, password, totp_code: totpCode },
    }),
  me: () => request<Me>("/api/auth/me"),
  verifyEmail: (token: string) =>
    request<TokenResponse>("/api/auth/verify-email", {
      method: "POST",
      json: { token },
    }),
  resendVerification: (email: string) =>
    request<{ status: string }>("/api/auth/resend-verification", {
      method: "POST",
      json: { email },
    }),
  forgotPassword: (email: string) =>
    request<void>("/api/auth/forgot-password", {
      method: "POST",
      json: { email },
    }),
  resetPassword: (token: string, newPassword: string) =>
    request<void>("/api/auth/reset-password", {
      method: "POST",
      json: { token, new_password: newPassword },
    }),
};

// -------- MFA --------

export interface MfaStatus {
  enabled: boolean;
  pending: boolean;
  enabled_at: string | null;
  backup_codes_remaining: number;
}

export interface MfaSetup {
  provisioning_uri: string;
  qr_png_base64: string;
  secret: string;
}

export interface MfaActivateResult {
  backup_codes: string[];
  enabled_at: string;
}

export const mfaApi = {
  status: () => request<MfaStatus>("/api/mfa/status"),
  setup: () => request<MfaSetup>("/api/mfa/setup", { method: "POST" }),
  activate: (totpCode: string) =>
    request<MfaActivateResult>("/api/mfa/activate", {
      method: "POST",
      json: { totp_code: totpCode },
    }),
  disable: (totpCode: string) =>
    request<void>("/api/mfa/disable", {
      method: "POST",
      json: { totp_code: totpCode },
    }),
};

// -------- studies / series --------

export interface Study {
  id: string;
  study_instance_uid: string;
  owner_subject_id: string;
  patient_id: string | null;
  contribution_tier: string;
  is_public: boolean;
  is_listed_for_sale: boolean;
  ingestion_complete: boolean;
  study_description: string | null;
  study_date: string | null;
  modalities: string[];
  created_at: string;
  // Provenance / license for OpenData public-dataset imports (tier=t4).
  // NULL on user-uploaded private studies. Drives the citation badge.
  source_collection?: string | null;
  license_spdx?: string | null;
  license_url?: string | null;
  citation_required?: boolean;
  citation_text?: string | null;
  // False for CC-BY-NC-* licenses (educational / non-commercial reuse
  // only); true (or undefined) otherwise. Drives the badge's
  // "Non-commercial" treatment in <LicenseBadge>. Returned server-side
  // on OpenData imports alongside the other license fields.
  commercial_use_allowed?: boolean;
  // True iff owner_subject_id is the platform-owner subject. Computed
  // server-side on StudyOut so the FE does not need the platform-owner
  // UUID. Drives the "OpenData" filter distinction in /search.
  is_opendata?: boolean;
  // Whether the study has ≥1 image series with a BiomedCLIP vector, i.e.
  // /similar-to can anchor on it. Present only when the endpoint computes
  // it (/search?include_index_status=true, /studies/{id}); undefined =
  // unknown (don't render a badge). Drives the Visual Search picker.
  indexed?: boolean | null;
}

// -------- pathology whole-slide images --------

/**
 * One whole-slide image (WSI) in the pathology library. Mirrors the
 * backend ``PathologySlideOut`` shape. Public slides (``is_public``)
 * are served anonymously: the thumbnail / DZI / tile endpoints need no
 * auth header, so the public library + deep-zoom viewer work without a
 * session. The license / citation fields drive <LicenseBadge> exactly
 * as on studies.
 */
export interface PathologySlide {
  id: string;
  patient_id: string;
  stain: string | null;
  magnification: number | null;
  /** Micrometres per pixel at the base level (null = scale unknown, e.g.
   *  a gross photo with no calibration → the scale bar hides until the
   *  user calibrates manually). */
  mpp_x: number | null;
  mpp_y: number | null;
  source_format: string;
  /** wsi | gross | micrograph — drives whether a physical scale is
   *  expected (gross photos typically have no mpp). */
  slide_class: string;
  source_collection: string | null;
  license_spdx: string | null;
  license_url: string | null;
  citation_required: boolean;
  citation_text: string | null;
  commercial_use_allowed: boolean;
  is_opendata: boolean;
  is_public: boolean;
  /** Base (level-0) pixel dimensions of the pyramid. Null until the
   *  slide has been tiled. Used to seed the OpenSeadragon tile source
   *  as a fallback before the .dzi is parsed. */
  base_width: number | null;
  base_height: number | null;
  /** Deep-zoom pyramid state. ``dzi_ready`` gates the viewer; when false
   *  the pyramid is still being built (poll until true). */
  dzi_ready: boolean;
  dzi_levels: number | null;
  dzi_tile_size: number | null;
  pyramid_levels: number | null;
  created_at: string;
  has_macro: boolean;
}

export const pathologySlidesApi = {
  /** Public library listing. Returns a plain JSON array (not the
   *  ``Paginated`` envelope) — the backend honours limit/offset for
   *  cursor-style pagination. */
  listPublic: (params: { limit?: number; offset?: number } = {}) => {
    const merged = { public_only: true, ...params };
    return request<PathologySlide[]>(
      `/api/pathology-slides${qs(merged as Record<string, QSValue>)}`,
    );
  },
  /** List a patient's slides for the clinical viewer's slide tray. */
  listForPatient: (patientId: string, params: { limit?: number; offset?: number } = {}) =>
    request<PathologySlide[]>(
      `/api/pathology-slides${qs({ patient_id: patientId, public_only: false, ...params } as Record<string, QSValue>)}`,
    ),
  detail: (id: string) =>
    request<PathologySlide>(`/api/pathology-slides/${encodeURIComponent(id)}`),
  /** Absolute URL of the slide thumbnail JPEG. */
  thumbnailUrl: (id: string) =>
    `${API_BASE_URL}/api/pathology-slides/${encodeURIComponent(id)}/thumbnail`,
  /** Absolute URL of the macro overview JPEG (1x photo of the glass slide). */
  macroUrl: (id: string) => `${API_BASE_URL}/api/pathology-slides/${encodeURIComponent(id)}/macro`,
  /** Absolute URL of a stitched region JPEG at a DZI ``level``. */
  regionUrl: (id: string, region: { x: number; y: number; w: number; h: number; level: number }) =>
    `${API_BASE_URL}/api/pathology-slides/${encodeURIComponent(id)}/region${qs(region as Record<string, QSValue>)}`,
  /** Absolute URL of the Deep Zoom ``.dzi`` descriptor (XML). */
  dziUrl: (id: string) => `${API_BASE_URL}/api/pathology-slides/${encodeURIComponent(id)}/dzi`,
  /** Absolute URL of a single Deep Zoom tile. NOTE: slash-separated
   *  ``{level}/{col}/{row}`` (not the DZI default ``{col}_{row}.jpeg``),
   *  so OpenSeadragon must be fed a custom ``getTileUrl`` rather than
   *  the ``.dzi`` URL directly. */
  tileUrl: (id: string, level: number, col: number, row: number) =>
    `${API_BASE_URL}/api/pathology-slides/${encodeURIComponent(id)}/tiles/${level}/${col}/${row}`,
};

/** Convenience wrapper: list the public pathology library. */
export function listPublicPathologySlides(
  params: { limit?: number; offset?: number } = {},
): Promise<PathologySlide[]> {
  return pathologySlidesApi.listPublic(params);
}

/** Convenience wrapper: fetch one pathology slide's metadata. */
export function getPathologySlide(id: string): Promise<PathologySlide> {
  return pathologySlidesApi.detail(id);
}

export interface Series {
  id: string;
  study_id: string;
  series_instance_uid: string;
  series_number: number | null;
  modality: string | null;
  body_part_examined: string | null;
  series_description: string | null;
  expected_instance_count: number | null;
  received_instance_count: number;
  ingestion_complete: boolean;
  // Populated by /api/series/{id} from the middle DICOM instance's
  // WindowCenter / WindowWidth tags; null when absent.
  suggested_wc?: number | null;
  suggested_ww?: number | null;
  // Whether this series carries a BiomedCLIP vector (can anchor a visual
  // search). Present only on /studies/{id}; undefined = unknown. A
  // non-image modality (SR/SEG/...) is never indexed.
  indexed?: boolean | null;
}

export interface StudyDetail extends Study {
  series: Series[];
}

// Multiphase contrast-CT acquisition phase manifest (GET /studies/{id}/phases).
export interface SeriesPhase {
  series_id: string;
  series_number: number | null;
  modality: string | null;
  series_description: string | null;
  body_part_examined: string | null;
  acquisition_phase: string | null;
  phase_confidence: number | null;
  phase_source: string | null;
  needs_confirmation: boolean;
  acquisition_time_of_day: string | null;
  contrast_bolus_agent: string | null;
  frame_of_reference_uid: string | null;
  instance_count: number | null;
  /** Acquisition plane from packed geometry: axial | sagittal | coronal |
   *  oblique, or null when the series is not packed yet. */
  series_plane?: string | null;
  /** True when the series is a reviewable axial phase volume (CT, axial,
   *  enough slices, not a localizer / capture / dose / prep / reformat).
   *  Optional so an older backend (no field) degrades to the FE heuristic. */
  is_reviewable_phase?: boolean;
}

export interface StudyPhases {
  study_id: string;
  phases: SeriesPhase[];
}

// Cross-phase HU + wash-out (POST /studies/{id}/phase-roi-stats).
export interface PhaseRelativePoint {
  acquisition_phase: string;
  lesion_hu: number;
  parenchyma_hu: number;
  /** lesion − parenchyma; negative ⇒ lesion hypodense vs reference (liver wash-out). */
  delta_hu: number;
}

export interface PhaseWashout {
  /** Region scoping the interpretation: "adrenal" | "liver" | other/null. */
  region: string | null;
  unenhanced_phase: string | null;
  enhanced_phase: string | null;
  delayed_phase: string | null;
  unenhanced_hu: number | null;
  enhanced_hu: number | null;
  delayed_hu: number | null;
  absolute_enhancement_hu: number | null;
  // APW/RPW are adrenal indices: present for adrenal/other, null for liver.
  // The *_ge_* flags are adenoma verdicts: emitted for adrenal only.
  apw: number | null;
  rpw: number | null;
  apw_ge_60: boolean | null;
  rpw_ge_40: boolean | null;
  unenhanced_below_10hu: boolean | null;
  curve: Array<{ acquisition_phase: string; hu_mean: number }>;
  // Liver workflow: reference-parenchyma HU per phase + lesion-minus-
  // parenchyma per phase (the qualitative LI-RADS wash-out signal).
  parenchyma_curve: Array<{ acquisition_phase: string; hu_mean: number }>;
  relative_curve: PhaseRelativePoint[];
}

export interface PhaseSampleStat {
  series_id: string;
  acquisition_phase: string | null;
  hu_mean: number;
  hu_std: number;
  voxel_count: number;
  frame_of_reference_uid: string | null;
}

export interface PhaseRoiStats {
  study_id: string;
  reference_frame_of_reference_uid: string | null;
  samples: PhaseSampleStat[];
  skipped: Array<{ series_id: string; acquisition_phase: string | null; reason: string }>;
  washout: PhaseWashout;
}

export interface PhaseRoiInput {
  kind: "sphere" | "bbox";
  center_lps?: [number, number, number];
  radius_mm?: number;
  min_lps?: [number, number, number];
  max_lps?: [number, number, number];
  frame_of_reference_uid?: string | null;
  /** Anatomical region — scopes the wash-out interpretation. */
  region?: "adrenal" | "liver" | "other" | null;
  /** Optional reference-parenchyma sphere (liver workflow), sampled per phase. */
  parenchyma_center_lps?: [number, number, number];
  parenchyma_radius_mm?: number;
  /**
   * Per-phase lesion ROIs, each in THAT phase's own world (LPS) frame. The
   * multiphase phases share a FrameOfReferenceUID but were acquired at
   * different table positions (different world origins), so a single world ROI
   * re-mapped across them falls outside the shifted phases' z-range. The viewer
   * syncs by slice index, so it sends each phase its own world point for the
   * same anatomy. When present, the phase matching ``series_id`` is sampled at
   * its own centre instead of ``center_lps``.
   */
  phase_rois?: Array<{
    series_id: string;
    center_lps: [number, number, number];
    radius_mm: number;
  }>;
  /** Per-phase reference-parenchyma ROIs (liver workflow), same semantics. */
  phase_parenchyma_rois?: Array<{
    series_id: string;
    center_lps: [number, number, number];
    radius_mm: number;
  }>;
}

/** Per-voxel wash-out / subtraction heat map (POST /studies/{id}/washout-map). */
export interface PhaseMap {
  metric: string;
  phase_a: string;
  phase_b: string;
  /** Symmetric HU colour scale (green = wash-out, red = uptake). */
  vabs: number;
  width: number;
  height: number;
  png_base64: string;
}

export interface PhaseEnhancementSet {
  id: string;
  study_id: string;
  patient_id: string;
  label: string | null;
  roi_kind: string;
  roi: Record<string, unknown>;
  samples: Array<Record<string, unknown>> | null;
  washout: PhaseWashout | null;
  apw: number | null;
  rpw: number | null;
  enhanced_phase: string | null;
  delayed_phase: string | null;
  author_kind: string;
  etag: string;
  deleted_at: string | null;
  created_at: string | null;
}

export interface DisplayMetadata {
  series_id: string;
  photometric_interpretation: string | null;
  invert: boolean;
  pixel_spacing: [number, number];
  rows: number;
  columns: number;
  /**
   * PET-specific: True iff modality is PT and the series carries
   * enough header tags to compute SUV. The frontend only shows the
   * PET HUD when this flag is set.
   */
  is_pet: boolean;
  /**
   * Multiplier the client applies to the rescaled pixel value to
   * obtain SUV body-weight: ``suv = pixel * suv_factor_bw``. Null
   * when PET metadata is incomplete (missing weight / dose / time).
   */
  suv_factor_bw: number | null;
  patient_weight_kg: number | null;
  radionuclide: string | null;
  units: string | null;
  suv_notes: string[];
  /** SUV variants per Addendum C §5–§6. Null when PatientSize /
   *  PatientSex are missing or implausible (each formula has its
   *  own validity domain — Janmahasatian works at any BMI, James
   *  goes negative at very high BMI, BSA needs height + weight).
   *  Frontend picks one from preferences; the chosen factor is
   *  multiplied with the rescaled pixel to display SUV. */
  suv_factor_lbm_janmahasatian: number | null;
  suv_factor_lbm_james: number | null;
  suv_factor_bsa_mosteller: number | null;
  suv_factor_bsa_dubois: number | null;
  patient_height_m: number | null;
  patient_sex: string | null;
  /** Canonical tracer short name (FDG / PSMA / DOTATATE / FET / ...). */
  tracer: string | null;
  /** Positron branching ratio of the detected nuclide. ~1 for FDG,
   *  lower for ⁶⁸Ga / ⁶⁴Cu / ⁸⁹Zr. Surfaced for transparency; the
   *  scanner's calibration usually already accounts for it. */
  branching_ratio: number | null;
  /** Non-blocking sanity warnings: implausible weight, dose out of
   *  clinical range, decay correction NONE, etc. */
  suv_warnings: string[];
  /** DICOM FrameOfReferenceUID. Two series share the same FoR only
   *  when coregistered hardware-side; the viewer compares primary vs
   *  fusion FoRs and warns on mismatch (spec §1.2). */
  frame_of_reference_uid: string | null;
  /** Multi-frame 4D dynamic study flag. Backend exposes only the
   *  first time frame; the viewer surfaces a banner when this is set. */
  is_dynamic_4d: boolean;
  n_time_frames: number;
  /**
   * DICOM ImageOrientationPatient (0020,0037) of the first instance:
   * six floats [Rx, Ry, Rz, Cx, Cy, Cz]. ``null`` when the tag is
   * absent (legacy CR / DX). Drives ``primary_plane`` and lets the
   * MPR viewer align oblique reformats to the slice plane.
   */
  image_orientation_patient: [number, number, number, number, number, number] | null;
  /**
   * Acquisition plane derived from the slice normal:
   * ``"axial" | "sagittal" | "coronal" | "oblique" | "unknown"``.
   * ``unknown`` is the routing hint for non-volumetric / 2D-only
   * series (single slice, secondary capture, etc.); the frontend
   * skips MPR setup and goes straight to the 2D slice viewer.
   */
  primary_plane: "axial" | "sagittal" | "coronal" | "oblique" | "unknown";
  /** Number of instances in the series. Single-slice series open in
   *  the 2D viewer; ``>= 2`` is the MPR threshold. */
  instance_count: number;
  /** Co-located sub-stacks packed under one SeriesInstanceUID (Philips
   *  mDIXON Water/Fat/In-phase/Out-of-phase, multi-echo, DWI). One entry
   *  for the common single-stack series; multiple entries drive the
   *  viewer's contrast picker. Each is fetched via
   *  ``volume.raw?stack=<stack_index>``. */
  sub_stacks: SubStackInfo[];
  /** stack_index the viewer should open by default (the primary; for
   *  mDIXON the Water contrast). */
  default_stack_index: number;
}

export interface SubStackInfo {
  stack_index: number;
  /** Human-readable: 'Water', 'Fat', 'In-phase', 'b=1000', 'TE=2.3ms', 'main', ... */
  label: string;
  /** ImageType[2] token when present (W / F / IP / OP). */
  image_type: string | null;
  instance_count: number;
}

export const studiesApi = {
  list: (params: StudyListParams = {}) =>
    request<Paginated<Study>>(`/api/studies${qs(params as Record<string, QSValue>)}`),
  detail: (id: string) => request<StudyDetail>(`/api/studies/${id}`),
  series: (id: string) => request<Series>(`/api/series/${id}`),
  fusionCandidates: (studyId: string, excludeSeriesId?: string) =>
    request<Series[]>(
      `/api/studies/${studyId}/fusion-candidates${qs({ exclude_series_id: excludeSeriesId })}`,
    ),
  /** Volume URL builder. ``earlFwhmMm > 0`` opts into EANM/EARL
   *  Gaussian harmonisation server-side (Addendum C §7); the
   *  filtered volume is cached separately under a derivative
   *  format keyed by the FWHM, so subsequent fetches at the same
   *  level hit the cache. ``stackIndex`` selects a sub-stack of a
   *  multi-stack series (mDIXON W/F/IP/OP, multi-echo, DWI); omit or
   *  0 for the primary stack. */
  volumeUrl: (seriesId: string, opts?: { earlFwhmMm?: number; stackIndex?: number }) => {
    const earl = opts?.earlFwhmMm ?? 0;
    const stack = opts?.stackIndex ?? 0;
    const params = new URLSearchParams();
    if (earl > 0) params.set("earl_fwhm_mm", String(earl));
    if (stack > 0) params.set("stack", String(stack));
    const query = params.toString();
    return `${API_BASE_URL}/api/series/${seriesId}/volume.raw${query ? `?${query}` : ""}`;
  },
  /** Low-res (1/8) preview of the primary stack — same packed format,
   *  ~8x smaller. Drives the progressive first paint: the viewer renders
   *  this in ~3s, then swaps in ``volume.raw`` when it finishes streaming.
   *  No earl/stack params: the preview is always the primary, unfiltered. */
  volumePreviewUrl: (seriesId: string) =>
    `${API_BASE_URL}/api/series/${seriesId}/volume-preview.raw`,
  instanceFileUrl: (instanceId: string) => `${API_BASE_URL}/api/instances/${instanceId}/file`,
  packVolume: (seriesId: string) =>
    request<{ status: string; series_id: string }>(`/api/series/${seriesId}/pack-volume`, {
      method: "POST",
    }),
  embedSeries: (seriesId: string) =>
    request<{ status: string; series_id: string }>(`/api/series/${seriesId}/embed`, {
      method: "POST",
    }),
  displayMetadata: (seriesId: string) =>
    request<DisplayMetadata>(`/api/series/${seriesId}/display-metadata`),
  // --- Multiphase contrast-CT acquisition phases ---
  /** Read-only manifest: the study's series ordered by acquisition time,
   *  each with its classified contrast phase + confidence + source + FoR. */
  phases: (studyId: string) => request<StudyPhases>(`/api/studies/${studyId}/phases`),
  /** Run the classifier and persist auto labels (preserves human overrides
   *  unless ``force``). Returns the refreshed manifest. */
  detectPhases: (studyId: string, force = false) =>
    request<StudyPhases>(`/api/studies/${studyId}/phases/detect${force ? "?force=true" : ""}`, {
      method: "POST",
    }),
  /** Human override of one series' contrast phase (phase_source='human'),
   *  or ``null`` to clear and re-enable auto. */
  setSeriesPhase: (seriesId: string, acquisitionPhase: string | null, dryRun = false) =>
    request<SeriesPhase>(`/api/series/${seriesId}/acquisition-phase`, {
      method: "PATCH",
      json: { acquisition_phase: acquisitionPhase, dry_run: dryRun },
    }),
  /** Sample one world-space (LPS) ROI across the study's phases + wash-out. */
  phaseRoiStats: (studyId: string, roi: PhaseRoiInput) =>
    request<PhaseRoiStats>(`/api/studies/${studyId}/phase-roi-stats`, {
      method: "POST",
      json: roi,
    }),
  /** Per-voxel wash-out / subtraction heat map over the lesion region. */
  washoutMap: (
    studyId: string,
    body: {
      center_lps: [number, number, number];
      radius_mm: number;
      metric?: "washout" | "subtraction";
    },
  ) =>
    request<PhaseMap>(`/api/studies/${studyId}/washout-map`, {
      method: "POST",
      json: body,
    }),
  /** Persist a wash-out measurement (the samples from phaseRoiStats). */
  createPhaseEnhancementSet: (
    studyId: string,
    body: {
      roi_kind: "sphere" | "bbox";
      roi: Record<string, unknown>;
      label?: string;
      samples: Array<Record<string, unknown>>;
    },
  ) =>
    request<PhaseEnhancementSet>(`/api/studies/${studyId}/phase-enhancement-sets`, {
      method: "POST",
      json: body,
    }),
  listPhaseEnhancementSets: (studyId: string) =>
    request<PhaseEnhancementSet[]>(`/api/studies/${studyId}/phase-enhancement-sets`),
  /**
   * Enqueue an async Job that streams every DICOM in a study into a
   * ZIP archive on S3. Returns the Job descriptor; poll
   * ``GET /api/jobs/{id}`` (via ``useJob``) for progress and the
   * presigned download URL.
   *
   * Mirrors ``requestFascicoloExport``: the heavy work runs in the
   * worker so it survives client disconnects, laptop sleep, and
   * logout/login. The artifact lives on S3 for 48h and is fetched
   * back through the proxied ``/api/jobs/{id}/result_download``
   * (storage-isolation invariant preserved end-to-end). The endpoint
   * is dedup-keyed by ``(kind="study_export", scope=study_id)`` so
   * a second click while a first export is still running rebinds to
   * the in-flight job instead of starting a duplicate.
   */
  requestStudyExport: (studyId: string) =>
    request<import("../jobs").JobOut>(`/api/studies/${encodeURIComponent(studyId)}/export`, {
      method: "POST",
      json: {},
    }),
  /** Create a study-scoped share link. The backend defaults
   *  ``deidentify`` to ON for external grants (authorization.md §7),
   *  but pass an explicit boolean to override either way. */
  share: (studyId: string, input: Record<string, unknown>) =>
    request<ShareLink>(`/api/studies/${encodeURIComponent(studyId)}/share`, {
      method: "POST",
      json: input,
    }),
  /** Email the recipient_email on file for an existing share link.
   *  The body is plain-text and explicitly excludes the password
   *  (transactional credentials must travel out-of-band). ``locale``
   *  drives IT/EN copy on the email; defaults to the request's
   *  Accept-Language on the backend if omitted. */
  notifyShare: (linkId: string, customMessage?: string | null, locale?: "it" | "en" | null) =>
    request<{ sent: boolean; to: string }>(
      `/api/share-links/${encodeURIComponent(linkId)}/notify`,
      {
        method: "POST",
        json: { custom_message: customMessage ?? null, locale: locale ?? null },
      },
    ),
};

// -------- PET-specific endpoints --------

export interface VoiMetricsOut {
  suv_max: number;
  suv_peak: number | null;
  suv_mean: number;
  mtv_ml: number;
  tlg: number;
  voxel_count: number;
  units: "SUV" | "raw";
  voi_kind: string;
  notes: string[];
  suv_factor_bw_used: number | null;
}

export interface SphericalVoiInput {
  center_mm: { x: number; y: number; z: number };
  radius_mm: number;
}

export interface ThresholdVoiInput {
  seed_mm: { x: number; y: number; z: number };
  threshold: number;
  threshold_units: "SUV" | "raw";
}

export const petVoiApi = {
  spherical: (seriesId: string, body: SphericalVoiInput) =>
    request<VoiMetricsOut>(`/api/series/${seriesId}/voi/spherical`, {
      method: "POST",
      json: body,
    }),
  threshold: (seriesId: string, body: ThresholdVoiInput) =>
    request<VoiMetricsOut>(`/api/series/${seriesId}/voi/threshold`, {
      method: "POST",
      json: body,
    }),
};

export interface MipCineManifest {
  sprite_url: string;
  frame_count: number;
  frame_width: number;
  frame_height: number;
  units: "SUV" | "raw";
  suv_window: [number, number];
}

export const petMipApi = {
  /** Trigger generation (cached) and return the manifest. The sprite PNG
   *  lives at the sibling .png URL, ready to be set as <img src>. */
  cine: (seriesId: string, params: { num_frames?: number; target_height?: number } = {}) =>
    request<MipCineManifest>(
      `/api/series/${seriesId}/mip-cine${qs(params as Record<string, QSValue>)}`,
    ),
  spriteUrl: (seriesId: string, params: { frames?: number; height?: number } = {}) =>
    `${API_BASE_URL}/api/series/${seriesId}/mip-cine.png${qs(params as Record<string, QSValue>)}`,
};

export interface SimilarStudy {
  study: Study;
  score: number;
  matched_series_id: string;
}

export interface HybridSignalScores {
  tag: number;
  text: number;
  image: number;
}

export interface HybridSearchItem {
  study: Study;
  score: number;
  signals: HybridSignalScores;
}

export interface HybridSearchOut {
  items: HybridSearchItem[];
  weights_used: Record<string, number>;
  query: string;
}

export interface HybridSignalScores {
  tag: number;
  text: number;
  image: number;
}
export interface HybridSearchItem {
  study: Study;
  score: number;
  signals: HybridSignalScores;
}
export interface HybridSearchOut {
  items: HybridSearchItem[];
  weights_used: Record<string, number>;
  query: string;
}

export interface AvailableAiModel {
  model_id: string;
  display_name: string;
  provider: string;
  tier_hint: "free" | "standard" | "premium";
  is_in_house: boolean;
}
export interface AiModelsBundle {
  available: AvailableAiModel[];
  current_tier: "free" | "standard" | "premium";
  current_default_model_id: string;
}
export const aiModelsApi = {
  list: () => request<AiModelsBundle>("/api/me/ai-models"),
};

export interface TextChunkBySource {
  source_kind: string;
  total: number;
  embedded: number;
  pending: number;
}
export interface TextChunkCoverage {
  total_chunks: number;
  embedded_chunks: number;
  pending_chunks: number;
  pct: number;
  by_source_kind: TextChunkBySource[];
  model_id: string;
}

export interface LLMProviderStatus {
  name: string;
  configured: boolean;
  description: string;
  note: string | null;
}
export interface LLMTierDefault {
  tier: "free" | "standard" | "premium";
  provider_kind: string;
  model_id: string;
  is_callable: boolean;
}
export interface LLMProviderStatusBundle {
  providers: LLMProviderStatus[];
  tier_defaults: LLMTierDefault[];
}

export const searchApi = {
  run: (params: SearchParams) =>
    request<Paginated<Study>>(`/api/search${qs(params as Record<string, QSValue>)}`),
  similarTo: (targetId: string, params: { k?: number; modality?: string } = {}) =>
    request<SimilarStudy[]>(`/api/similar-to/${targetId}${qs(params as Record<string, QSValue>)}`),
  hybrid: (params: {
    q: string;
    k?: number;
    weights?: string;
    scope?: "all" | "public" | "mine";
  }) => request<HybridSearchOut>(`/api/search/hybrid${qs(params as Record<string, QSValue>)}`),
};

// -------- measurements --------

/**
 * Wire shape returned by ``GET / POST /api/series/{id}/measurements``.
 * Backed by the unified ``markers`` table; the API flattens the
 * Marker geometry/computed/body columns into the ``payload`` blob the
 * viewer reads directly.
 */
export interface MeasurementPayload {
  tool: "distance" | "angle" | "area";
  points: Array<{ x: number; y: number }>;
  value: number;
  unit: "mm" | "deg" | "mm2";
  label?: string | null;
  slice_index?: number | null;
  viewport?: string | null;
  client_id?: string | null;
}

export interface MeasurementRow {
  id: string;
  target_kind: "study" | "series" | "instance";
  target_id: string;
  author_subject_id: string | null;
  payload: Record<string, unknown> & Partial<MeasurementPayload>;
  created_at: string;
  updated_at: string;
}

export const measurementsApi = {
  list: (seriesId: string) => request<MeasurementRow[]>(`/api/series/${seriesId}/measurements`),
  upsert: (seriesId: string, measurements: MeasurementPayload[], replace = false) =>
    request<MeasurementRow[]>(`/api/series/${seriesId}/measurements`, {
      method: "POST",
      json: { measurements, replace },
    }),
  remove: (id: string) => request<void>(`/api/measurements/${id}`, { method: "DELETE" }),
  exportSr: (seriesId: string) =>
    request<Record<string, unknown>>(`/api/series/${seriesId}/measurements.sr`),
  exportSrUrl: (seriesId: string) => `${API_BASE_URL}/api/series/${seriesId}/measurements.sr`,
};

// -------- reports --------

export interface Report {
  id: string;
  study_id: string;
  author_subject_id: string | null;
  version: number;
  text: string;
  file_s3_key: string | null;
  file_content_type: string | null;
  created_at: string;
}

export const reportsApi = {
  list: (studyId: string) => request<Report[]>(`/api/studies/${studyId}/reports`),
  // Passing FormData as body makes the browser set the multipart boundary.
  create: (studyId: string, fd: FormData) =>
    request<Report>(`/api/studies/${studyId}/reports`, { method: "POST", body: fd }),
};

/** Forward-direction view of a document linked to a study via
 *  ``DocumentStudyLink``. Powers the "Documenti collegati" panel on
 *  the study detail page. */
export interface StudyDocumentLink {
  document_id: string;
  document_title: string;
  document_kind: string;
  document_date: string | null;
  document_text_preview: string | null;
  has_attachment: boolean;
  link_kind: string;
  created_at: string;
  created_by_subject_id: string | null;
}

/** Canonical link kinds (post-migration 0089). ``primary_report`` is
 *  unique-per-study via partial index; the others are unconstrained. */
export const STUDY_DOCUMENT_LINK_KINDS = [
  "primary_report",
  "addendum",
  "second_opinion",
  "extracted_from",
  "cites",
  "mentions",
] as const;
export type StudyDocumentLinkKind = (typeof STUDY_DOCUMENT_LINK_KINDS)[number];

export const studyDocumentLinksApi = {
  /** List documents attached to a study. */
  list: (patientId: string, studyId: string) =>
    request<StudyDocumentLink[]>(`/api/patients/${patientId}/studies/${studyId}/document-links`),
  /** Attach an existing document to the study. Idempotent on the
   *  ``(document_id, study_id, link_kind)`` triple; a duplicate
   *  ``primary_report`` returns 409 with the existing link in
   *  ``problem.detail.existing_primary``. */
  create: (
    patientId: string,
    documentId: string,
    studyId: string,
    linkKind: StudyDocumentLinkKind,
  ) =>
    request<{
      id: string;
      document_id: string;
      study_id: string;
      link_kind: string;
      created_at: string;
    }>(`/api/patients/${patientId}/documents/${documentId}/links`, {
      method: "POST",
      json: { study_id: studyId, link_kind: linkKind },
    }),
  /** Detach a document from a study. Idempotent: a non-existent
   *  triple returns 204. */
  remove: (patientId: string, documentId: string, studyId: string, linkKind: string) =>
    request<void>(
      `/api/patients/${patientId}/documents/${documentId}/links/${studyId}/${linkKind}`,
      { method: "DELETE" },
    ),
};

// -------- patients --------

export interface PatientContact {
  /** Stable opaque ID assigned by the backend on first write. Used to
   *  address contacts in the delegation + CRUD endpoints. Optional
   *  only on legacy rows that haven't been re-saved since the field
   *  landed. */
  id?: string | null;
  label: string;
  relationship?: string | null;
  email?: string | null;
  phone?: string | null;
  /** Free-text per-contact note. */
  notes?: string | null;
  /** Marks the default primary contact for the patient. The backend
   *  enforces at most one primary per patient. */
  is_primary?: boolean;
  /** Explicit GDPR consent: the contact agreed to be contacted by
   *  the clinic on the patient's behalf. */
  consent_to_contact?: boolean;
  /** Set when the contact has been promoted to a fascicolo delegate.
   *  ``delegation_level`` mirrors the access level granted on the
   *  underlying ShareLink so the UI can render the right badge
   *  without an extra round-trip. */
  delegation_subject_id?: string | null;
  delegation_share_link_id?: string | null;
  delegation_level?: "viewer" | "editor" | "manager" | null;
  // ---- Notification channels (v3.5 sprint C/D) ----------------------
  /** Ordered list of preferred channels the dispatcher tries. */
  preferred_channels?: string[] | null;
  preferred_locale?: string;
  telegram_chat_id?: string | null;
  whatsapp_phone?: string | null;
  webhook_url?: string | null;
  consent_email?: boolean;
  consent_telegram?: boolean;
  consent_whatsapp?: boolean;
  consent_webhook?: boolean;
  /** ``active`` / ``bounced`` / ``suppressed`` / ``unsubscribed`` —
   *  driven by Scaleway TEM bounce webhook. */
  email_delivery_state?: "active" | "bounced" | "suppressed" | "unsubscribed";
}

export interface ContactDelegateRequest {
  access_level: "viewer" | "editor" | "manager";
  expires_in_hours?: number | null;
  autogen_password?: boolean;
  password?: string | null;
}

export interface ContactDelegateResponse {
  contact_id: string;
  delegation_subject_id: string;
  delegation_share_link_id: string;
  delegation_share_link_token: string;
  delegation_level: string;
  expires_at: string | null;
  /** Plaintext password — returned ONCE (autogen path) and never
   *  again. Capture it on the response and surface it once with a
   *  copy-to-clipboard affordance. */
  generated_password: string | null;
  /** URL the operator should deliver to the recipient OOB. */
  share_url: string;
}

export interface Patient {
  id: string;
  display_name: string;
  external_id: string | null;
  birth_date: string | null;
  sex: string | null;
  tax_id: string | null;
  phone: string | null;
  email: string | null;
  address: string | null;
  blood_type: string | null;
  birth_place_city: string | null;
  birth_place_province: string | null;
  asl_code: string | null;
  asl_name: string | null;
  allergies: string | null;
  notes: string | null;
  /** Provenance for the ``notes`` field. Bumped only when notes
   *  change (not on a demographics-only PATCH), so the sticky
   *  panel can render "Aggiornata da X · 2 ore fa" without
   *  conflating unrelated edits. NULL on rows whose notes have
   *  never been edited since backend migration 0094 landed. */
  notes_updated_at?: string | null;
  notes_updated_by_display_name?: string | null;
  contacts: PatientContact[];
  managed_by_subject_id: string | null;
  self_user_subject_id: string | null;
  /**
   * Caller-relative classification computed server-side (see
   * patients._patient_origin):
   *   "mine"   — caller manages or *is* the patient
   *   "shared" — visible via a Grant only
   *   "public" — open-data dataset owned by the platform owner
   * Null only on unauthenticated reads (the list endpoint requires auth).
   */
  origin: "mine" | "shared" | "public" | null;
  created_at: string;
  /** Concurrency token. Hex of the latest commit on the patient's
   *  main branch; the backend refuses ``PATCH /patients/{id}``
   *  unless the request carries it back as ``If-Match``. */
  etag: string | null;
}

export type PatientScope = "personal" | "mine" | "shared" | "public" | "all";

/**
 * Voxel anchor pinned to a specific ``(x, y, z)`` of the active series
 * volume. ``null`` for plain text notes that don't reference a voxel.
 * Populated when the note was authored from inside the viewer.
 */
export interface NoteAnchor {
  x: number;
  y: number;
  z: number;
}

export interface ClinicalNote {
  id: string;
  patient_id: string;
  target_kind: "study" | "series" | "document" | "consultation" | "patient";
  target_id: string;
  author_subject_id: string;
  author_kind: "human" | "agent";
  /**
   * Hard boolean: true iff the note was authored by an AI agent.
   * Render with a treatment that cannot be confused with human
   * authorship (banner, color, icon — never just text).
   */
  is_ai_generated: boolean;
  model_id: string | null;
  provider: string | null;
  agent_token_id: string | null;
  body: string;
  pinned: boolean;
  anchor: NoteAnchor | null;
  created_at: string;
  updated_at: string;
}

export interface PatientDocumentFile {
  id: string;
  sequence: number;
  file_content_type: string | null;
  original_filename: string | null;
  size_bytes: number | null;
  created_at: string;
}

export interface PatientDocument {
  id: string;
  patient_id: string;
  uploaded_by_subject_id: string | null;
  /** v3: ``document_type`` is an alias of ``kind_id`` kept for the
   * legacy slice. Prefer the explicit 3-axis fields below. */
  document_type: string;
  kind_id: string;
  provenance_id: string;
  authority_id: string;
  content_sha256: string | null;
  original_blob_hash: string | null;
  title: string;
  text: string | null;
  file_s3_key: string | null;
  file_content_type: string | null;
  document_date: string | null;
  created_at: string;
  /**
   * Multi-file gallery. Empty for legacy single-file rows (where the
   * binary lives in the top-level ``file_s3_key``); populated when a
   * single document carries a list of scans / pages / images.
   */
  files: PatientDocumentFile[];
  etag: string | null;
  /**
   * Number of folders that contain this document (hardlink count).
   * Always ≥ 1 for live documents post-0088 (no-orphan invariant).
   * The card UI surfaces a chain-link badge when ≥ 2 so the user
   * understands it's the *same* document, not a duplicate.
   */
  folder_count?: number;
  /**
   * True iff the document only lives under the patient root folder
   * (no user-filed copy yet). UI uses this to mark cards that the
   * user might want to file into a more specific folder.
   */
  is_in_root_only?: boolean;
}

/** Reverse-direction reference inventory for the "Riferito da" panel. */
export interface DocumentReferences {
  studies: Array<{
    study_id: string;
    link_kind: string;
    created_at: string;
    created_by_subject_id: string | null;
  }>;
  report_contents: Array<{
    report_content_id: string;
    role: string;
    excerpt: string | null;
    clinical_event_id: string | null;
  }>;
  citations: Array<{
    report_content_id: string;
    citation_id: string;
    page: number | null;
    excerpt: string | null;
  }>;
  folders: Array<{
    folder_id: string;
    name: string;
    is_root: boolean;
  }>;
}

/** Structured 409 payload returned when a delete is blocked by active
 *  references. The same shape is used by the ``BlockingReferencesModal``. */
export interface BlockingReference {
  kind: "study_link" | "content_link" | "citation";
  id: string;
  label: string;
  detail_url: string;
  extra: Record<string, unknown>;
}

export interface TimelineItem {
  type: "study" | "report" | "annotation" | "document";
  date: string;
  data: Record<string, unknown>;
}

export interface FascicoloSection {
  key: string;
  label: string;
  count: number;
  last_date: string | null;
  breakdown: Record<string, number> | null;
}

export interface FascicoloIndex {
  patient: Patient;
  sections: FascicoloSection[];
  total_items: number;
}

export interface ShareLink {
  id: string;
  token: string;
  url: string;
  label: string | null;
  permissions: string[];
  expires_at: string | null;
  revoked: boolean;
  use_count: number;
  max_uses: number | null;
  requires_password: boolean;
  created_at: string;
  recipient_name?: string | null;
  recipient_email?: string | null;
  /** Set ONCE on POST when ``autogen_password`` was requested. */
  generated_password?: string | null;
  /** True when the underlying grant scrubs PHI on download. */
  deidentify?: boolean;
  /** ISO timestamp of the recipient's "ho ricevuto" confirmation. */
  received_at?: string | null;
  /** Count of complete (200 full-body) downloads — distinct from
   *  ``use_count`` which tracks landing-page accesses. */
  download_count?: number;
  resource_kind?: string;
  resource_id?: string;
  grantor_subject_id?: string | null;
  /** Pre-export cache snapshot (study scope only today). */
  prepared_job_id?: string | null;
  prepared_status?: string | null;
  prepared_progress_done?: number | null;
  prepared_progress_total?: number | null;
  revoked_at?: string | null;
  /** Optional AI sponsorship cap in cents. When set, claiming this
   *  link auto-creates a WalletSponsorship of the share creator's
   *  wallet so the recipient can run AI on the shared record at the
   *  creator's expense up to this cap. */
  ai_sponsorship_cap_cents?: number | null;
  ai_sponsorship_id?: string | null;
}

export interface ShareLinkList {
  items: ShareLink[];
}

export const patientsApi = {
  list: (
    params: {
      limit?: number;
      offset?: number;
      q?: string;
      scope?: PatientScope;
      /** Tag filter, format ``namespace:value``. */
      tag?: string;
    } = {},
  ) => request<Paginated<Patient>>(`/api/patients${qs(params as Record<string, QSValue>)}`),
  detail: (id: string) => request<Patient>(`/api/patients/${id}`),
  create: (input: {
    display_name: string;
    external_id?: string | null;
    birth_date?: string | null;
    sex?: string | null;
    tax_id?: string | null;
    phone?: string | null;
    email?: string | null;
    address?: string | null;
    blood_type?: string | null;
    allergies?: string | null;
    notes?: string | null;
  }) => request<Patient>("/api/patients", { method: "POST", json: input }),
  update: (id: string, input: Record<string, unknown>, etag?: string | null) => {
    // ``If-Match`` is mandatory on the backend for the legacy patient
    // PATCH (it commits to the patient's main branch under optimistic
    // concurrency). Caller passes the ``etag`` from the latest detail
    // read; without it the server replies 428 "If-Match header is
    // required for this mutation" and the user sees an opaque error.
    const headers = new Headers();
    if (etag) headers.set("if-match", `"${etag}"`);
    return request<Patient>(`/api/patients/${id}`, {
      method: "PATCH",
      json: input,
      headers,
    });
  },
  remove: (id: string) => request<void>(`/api/patients/${id}`, { method: "DELETE" }),
  /** Promote a contact to a fascicolo delegate. The recipient claims
   *  a real account via ``share_url`` + the one-time password
   *  returned in ``generated_password``. */
  delegateContact: (patientId: string, contactId: string, body: ContactDelegateRequest) =>
    request<ContactDelegateResponse>(`/api/patients/${patientId}/contacts/${contactId}/delegate`, {
      method: "POST",
      json: body,
    }),
  /** Revoke an active delegation. The contact stays as informational
   *  on the row; only the underlying Grant is dropped. */
  revokeContactDelegation: (patientId: string, contactId: string) =>
    request<void>(`/api/patients/${patientId}/contacts/${contactId}/delegate`, {
      method: "DELETE",
    }),
  // ---- Notification channels (v3.5 sprint D) -------------------------
  configureContactChannel: (
    patientId: string,
    contactId: string,
    body: {
      preferred_locale?: string;
      telegram_chat_id?: string | null;
      whatsapp_phone?: string | null;
      webhook_url?: string | null;
      consent_email?: boolean;
      consent_telegram?: boolean;
      consent_whatsapp?: boolean;
      consent_webhook?: boolean;
      append_preferred_channel?: string;
    },
  ) =>
    request<{
      contact_id: string;
      preferred_channels: string[];
      preferred_locale: string;
      telegram_chat_id: string | null;
      whatsapp_phone: string | null;
      webhook_url: string | null;
      consent_email: boolean;
      consent_telegram: boolean;
      consent_whatsapp: boolean;
      consent_webhook: boolean;
      email_delivery_state: string;
    }>(`/api/patients/${patientId}/contacts/${contactId}/configure-channel`, {
      method: "POST",
      json: body,
    }),
  revokeContactChannel: (patientId: string, contactId: string, channel: string) =>
    request<{
      contact_id: string;
      channel: string;
      consent_to_contact: boolean;
      consent_email: boolean;
      consent_telegram: boolean;
      consent_whatsapp: boolean;
      consent_webhook: boolean;
    }>(`/api/patients/${patientId}/contacts/${contactId}/revoke-consent`, {
      method: "POST",
      json: { channel },
    }),
  startTelegramLink: (patientId: string, contactId: string) =>
    request<{ code: string; deep_link_url: string; expires_at: string }>(
      `/api/patients/${patientId}/contacts/${contactId}/telegram-link/start`,
      { method: "POST", json: {} },
    ),
  pollTelegramLink: (patientId: string, contactId: string) =>
    request<{
      status: "pending" | "linked" | "expired" | "none";
      code?: string;
      deep_link_url?: string;
      expires_at?: string;
      chat_linked: boolean;
    }>(`/api/patients/${patientId}/contacts/${contactId}/telegram-link/status`),
  unlinkTelegram: (patientId: string, contactId: string) =>
    request<{
      status: string;
      contact_id: string;
      telegram_chat_id: string | null;
    }>(`/api/patients/${patientId}/contacts/${contactId}/telegram-link/unlink`, {
      method: "POST",
      json: {},
    }),
  setWebhookSecret: (patientId: string, contactId: string, secret: string) =>
    request<{ contact_id: string; has_webhook_secret: true }>(
      `/api/patients/${patientId}/contacts/${contactId}/webhook-secret`,
      { method: "POST", json: { secret } },
    ),
  clearWebhookSecret: (patientId: string, contactId: string) =>
    request<{ contact_id: string; has_webhook_secret: false }>(
      `/api/patients/${patientId}/contacts/${contactId}/webhook-secret`,
      { method: "DELETE" },
    ),
  sendTestNotification: (patientId: string, contactId: string, channel: string) =>
    request<{ id: string; status: string; channel: string }>(
      `/api/patients/${patientId}/notifications/test`,
      { method: "POST", json: { contact_id: contactId, channel } },
    ),
  index: (id: string) => request<FascicoloIndex>(`/api/patients/${id}/index`),
  timeline: (id: string, params: { section?: string; limit?: number; offset?: number } = {}) =>
    request<TimelineItem[]>(`/api/patients/${id}/timeline${qs(params as Record<string, QSValue>)}`),
  listDocuments: (id: string, type?: string) =>
    request<PatientDocument[]>(
      `/api/patients/${id}/documents${qs({ type } as Record<string, QSValue>)}`,
    ),
  /** Single document: metadata + inline ``text`` body when present. */
  getDocument: (patientId: string, docId: string) =>
    request<PatientDocument>(`/api/patients/${patientId}/documents/${docId}`),
  /**
   * Edit document metadata in place. Most common use: a paper report
   * acquired in a previous year is scanned and uploaded today —
   * ``document_date`` lets the user record the original clinical date
   * even though ``created_at`` is "now". Title / type / inline text
   * are also editable; multi-file gallery is not touched.
   */
  updateDocument: (
    patientId: string,
    docId: string,
    input: {
      title?: string;
      document_type?: string;
      document_date?: string | null;
      text?: string | null;
    },
  ) =>
    request<PatientDocument>(`/api/patients/${patientId}/documents/${docId}`, {
      method: "PATCH",
      json: input,
    }),
  /**
   * Absolute URL for the document content endpoint. The server returns
   * either a 307 redirect to a presigned S3 URL (PDF/image) or the raw
   * bytes inline (small text/markdown). Used by DocumentPreview.
   */
  documentContentUrl: (patientId: string, docId: string) =>
    `${API_BASE_URL}/api/patients/${patientId}/documents/${docId}/content`,
  /**
   * Absolute URL for the document thumbnail endpoint. Returns a JPEG
   * (PDF first page or downscaled image) or 404 for unsupported
   * kinds (text/markdown/inline-only). Cached for 1 day client-side.
   */
  documentThumbnailUrl: (patientId: string, docId: string, maxSide = 256) =>
    `${API_BASE_URL}/api/patients/${patientId}/documents/${docId}/thumbnail?max_side=${maxSide}`,
  /**
   * Enqueue an async fascicolo export Job. Returns the Job descriptor
   * (poll ``GET /api/jobs/{id}`` for progress + the presigned download
   * URL on ``result_download_url``). Preferred over the legacy
   * ``exportFascicoloAsBlob`` because the work survives a client
   * disconnect and is bounded by the server-side per-user job cap.
   */
  requestFascicoloExport: (
    patientId: string,
    include?: ReadonlyArray<"studies" | "reports" | "documents" | "annotations" | "dicom">,
  ) =>
    request<import("../jobs").JobOut>(`/api/patients/${encodeURIComponent(patientId)}/export`, {
      method: "POST",
      json: { include: include && include.length > 0 ? include.join(",") : null },
    }),
  /** URL of one file in a multi-file gallery document. */
  documentFileContentUrl: (patientId: string, docId: string, fileId: string) =>
    `${API_BASE_URL}/api/patients/${patientId}/documents/${docId}/files/${fileId}/content`,
  /**
   * Trigger a download of the document's underlying blob.
   *
   * Streaming-to-disk via native anchor click — same UX as GitHub
   * Releases, GDrive single files, every other web app. No fetch +
   * Blob in the browser, no 2 GiB cap, no RAM pressure on multi-GiB
   * DVD ISOs.
   *
   * Auth flow: ``POST /api/auth/download-token`` mints a 5-minute
   * single-use token bound to (kind, id, child_id). The token is
   * appended as ``?dt=<token>`` and the browser navigates to the
   * URL via a synthetic anchor click — Chrome / Firefox / Safari all
   * stream the response body straight to disk. The persistent JWT
   * never appears in URLs / referrers / proxy logs.
   *
   * On error, throws ``ApiError`` with the parsed problem details.
   * Storage isolation invariant (memory
   * ``feedback_storage_isolation``) preserved end-to-end: the
   * backend proxies the bytes, the bucket / key never leak.
   */
  downloadDocument: async (docId: string, suggestedFilename?: string) => {
    const url = await mintDownloadUrl(`${API_BASE_URL}/api/documents/${docId}/download`, {
      resource_kind: "document",
      resource_id: docId,
    });
    triggerDownload(url, suggestedFilename);
  },
  /**
   * Same as ``downloadDocument`` but for a child file inside a
   * multi-file document — e.g. one page of a 5-scan paper report,
   * or one DocumentFile child of an ISO bundle.
   */
  downloadDocumentFile: async (docId: string, fileId: string, suggestedFilename?: string) => {
    const url = await mintDownloadUrl(
      `${API_BASE_URL}/api/documents/${docId}/files/${fileId}/download`,
      {
        resource_kind: "document_file",
        resource_id: docId,
        child_id: fileId,
      },
    );
    triggerDownload(url, suggestedFilename);
  },
  createShare: (id: string, input: Record<string, unknown>) =>
    request<ShareLink>(`/api/patients/${id}/share`, { method: "POST", json: input }),
  listShares: (id: string) => request<ShareLink[]>(`/api/patients/${id}/shares`),
  /** Cross-patient list of share-links the caller created. Filters:
   *  ``patient_id`` narrows to one fascicolo (matches both patient-
   *  scoped grants and study-scoped grants whose study sits under
   *  the patient); ``include_revoked`` / ``include_expired`` let
   *  the lista show terminal rows for audit. */
  listMyShares: (
    params: {
      patient_id?: string;
      include_revoked?: boolean;
      include_expired?: boolean;
      limit?: number;
    } = {},
  ) => request<ShareLinkList>(`/api/share-links${qs(params as Record<string, QSValue>)}`),
  /** Roll forward the share-link expiry by N 30-day blocks (1, 3,
   *  6, or 12 in the UI). Idempotency lives on the server-side
   *  audit; calling twice extends twice (the user sees the new
   *  ``expires_at`` and decides). */
  extendShare: (linkId: string, addMonths: 1 | 3 | 6 | 12) =>
    request<ShareLink>(`/api/share-links/${encodeURIComponent(linkId)}/extend`, {
      method: "POST",
      json: { add_months: addMonths },
    }),
  /**
   * Revoke a share link by id. Backend route is global (not scoped to
   * the patient) because share links are also used for study-level
   * sharing; access control is enforced server-side by checking the
   * caller is the grantor. Soft revoke — the row stays visible as
   * "revoked" so the audit trail is preserved.
   */
  revokeShare: (linkId: string) =>
    request<void>(`/api/share-links/${linkId}`, { method: "DELETE" }),
  /**
   * Hard-delete an already-revoked share link (purges the row + its
   * grant). Backend rejects with 409 if the link isn't revoked first.
   */
  purgeShare: (linkId: string) =>
    request<void>(`/api/share-links/${linkId}?purge=true`, { method: "DELETE" }),
  /**
   * Edit an active share link in place. Useful for rotating the
   * password, extending the validity window, or broadening / narrowing
   * access. Pass only the fields you want to change. ``password = ""``
   * clears the password; ``password = null`` leaves it unchanged.
   */
  updateShare: (
    linkId: string,
    input: {
      label?: string | null;
      access_level?: "viewer" | "editor" | "manager";
      download?: boolean;
      expires_in_hours?: number | null;
      max_uses?: number | null;
      password?: string | null;
    },
  ) =>
    request<ShareLink>(`/api/share-links/${linkId}`, {
      method: "PATCH",
      json: input,
    }),

  /**
   * Clinical notes: free-text per-item commentary. Without ``params``
   * the response is the aggregated evidence view (every note under
   * the patient); with ``target_kind`` + ``target_id`` it scopes to a
   * specific study / document / consultation / series / patient.
   */
  listNotes: (
    patientId: string,
    params: { target_kind?: string; target_id?: string; limit?: number } = {},
  ) =>
    request<ClinicalNote[]>(
      `/api/patients/${patientId}/notes${qs(params as Record<string, QSValue>)}`,
    ),
  createNote: (
    patientId: string,
    input: {
      target_kind: string;
      target_id: string;
      body: string;
      pinned?: boolean;
      /** Voxel anchor — populate when the note is created inside the viewer. */
      anchor?: NoteAnchor | null;
    },
  ) =>
    request<ClinicalNote>(`/api/patients/${patientId}/notes`, {
      method: "POST",
      json: input,
    }),
  updateNote: (
    patientId: string,
    noteId: string,
    input: {
      body?: string;
      pinned?: boolean;
      /** ``null`` clears the anchor; omit to leave it untouched. */
      anchor?: NoteAnchor | null;
    },
  ) =>
    request<ClinicalNote>(`/api/patients/${patientId}/notes/${noteId}`, {
      method: "PATCH",
      json: input,
    }),
  deleteNote: (patientId: string, noteId: string) =>
    request<void>(`/api/patients/${patientId}/notes/${noteId}`, {
      method: "DELETE",
    }),
  /**
   * Move one or more items into a target folder (or to the patient root when
   * `target_folder_id` is null). Accepts a batch so multi-select drags are one
   * round-trip; the backend is expected to be transactional on a per-request
   * basis. Matching endpoint: `POST /api/patients/{id}/tree/move`.
   */
  treeMove: (id: string, input: TreeMoveInput) =>
    request<TreeMoveResult>(`/api/patients/${id}/tree/move`, {
      method: "POST",
      json: input,
    }),
};

// -------- public iCal calendar subscriptions --------

/**
 * A revocable, non-expiring public calendar feed handle. ``feed_url``
 * is the absolute URL to paste into Google / Apple Calendar; the HMAC
 * token lives inside the path, so this object is no more sensitive than
 * the URL the owner is about to share. ``revoked_at`` non-null means the
 * URL is dead (kept for audit).
 */
export interface CalendarSubscription {
  id: string;
  patient_id: string;
  label: string | null;
  author_kind: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_accessed_at: string | null;
  access_count: number;
  feed_path: string;
  feed_url: string;
}

export const calendarSubscriptionsApi = {
  /** Active handles for a patient (pass includeRevoked for the audit view). */
  list: (patientId: string, includeRevoked = false) =>
    request<CalendarSubscription[]>(
      `/api/patients/${patientId}/calendar/subscriptions${
        includeRevoked ? "?include_revoked=true" : ""
      }`,
    ),

  /**
   * Mint a public feed URL. The backend mandates an ``Idempotency-Key``
   * (a double-clicked button must not produce two leakable URLs); we
   * generate one per invocation.
   */
  create: (patientId: string, label?: string | null) => {
    const headers = new Headers();
    headers.set(
      "idempotency-key",
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    );
    return request<CalendarSubscription>(`/api/patients/${patientId}/calendar/subscriptions`, {
      method: "POST",
      json: { label: label ?? null },
      headers,
    });
  },

  /** Revoke a handle (soft by default; the URL stops working at once). */
  revoke: (patientId: string, subscriptionId: string) =>
    request<void>(`/api/patients/${patientId}/calendar/subscriptions/${subscriptionId}`, {
      method: "DELETE",
    }),
};

// -------- patient tree (Drive-style fascicolo) --------

/**
 * One node in the Drive-style tree. Folders can contain children; leaf items
 * (study/document/report/annotation/consultation) link to their own viewer.
 * `path` is a POSIX-style slash-joined key used for navigation + breadcrumbs.
 */
export type TreeNodeType =
  | "folder"
  | "study"
  | "series"
  | "document"
  | "report"
  | "annotation"
  | "consultation"
  | "pathology_slide";

export interface TreeNode {
  id: string;
  type: TreeNodeType;
  name: string;
  path: string;
  parent_path: string | null;
  /** Leaf-only: id of the underlying resource. For folders, null. */
  target_id: string | null;
  item_count: number | null;
  size_bytes: number | null;
  mime_type: string | null;
  /**
   * Document-only: the document_type slug (LOINC-aligned) the
   * backend stores for fascicolo classification ("er_report",
   * "imaging_report", "clinical_note", ...). Rendered as a chip
   * under the title in the grid card.
   */
  document_type?: string | null;
  date: string | null;
  /** ISO timestamp of the underlying resource's creation. Folders use
   *  ``folder.created_at``; for studies/documents this is the upload
   *  time (distinct from the clinical ``date``). Drives the
   *  "Creazione" sort mode. */
  created_at?: string | null;
  /** ISO timestamp of the last server-side update of the underlying
   *  resource (rename, metadata edit, ...). Populated for folders,
   *  studies, documents, consultations. Null for series / reports /
   *  annotations whose models don't track it. Drives the "Modifica"
   *  sort mode and the secondary date stamp on each card. */
  updated_at?: string | null;
  /**
   * Set on study nodes when the backend has identified a series whose
   * thumbnail is worth showing as the card cover. The full URL is
   * ``${API_BASE_URL}/api/series/${thumbnail_series_id}/thumbnail``.
   */
  thumbnail_series_id?: string | null;
  modality?: string | null;
  /** Per-card aggregates rendered on study/series previews:
   *   - study: ``series_count`` + total ``instance_count``;
   *   - series: ``instance_count`` of that series.
   * Other node kinds leave them null. */
  series_count?: number | null;
  instance_count?: number | null;
  /**
   * Folder-only: 0..4 representative children rendered as a stack
   * behind the folder icon in the grid view. Each entry mirrors a
   * thin slice of the child node (kind + name + optional thumbnail
   * series id for studies, modality for studies/series).
   */
  preview?: FolderPreviewEntry[] | null;
  /**
   * Folder-only: per-kind count of direct children (excluding sub-
   * folder markers and the patient placeholder). Drives the kind-
   * dominant icon ("studio + referto", "documento", ...) without
   * re-walking ``preview``.
   *
   * Note: only counts kinds visible in ``preview`` (capped at 8 by
   * the row-numbered query that builds the stack). Use
   * ``kind_counts`` for the full breakdown rendered in the hover
   * tooltip.
   */
  preview_kinds?: Record<string, number> | null;
  /**
   * Folder-only: full per-kind aggregate of every child of this
   * folder, ignoring the preview-stack cap. Mirrors ``item_count``
   * but split by kind. Used by the grid hover preview to render
   * lines like "3 studi · 2 PDF · 1 nota".
   */
  kind_counts?: Record<string, number> | null;
  /**
   * Folder-only: recursive aggregate — every entity reachable through
   * the sub-folder tree (depth capped backend-side at 5), including a
   * ``"folder"`` bucket for the nested folders themselves. The grid
   * prefers this over ``kind_counts`` when populated so a parent like
   * ``2024/`` shows what's actually buried in ``2024/TAC/`` etc.
   */
  recursive_kind_counts?: Record<string, number> | null;
  /**
   * Folder-only: number of (study, report) pairs reachable below the
   * folder (via DocumentStudyLink.report_of). Lets the UI render
   * "3 esami refertati" instead of summing studies + documents.
   */
  paired_study_report_count?: number | null;
  /**
   * Folder-only: short description rendered in the grid hover
   * preview. Capped at 500 chars by the backend; supports Markdown
   * (the FE renders bold / italic / lists). For longer commentary
   * use ``narrative_md`` below.
   */
  description?: string | null;
  /**
   * Folder-only: free-form Markdown commentary on the folder's
   * clinical context. No length cap. Rendered in the folder detail
   * panel; not surfaced in the hover tooltip (keeps it compact).
   */
  narrative_md?: string | null;
  /**
   * Folder-only: optional clinical / display date the folder
   * represents in the patient timeline (distinct from
   * ``created_at`` system audit). When set the FE prefers it for
   * the card date label.
   */
  clinical_date?: string | null;
  /** Only populated when the endpoint is asked to recurse. */
  children?: TreeNode[];
  /**
   * Document-only: number of folders that contain this document
   * (hardlink count). Always ≥ 1 for live documents post-0088. The
   * grid card shows a chain-link badge when ≥ 2 so the user knows
   * the same document is reachable from multiple folders (avoiding
   * the "this looks duplicate" confusion).
   */
  folder_count?: number | null;
  /** Document-only: true iff the only containment is the patient root. */
  is_in_root_only?: boolean | null;
  /**
   * Study-only: provenance + license for OpenData public-dataset
   * imports (tier=t4). Populated by ``_study_node`` so <LicenseBadge>
   * can render directly on each study card without an extra per-card
   * fetch. NULL on user-uploaded private studies.
   */
  source_collection?: string | null;
  license_spdx?: string | null;
  license_url?: string | null;
  citation_required?: boolean | null;
  citation_text?: string | null;
  /**
   * Pathology-slide only: stain + magnification render as chips on
   * the card; source_format hints at SVS / NDPI / OME-TIFF for the
   * future viewer. All NULL on non-pathology nodes.
   */
  stain?: string | null;
  magnification?: number | null;
  source_format?: string | null;
  has_macro?: boolean | null;
}

export interface FolderPreviewEntry {
  type: TreeNodeType;
  name: string;
  modality?: string | null;
  thumbnail_series_id?: string | null;
  /** Document UUID + MIME so the peek tile can fetch the rendered
   *  thumbnail (PDF first page / downscaled image) instead of showing
   *  a generic icon that occludes the stacked studies behind it. */
  target_id?: string | null;
  mime_type?: string | null;
  /**
   * Chain of intermediate folder names when the entry was promoted
   * from a sub-folder by the recursive preview. Empty / undefined for
   * direct children of the parent.
   */
  via_folder_path?: string[] | null;
  /** When ``type === "folder"``: total descendants in that sub-tree. */
  folder_descendant_count?: number | null;
}

export interface BreadcrumbSegment {
  name: string;
  path: string;
  /**
   * Folder UUID, or ``null`` for the synthetic patient-root segment.
   * Lets callers distinguish "we are at the root" (no folder context)
   * from a real folder hop without having to compare ``path`` to "/".
   */
  id?: string | null;
}

export interface TreeListing {
  path: string;
  /**
   * The id of the folder we are listing (null at the fascicolo root). The
   * backend takes this as the canonical identity of "current folder";
   * createFolder needs it as ``parent_folder_id``.
   */
  folder_id: string | null;
  breadcrumb: BreadcrumbSegment[];
  nodes: TreeNode[];
  /**
   * Current folder surfaced as a ``TreeNode`` so the FE can render
   * ``description`` / ``narrative_md`` / ``clinical_date`` in a header
   * strip without a second round-trip. ``null`` at the patient root
   * (no folder context) and for shared-link / anonymous callers.
   */
  current_folder?: TreeNode | null;
}

export const patientTreeApi = {
  /** List children at `path`. Backend may return 404 until F2 ships. */
  tree: (patientId: string, path = "/") =>
    request<TreeListing>(`/api/patients/${patientId}/tree${qs({ path })}`),
  /**
   * Breadcrumb leading from the patient root to the *parent folder* of
   * a leaf item (study / document / report / consultation), or to a
   * folder itself. The last segment is the immediate parent (or the
   * folder itself); use its ``path`` to navigate the fascicolo back to
   * the place the user was browsing when they opened the leaf.
   */
  breadcrumbForItem: (
    patientId: string,
    item_kind: "folder" | "study" | "series" | "document" | "report" | "consultation",
    item_id: string,
  ) =>
    request<BreadcrumbSegment[]>(
      `/api/patients/${patientId}/tree/breadcrumb${qs({ item_kind, item_id })}`,
    ),
  /**
   * Create a new folder under ``parentFolderId`` (null = patient root).
   * Backend route is singular ``/tree/folder`` and takes the parent's
   * folder UUID, not a path string.
   */
  createFolder: (
    patientId: string,
    parentFolderId: string | null,
    name: string,
    description?: string | null,
  ) =>
    request<TreeNode>(`/api/patients/${patientId}/tree/folder`, {
      method: "POST",
      json: {
        parent_folder_id: parentFolderId,
        name,
        ...(description !== undefined ? { description } : {}),
      },
    }),
  /**
   * Move an existing node to a new parent folder. The backend
   * accepts ``{item_kind, item_id, target_folder_id}`` (see
   * ``api/patient_tree.MoveIn``); ``target_folder_id = null`` lands
   * the resource at the patient root.
   */
  move: (patientId: string, item_kind: string, item_id: string, target_folder_id: string | null) =>
    request<{
      status: string;
      item_kind: string;
      item_id: string;
      target_folder_id: string | null;
    }>(`/api/patients/${patientId}/tree/move`, {
      method: "POST",
      json: { item_kind, item_id, target_folder_id },
    }),
  /** Rename a folder in the patient tree. */
  renameFolder: (patientId: string, folderId: string, name: string) =>
    request<{
      id: string;
      name: string;
      parent_folder_id: string | null;
      created_at: string;
      description: string | null;
    }>(`/api/patients/${patientId}/tree/folder/${folderId}`, {
      method: "PATCH",
      json: { name },
    }),
  /**
   * Edit folder metadata (name and / or description). Either field
   * is optional: omit to leave it alone, send ``""`` or ``null`` to
   * clear ``description`` (``name`` cannot be cleared).
   */
  updateFolder: (
    patientId: string,
    folderId: string,
    patch: { name?: string; description?: string | null; created_at?: string },
  ) =>
    request<{
      id: string;
      name: string;
      parent_folder_id: string | null;
      created_at: string;
      description: string | null;
    }>(`/api/patients/${patientId}/tree/folder/${folderId}`, {
      method: "PATCH",
      json: patch,
    }),
  /**
   * Rename a tree item generically. Supports ``folder`` (renames the
   * folder), ``study`` (renames the study description), and
   * ``document`` (renames the patient-document title). Other kinds
   * (series, report, consultation) are rejected by the backend.
   */
  renameItem: (patientId: string, item_kind: string, item_id: string, name: string) =>
    request<{ status: string; item_kind: string; item_id: string; name: string }>(
      `/api/patients/${patientId}/tree/rename`,
      { method: "POST", json: { item_kind, item_id, name } },
    ),
  /**
   * Delete a folder from the patient tree. Child folders cascade;
   * non-folder resources (studies, documents) survive and re-surface
   * at the patient root.
   */
  deleteFolder: (patientId: string, folderId: string) =>
    request<void>(`/api/patients/${patientId}/tree/folder/${folderId}`, { method: "DELETE" }),
};

export interface TreeMoveItem {
  kind: "study" | "series" | "folder" | "report" | "document";
  id: string;
}

export interface TreeMoveInput {
  items: TreeMoveItem[];
  /** Null means "move to the patient root" (out of any folder). */
  target_folder_id: string | null;
}

export interface TreeMoveResult {
  moved: number;
  skipped: number;
}

// -------- patient-scoped search --------

export type PatientSearchSection =
  | "studies"
  | "reports"
  | "annotations"
  | "documents"
  | "consultations"
  | "folders";

export interface PatientSearchItem {
  section: PatientSearchSection;
  id: string;
  title: string;
  preview: string | null;
  rank: number;
  created_at: string;
}

export interface PatientSearchResult {
  patient_id: string;
  query: string;
  total: number;
  by_section: Record<string, number>;
  items: PatientSearchItem[];
}

export interface PatientSearchParams {
  q: string;
  sections?: string;
  semantic?: boolean;
  limit?: number;
  offset?: number;
}

export const patientSearchApi = {
  run: (patientId: string, params: PatientSearchParams) =>
    request<PatientSearchResult>(
      `/api/patients/${patientId}/search${qs(params as unknown as Record<string, QSValue>)}`,
    ),
  /**
   * Lightweight prefix-match autocomplete used by the Evidenze e
   * sintesi editor. Empty / null ``q`` returns the most recent items
   * across all kinds; non-empty ``q`` runs ILIKE 'q%' on the title
   * fields. Distinct from ``run`` which uses ``plainto_tsquery``
   * full-text search and won't match partial words.
   */
  mentionSearch: (
    patientId: string,
    params: {
      q?: string;
      /** Single section name (``"studies"`` / ``"reports"`` / ...).
       *  The editor only ever scopes to one kind at a time. */
      sections?: PatientSearchSection;
      limit?: number;
    } = {},
  ) =>
    request<PatientSearchResult>(
      `/api/patients/${patientId}/mention-search${qs(params as Record<string, QSValue>)}`,
    ),
};

// -------- AI consultations --------

export type ConsultationStatus = "draft" | "submitted" | "signed" | "rejected";
export type ConsultationAuthorKind = "agent" | "human";

export interface ConsultationCitation {
  id: string;
  target_kind: "study" | "series" | "report" | "document" | "annotation" | "marker";
  target_id: string;
  excerpt: string | null;
  locator: string | null;
}

export interface Consultation {
  id: string;
  patient_id: string;
  title: string;
  status: ConsultationStatus;
  author_kind: ConsultationAuthorKind;
  author_subject_id: string | null;
  model_id: string | null;
  de_identified: boolean;
  summary_md: string | null;
  findings_md: string | null;
  recommendations_md: string | null;
  signed_at: string | null;
  signed_by_subject_id: string | null;
  sign_note: string | null;
  rejected_at: string | null;
  rejected_by_subject_id: string | null;
  reject_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConsultationDetail extends Consultation {
  citations: ConsultationCitation[];
}

export interface ConsultationListParams {
  status?: ConsultationStatus | "all";
  author_kind?: ConsultationAuthorKind | "all";
  /** Restrict to consultations whose citations include this target.
   * Both fields must be set together (server returns 400 otherwise). */
  citation_target_kind?: ConsultationCitation["target_kind"];
  citation_target_id?: string;
}

export const consultationsApi = {
  list: (patientId: string, params: ConsultationListParams = {}) => {
    const q: Record<string, QSValue> = {};
    if (params.status && params.status !== "all") q.status = params.status;
    if (params.author_kind && params.author_kind !== "all") q.author_kind = params.author_kind;
    if (params.citation_target_kind && params.citation_target_id) {
      q.citation_target_kind = params.citation_target_kind;
      q.citation_target_id = params.citation_target_id;
    }
    return request<Consultation[]>(`/api/patients/${patientId}/consultations${qs(q)}`);
  },
  detail: (id: string) => request<ConsultationDetail>(`/api/consultations/${id}`),
  /** Create a new consultation. Use ``status: "draft"`` for a synthesis
   * the doctor wants to keep editing; ``"submitted"`` once it's ready
   * for review/sign. ``citations`` anchors the consultation to concrete
   * fascicolo items (study/series/report/annotation/document) — used
   * by the radiology Refer flow to pin the report to the study viewer. */
  create: (input: {
    patient_id: string;
    title: string;
    summary_md?: string | null;
    findings_md?: string | null;
    recommendations_md?: string | null;
    author_kind?: "human" | "agent";
    status?: "draft" | "submitted";
    citations?: {
      target_kind: ConsultationCitation["target_kind"];
      target_id: string;
      excerpt?: string | null;
    }[];
  }) =>
    request<{ id: string; url: string }>("/api/consultations", {
      method: "POST",
      json: input,
    }),
  /** Update a draft consultation in place. 409 if status != "draft". */
  update: (
    id: string,
    input: {
      title?: string;
      summary_md?: string | null;
      findings_md?: string | null;
      recommendations_md?: string | null;
    },
  ) =>
    request<Consultation>(`/api/consultations/${id}`, {
      method: "PATCH",
      json: input,
    }),
  sign: (id: string, note: string | null) =>
    request<Consultation>(`/api/consultations/${id}/sign`, {
      method: "POST",
      json: { note },
    }),
  reject: (id: string, reason: string) =>
    request<Consultation>(`/api/consultations/${id}/reject`, {
      method: "POST",
      json: { reason },
    }),
};

// -------- AI assistants (per-user) --------

/**
 * One row per AI assistant the user has configured. The same patient
 * can be shared with multiple assistants — the benchmark workflow.
 * Each assistant carries its own client_id + client_secret; the
 * plaintext secret is returned exactly once at create / rotate time.
 */
export interface AiAssistant {
  id: string;
  label: string;
  provider: string | null;
  model_id: string | null;
  notes: string | null;
  permissions: string[];
  deidentify_on_use: boolean;
  patient_count: number;
  /** Stable public identifier of the assistant ("bvp_agt_<uuid>"). */
  client_id: string;
  /** First ~8 chars of the latest secret, shown for identification only. */
  client_secret_prefix: string | null;
  /** True once at least one secret has been minted. */
  has_secret: boolean;
  /** Soft-revocation flag. False = bearer secret is rejected. */
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

/**
 * Returned by POST /api/ai-assistants and POST /api/ai-assistants/{id}/rotate
 * exclusively. ``client_secret`` is the plaintext bearer the operator
 * has to copy NOW; the server only stores its sha256 hash.
 */
export interface AiAssistantCreated extends AiAssistant {
  client_secret: string;
}

export interface AiAssistantCreateInput {
  label: string;
  provider?: string | null;
  model_id?: string | null;
  notes?: string | null;
  permissions: string[];
  deidentify_on_use?: boolean;
}

export interface AiAssistantUpdateInput {
  label?: string;
  provider?: string | null;
  model_id?: string | null;
  notes?: string | null;
  permissions?: string[];
  deidentify_on_use?: boolean;
  is_active?: boolean;
}

export interface AssistantSharedPatient {
  patient_id: string;
  display_name: string;
  granted_at: string;
}

export interface ScopeCatalogEntry {
  key: string;
  category: "read" | "write" | "danger";
  label: string;
  description: string;
  dangerous: boolean;
  /** False = UI-only flag, no backend gate yet. The form should show
   *  this clearly so the user does not assume the toggle has effect. */
  enforced: boolean;
}

export interface ConnectorInfo {
  mcp_url: string;
  instructions_md: string;
}

export const aiAssistantsApi = {
  list: () => request<AiAssistant[]>("/api/ai-assistants"),
  detail: (id: string) => request<AiAssistant>(`/api/ai-assistants/${id}`),
  scopeCatalog: () => request<ScopeCatalogEntry[]>("/api/ai-assistants/scope-catalog"),
  connectorInfo: () => request<ConnectorInfo>("/api/ai-assistants/connector-info"),
  create: (input: AiAssistantCreateInput) =>
    request<AiAssistantCreated>("/api/ai-assistants", {
      method: "POST",
      json: input,
    }),
  update: (id: string, input: AiAssistantUpdateInput) =>
    request<AiAssistant>(`/api/ai-assistants/${id}`, {
      method: "PATCH",
      json: input,
    }),
  remove: (id: string) => request<void>(`/api/ai-assistants/${id}`, { method: "DELETE" }),
  setActive: (id: string, isActive: boolean) =>
    request<AiAssistant>(`/api/ai-assistants/${id}`, {
      method: "PATCH",
      json: { is_active: isActive },
    }),
  rotate: (id: string) =>
    request<AiAssistantCreated>(`/api/ai-assistants/${id}/rotate`, {
      method: "POST",
    }),
  listPatients: (id: string) =>
    request<AssistantSharedPatient[]>(`/api/ai-assistants/${id}/patients`),
  sharePatient: (id: string, patientId: string) =>
    request<AssistantSharedPatient>(`/api/ai-assistants/${id}/patients`, {
      method: "POST",
      json: { patient_id: patientId },
    }),
  unsharePatient: (id: string, patientId: string) =>
    request<void>(`/api/ai-assistants/${id}/patients/${patientId}`, {
      method: "DELETE",
    }),
};

// -------- folders (Drive-style) --------

export type ItemKind = "folder" | "study" | "series" | "document" | "report" | "consultation";

export interface BulkItemRef {
  id: string;
  kind: ItemKind;
}

export interface BulkMoveInput {
  items: BulkItemRef[];
  target_folder_id: string | null; // null = root
}

export interface BulkDeleteInput {
  items: BulkItemRef[];
}

export interface BulkReassignInput {
  items: BulkItemRef[];
  target_patient_id: string;
}

export interface BulkShareInput {
  items: BulkItemRef[];
  permissions: string[];
  expires_in_hours: number | null;
  password: string | null;
  label: string | null;
}

export interface BulkResult {
  succeeded: string[];
  failed: Array<{ id: string; reason: string }>;
}

/**
 * Batch operations on Drive-style items. Endpoints are best-effort — if the
 * backend does not yet expose `/api/bulk/*`, wrappers throw `ApiError` with
 * status 404 and the caller is expected to surface a clear error message.
 *
 * `download` returns the raw URL for streaming: the browser follows the
 * redirect/stream into a file download via an anchor tag, so we don't buffer
 * the ZIP in memory. Token is appended as query arg because <a href> can't
 * carry an Authorization header.
 */
export const bulkApi = {
  /**
   * Enqueue a Job that ZIPs the given selection of (kind, id) items.
   * Returns the Job descriptor; the caller polls ``/api/jobs/{id}``
   * for progress and triggers the browser download from
   * ``result_download_url`` once the Job lands in ``succeeded``.
   *
   * Replaces the legacy GET-with-token-in-URL flow that had no
   * progress, no permission re-check, and a backend 404 (the route
   * was never wired). The new path reuses the patient-export Job
   * pipeline with a ``scope_*_ids`` filter.
   */
  requestDownload: (items: BulkItemRef[], include?: string) =>
    request<import("../jobs").JobOut>("/api/bulk/download", {
      method: "POST",
      json: {
        items: items.map((i) => ({ id: i.id, kind: i.kind })),
        include: include ?? null,
      },
    }),
  move: (input: BulkMoveInput) =>
    request<BulkResult>("/api/bulk/move", { method: "POST", json: input }),
  remove: (input: BulkDeleteInput) =>
    request<BulkResult>("/api/bulk/delete", { method: "POST", json: input }),
  reassignPatient: (input: BulkReassignInput) =>
    request<BulkResult>("/api/bulk/reassign-patient", { method: "POST", json: input }),
  share: (input: BulkShareInput) =>
    request<ShareLink>("/api/bulk/share", { method: "POST", json: input }),
};

export interface FolderSummary {
  id: string;
  name: string;
  parent_folder_id: string | null;
  created_at: string;
  description?: string | null;
  /**
   * Patient-scoped folders only: true for the materialised root
   * sentinel (post-0088). The FE hides root rows from listings,
   * pickers and breadcrumbs since the root is conceptually the
   * patient itself, not a folder card.
   */
  is_root?: boolean;
  patient_id?: string | null;
}

export interface FolderItemRow {
  resource_kind: string;
  resource_id: string;
  added_at: string;
}

export interface FolderDetail extends FolderSummary {
  owner_subject_id: string;
  items: FolderItemRow[];
}

/**
 * Enqueue a Job that ZIPs the contents of a folder (recursive). The
 * Job pipeline, the dedup key, the progress / S3-multipart machinery
 * are all shared with the patient-level fascicolo export — only the
 * scope differs.
 */
/** Per-item summary for the folder export-picker UI. The export
 *  dialog renders one row per study / document with a checkbox so
 *  the user can deselect heavyweight files (multi-GB DICOM ISOs)
 *  before submitting the Job. */
export interface FolderExportItem {
  resource_kind: "study" | "document";
  resource_id: string;
  name: string | null;
  size_bytes: number | null;
  file_count: number | null;
  modality: string | null;
  document_type: string | null;
  document_date: string | null;
  study_date: string | null;
}

export const folderExportApi = {
  /** Backwards-compatible variant: when ``includeStudyIds`` /
   *  ``includeDocumentIds`` are omitted the backend exports every
   *  item under the folder (the long-standing default). When
   *  provided, the export is narrowed to the chosen subset. */
  request: (
    folderId: string,
    include?: string,
    selection?: { study_ids?: string[]; document_ids?: string[] },
  ) =>
    request<import("../jobs").JobOut>(`/api/folders/${encodeURIComponent(folderId)}/export`, {
      method: "POST",
      json: {
        include: include ?? null,
        include_study_ids: selection?.study_ids ?? null,
        include_document_ids: selection?.document_ids ?? null,
      },
    }),
  /** List enriched items (kind, name, size, modality, document
   *  type) so the export dialog can render a checkbox list with
   *  meaningful labels. Read-only. */
  items: (folderId: string) =>
    request<FolderExportItem[]>(`/api/folders/${encodeURIComponent(folderId)}/export-items`),
};

export const foldersApi = {
  list: (params?: { patientId?: string }) => {
    const qs =
      params?.patientId !== undefined ? `?patient_id=${encodeURIComponent(params.patientId)}` : "";
    return request<FolderSummary[]>(`/api/folders${qs}`);
  },
  detail: (id: string) => request<FolderDetail>(`/api/folders/${id}`),
  create: (name: string, parentFolderId: string | null, description?: string | null) =>
    request<FolderSummary>("/api/folders", {
      method: "POST",
      json: {
        name,
        parent_folder_id: parentFolderId,
        ...(description !== undefined ? { description } : {}),
      },
    }),
  rename: (id: string, name: string) =>
    request<FolderSummary>(`/api/folders/${id}`, {
      method: "PATCH",
      json: { name },
    }),
  /** Generic metadata patch (name and / or description). */
  update: (id: string, patch: { name?: string; description?: string | null }) =>
    request<FolderSummary>(`/api/folders/${id}`, {
      method: "PATCH",
      json: patch,
    }),
  remove: (id: string) => request<void>(`/api/folders/${id}`, { method: "DELETE" }),
  addItem: (folderId: string, kind: string, resourceId: string) =>
    request<{ status: string }>(`/api/folders/${folderId}/items`, {
      method: "POST",
      json: { resource_kind: kind, resource_id: resourceId },
    }),
  removeItem: (folderId: string, kind: string, resourceId: string) =>
    request<void>(`/api/folders/${folderId}/items/${kind}/${resourceId}`, {
      method: "DELETE",
    }),
  /** Public-link folder share. Sibling of the legacy
   *  ``/folders/{id}/share`` endpoint (which only supports known-
   *  grantee Grants). This route creates a token-bearing
   *  ``ShareLink`` row plus the cascaded item Grants AND enqueues a
   *  background folder-export Job so the recipient lands on a
   *  pre-warmed ZIP. Same payload shape as ``studiesApi.share``. */
  shareLink: (folderId: string, input: Record<string, unknown>) =>
    request<ShareLink>(`/api/folders/${encodeURIComponent(folderId)}/share-link`, {
      method: "POST",
      json: input,
    }),
};

// -------- segmentations --------

export interface SegmentationItem {
  label: string;
  size_bytes: number;
  nonzero_voxels: number | null;
}

export interface SegmentationList {
  series_id: string;
  items: SegmentationItem[];
}

export const segmentationsApi = {
  list: (seriesId: string) => request<SegmentationList>(`/api/series/${seriesId}/segmentations`),
  remove: (seriesId: string, label: string) =>
    request<void>(`/api/series/${seriesId}/segmentations/${encodeURIComponent(label)}`, {
      method: "DELETE",
    }),
  upload: (seriesId: string, label: string, file: File) => {
    const form = new FormData();
    form.append("label", label);
    form.append("file", file);
    // Let fetch set the multipart boundary — do not override content-type.
    return request<SegmentationItem>(`/api/series/${seriesId}/segmentations`, {
      method: "POST",
      body: form,
    });
  },
  fetchMask: async (seriesId: string, label: string): Promise<Uint8Array> => {
    const buf = await request<ArrayBuffer>(
      `/api/series/${seriesId}/segmentations/${encodeURIComponent(label)}`,
    );
    return new Uint8Array(buf);
  },
  /** Enqueue a TotalSegmentator job. Returns immediately; callers
   *  poll ``list()`` to see new labels appear. ``rois`` defaults to
   *  the worker's curated CT abdomen + thorax subset; pass an
   *  explicit list to scope the job (e.g. ``["liver"]`` for a single
   *  organ). */
  autoSegment: (
    seriesId: string,
    opts?: {
      roi_subset?: string[];
      overwrite?: boolean;
      fast?: boolean;
    },
  ) =>
    request<{
      status: string;
      series_id: string;
      job_id: string;
      rois: string[];
    }>(`/api/series/${seriesId}/segmentations/auto`, {
      method: "POST",
      json: opts ?? {},
    }),
  /** Run MedSAM-2 on a single slice. Synchronous: backend awaits the
   *  worker result before responding (typical CPU latency 3-10s, hard
   *  timeout 60s). When ``label`` is provided the resulting 2D mask
   *  is also persisted as a full-volume binary at
   *  ``segmentations/{id}/{label}.bin`` so the viewer can apply it
   *  via ``setSegmentationMask`` without a separate fetch. */
  interactivePredict: (
    seriesId: string,
    body: {
      axis: 0 | 1 | 2;
      slice_idx: number;
      points: Array<[number, number]>;
      labels?: number[];
      label?: string;
    },
  ) =>
    request<{
      status: string;
      shape: [number, number];
      mask_b64: string;
      axis: number;
      slice_idx: number;
      volume_dims: [number, number, number];
      persisted_label?: string;
    }>(`/api/series/${seriesId}/segmentations/interactive/predict`, {
      method: "POST",
      json: body,
    }),
  /** Discover the models / capabilities of the configured MONAI Label
   *  server. 503 with a clear hint when ``BVP_MONAI_LABEL_URL`` is
   *  not set on the backend host. */
  monaiLabelInfo: () => request<Record<string, unknown>>("/api/segmentations/monai_label/info"),
};

// -------- tags --------

/** A single tag row as returned by GET /api/tags. */
export interface Tag {
  id: string;
  target_kind: "study" | "series" | "instance" | "patient" | "report" | "dataset";
  target_id: string;
  namespace: string;
  value: string;
  source?: string;
  confidence?: number | null;
  created_by_subject_id?: string | null;
  created_at?: string;
}

/**
 * Per-namespace tag tree returned by ``GET /api/tags/tree``. The
 * backend emits a list (one entry per namespace) of slash-segmented
 * paths (``lung/upper-lobe`` → nested ``children``); each leaf
 * carries an aggregate ``count`` and a per-source breakdown so the
 * UI can badge provenance (manual vs auto vs imported).
 */
export interface TagTreeNode {
  value: string;
  count: number;
  manual_count: number;
  auto_count: number;
  imported_count: number;
  children: TagTreeNode[];
}
export interface TagTreeNamespace {
  namespace: string;
  roots: TagTreeNode[];
}
export type TagTree = TagTreeNamespace[];

export interface TagListParams {
  namespace?: string;
  q?: string;
  limit?: number;
}

export const tagsApi = {
  /** Search tags by namespace and/or query prefix. Used by the autocomplete widget. */
  list: (params: TagListParams = {}) =>
    request<Tag[]>(`/api/tags${qs(params as Record<string, QSValue>)}`),
  /** Namespace-grouped counts, for the /tags browser page. */
  tree: () => request<TagTree>("/api/tags/tree"),
  /** Tags currently attached to a single target (study/series/...). Manual rows first. */
  forTarget: (targetKind: Tag["target_kind"], targetId: string) =>
    request<Tag[]>(`/api/tags/for-target${qs({ target_kind: targetKind, target_id: targetId })}`),
  add: (input: {
    target_kind: Tag["target_kind"];
    target_id: string;
    namespace: string;
    value: string;
  }) => request<Tag>("/api/tags", { method: "POST", json: input }),
  remove: (tagId: string) => request<void>(`/api/tags/${tagId}`, { method: "DELETE" }),
};

// -------- GDPR --------

export interface Consent {
  kind: string;
  granted: boolean;
  granted_at: string | null;
  revoked_at: string | null;
}

export interface ErasureRequest {
  id: string;
  scope: string;
  status: string;
  reason: string | null;
  requested_at: string;
  completed_at: string | null;
}

export const gdprApi = {
  listConsents: () => request<Consent[]>("/api/gdpr/consents"),
  setConsent: (kind: string, granted: boolean) =>
    request<Consent>("/api/gdpr/consent", {
      method: "POST",
      json: { kind, granted },
    }),
  requestErasure: (scope: string, reason: string | null) =>
    request<ErasureRequest>("/api/gdpr/erasure-request", {
      method: "POST",
      json: { scope, reason },
    }),
  /**
   * Enqueue an async GDPR Art. 20 export Job. Returns the Job
   * descriptor (poll ``GET /api/jobs/{id}`` for progress + the
   * presigned download URL on ``result_download_url``).
   */
  requestExport: () => request<import("../jobs").JobOut>("/api/gdpr/export", { method: "POST" }),
};

// -------- embeddings admin --------

export type EmbeddingTargetKind = "study" | "series" | "instance";

export interface EmbeddingLastFailure {
  target_id: string;
  error_message: string;
  error_class: string | null;
  failed_at: string;
  retry_count: number;
}

export interface EmbeddingCoverageRow {
  model_id: string;
  target_kind: EmbeddingTargetKind;
  total: number;
  done: number;
  failed: number;
  pending: number;
  percentage: number;
  last_failures: EmbeddingLastFailure[];
}

export interface EmbeddingCoverage {
  items: EmbeddingCoverageRow[];
}

export interface EmbeddingEnqueueResult {
  status: string;
  model_id: string;
  target_kind: EmbeddingTargetKind;
  enqueued: number;
  /** Targets matching the selection; the whole answer on a dry run. */
  candidates: number;
}

export interface TextChunkBySource {
  source_kind: string;
  total: number;
  embedded: number;
  pending: number;
}

export interface TextChunkCoverage {
  total_chunks: number;
  embedded_chunks: number;
  pending_chunks: number;
  pct: number;
  by_source_kind: TextChunkBySource[];
  model_id: string;
}

export const embeddingsAdminApi = {
  coverage: () => request<EmbeddingCoverage>("/api/embeddings/coverage"),
  textChunkCoverage: () => request<TextChunkCoverage>("/api/embeddings/text-chunks/coverage"),
  retryFailed: (modelId: string, targetKind: EmbeddingTargetKind) =>
    request<EmbeddingEnqueueResult>(
      `/api/embeddings/retry-failed${qs({ model_id: modelId, target_kind: targetKind })}`,
      { method: "POST" },
    ),
  embedMissing: (modelId: string, targetKind: EmbeddingTargetKind) =>
    request<EmbeddingEnqueueResult>(
      `/api/embeddings/embed-missing${qs({ model_id: modelId, target_kind: targetKind })}`,
      { method: "POST" },
    ),
  embedMissingTextChunks: () =>
    request<EmbeddingEnqueueResult>("/api/embeddings/text-chunks/embed-missing", {
      method: "POST",
    }),
};

// -------- universal bulk upload --------

/** Client-side classification buckets surfaced in the pre-upload table. */
export type DetectedKind = "dicom" | "pdf" | "image" | "archive" | "text" | "unknown";

/** Routing the UI suggests *before* the server makes the final call. */
export type SuggestedRoute =
  | { kind: "study" }
  | { kind: "document"; document_type: string }
  | { kind: "archive" }
  | { kind: "skip"; reason: string };

export interface BulkUploadFileResult {
  filename: string;
  relative_path: string;
  status: "ok" | "skipped" | "error";
  classification: DetectedKind | string;
  routed_to: "study" | "document" | "archive" | "skipped";
  study_id?: string | null;
  document_id?: string | null;
  message?: string | null;
}

export interface BulkStudyCreated {
  id: string;
  name: string;
  series_count: number;
}

export interface BulkDocumentCreated {
  id: string;
  name: string;
  document_type: string;
  kind: string;
}

/**
 * Wire shape returned by ``POST /api/upload/bulk``. The backend nests
 * the per-resource results under ``uploaded`` (see
 * ``api/bulk_upload.UploadedSummary``); the client adapter flattens
 * it before handing the value to the UI so every consumer gets the
 * same friendly shape regardless of the wrapper.
 */
export interface BulkUploadSummary {
  studies_created: BulkStudyCreated[];
  documents_created: BulkDocumentCreated[];
  skipped: { filename: string; reason: string }[];
  errors: { filename: string; message: string }[];
  files: BulkUploadFileResult[];
  dicomdir_found: boolean;
  zip_archives_found: number;
  total_files: number;
}

export type ContributionTier = "t1" | "t2" | "t3" | "t4";

export interface BulkUploadInput {
  files: File[];
  relativePaths: string[];
  patientId?: string | null;
  targetFolderId?: string | null;
  /**
   * Contribution tier (DESIGN.md §9). Defaults to t1 (private) on the
   * server when omitted; the UI should pass the user's explicit
   * selection so the auto-consent logic (F6.1) fires correctly.
   */
  tier?: ContributionTier;
  /**
   * Optional per-file override. The backend treats these as best-effort
   * hints: unknown document_type values fall back to "other".
   */
  overrides?: { relative_path: string; route: SuggestedRoute }[];
  /**
   * When the payload includes a CD/DVD ``.iso`` image: keep the
   * original archive on storage as a downloadable PatientDocument so
   * a referring clinician can read it on a certified workstation.
   * Default true; set false to save space when the unpacked content
   * is sufficient.
   */
  keepIsoArchive?: boolean;
  /**
   * Wrap each ISO's unpacked content into a sub-folder named after
   * the ISO stem so vendor README/autorun files don't pollute the
   * parent folder. Default true.
   */
  wrapIsoInFolder?: boolean;
  /**
   * When true (default) the worker unpacks every uploaded ISO and
   * ingests its DICOMDIR / DICOM members. Set false to attach the
   * ISO as a downloadable PatientDocument ONLY without re-creating
   * the underlying study — useful when the study has already been
   * imported in a previous session and the operator now just wants
   * to archive the original CD/DVD image.
   */
  extractIsoContents?: boolean;
  onProgress?: (percent: number) => void;
  signal?: AbortSignal;
}

/**
 * POST /api/upload/bulk — universal ingestion endpoint that sorts DICOM,
 * PDFs, images, and ZIP archives into studies vs. patient documents.
 *
 * XHR (not fetch) so we can surface upload progress: fetch()'s upload
 * streams still aren't uniformly supported across browsers.
 *
 * Async since 2026-04: the server stages the multipart bytes into S3
 * and returns ``202 Accepted`` with a ``JobOut``. The caller polls
 * ``GET /api/jobs/{id}`` (or wires the returned job id into the
 * ``useJob`` hook in ``./useJob.ts``) for progress + the final
 * summary on ``job.result``. Cancellation: ``DELETE /api/jobs/{id}``.
 */
export const bulkUploadApi = {
  upload(input: BulkUploadInput): Promise<import("../jobs").JobOut> {
    const {
      files,
      relativePaths,
      patientId,
      targetFolderId,
      tier,
      overrides,
      keepIsoArchive,
      wrapIsoInFolder,
      extractIsoContents,
      onProgress,
      signal,
    } = input;

    const form = new FormData();
    for (let i = 0; i < files.length; i += 1) {
      form.append("files", files[i], files[i].name);
      // Parallel to "files" so the server can reconstruct the tree.
      form.append("relative_paths", relativePaths[i] ?? files[i].name);
    }
    if (patientId) form.append("patient_id", patientId);
    if (targetFolderId) form.append("target_folder_id", targetFolderId);
    if (tier) form.append("tier", tier);
    if (keepIsoArchive !== undefined) {
      form.append("keep_iso_archive", keepIsoArchive ? "true" : "false");
    }
    if (wrapIsoInFolder !== undefined) {
      form.append("wrap_iso_in_folder", wrapIsoInFolder ? "true" : "false");
    }
    if (extractIsoContents !== undefined) {
      form.append("extract_iso_contents", extractIsoContents ? "true" : "false");
    }
    if (overrides && overrides.length > 0) {
      // One JSON blob per override so the server parses them independently.
      for (const o of overrides) form.append("manual_override", JSON.stringify(o));
    }

    const token = getStoredToken();
    return new Promise<import("../jobs").JobOut>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE_URL}/api/upload/bulk`);
      if (token) xhr.setRequestHeader("authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable && onProgress) {
          onProgress(Math.round((ev.loaded / ev.total) * 100));
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            const job = JSON.parse(xhr.responseText) as import("../jobs").JobOut;
            resolve(job);
          } catch (e) {
            reject(new ApiError(xhr.status, `invalid JSON response: ${e}`));
          }
        } else {
          let detail: unknown = xhr.responseText;
          try {
            detail = JSON.parse(xhr.responseText);
          } catch {
            /* keep as text */
          }
          if (xhr.status === 401) _markAuthExpired();
          reject(new ApiError(xhr.status, detail));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "network error"));
      xhr.onabort = () => reject(new ApiError(0, "aborted"));
      if (signal) {
        if (signal.aborted) xhr.abort();
        else signal.addEventListener("abort", () => xhr.abort(), { once: true });
      }
      xhr.send(form);
    });
  },
};

// -------- resumable upload sessions (DESIGN.md §11.6) --------
// The recoverable replacement for bulkUploadApi.upload: a durable session is
// created BEFORE any bytes, each file is PATCHed in fixed chunks carrying an
// Upload-Offset, and a disconnect resumes from the server-acked offset. The
// legacy bulkUploadApi above is kept as a fallback during the transition. All
// bytes flow through the backend (storage isolation) — the client only ever
// sees the session id + per-file offsets, never an S3 key.

export interface UploadFileDecl {
  filename: string;
  relative_path: string;
  size: number;
  sha256?: string;
}

export interface SessionFileState {
  file_index: number;
  filename: string;
  relative_path: string | null;
  declared_size: number;
  received_offset: number;
  status: string;
}

export interface UploadSession {
  id: string;
  status: string;
  chunk_size: number;
  declared_total_bytes: number;
  received_total_bytes: number;
  job_id: string | null;
  files: SessionFileState[];
}

export interface CreateSessionInput {
  files: UploadFileDecl[];
  tier?: ContributionTier;
  patientId?: string | null;
  folderId?: string | null;
  keepIsoArchive?: boolean;
  wrapIsoInFolder?: boolean;
  extractIsoContents?: boolean;
}

export const uploadSessionApi = {
  create(input: CreateSessionInput): Promise<UploadSession> {
    return request<UploadSession>("/api/upload/sessions", {
      method: "POST",
      json: {
        files: input.files,
        tier: input.tier ?? "t1",
        patient_id: input.patientId ?? null,
        folder_id: input.folderId ?? null,
        keep_iso_archive: input.keepIsoArchive ?? true,
        wrap_iso_in_folder: input.wrapIsoInFolder ?? true,
        extract_iso_contents: input.extractIsoContents ?? true,
      },
    });
  },
  get(sessionId: string): Promise<UploadSession> {
    return request<UploadSession>(`/api/upload/sessions/${sessionId}`);
  },
  /**
   * PATCH one chunk at ``offset``. The raw bytes are the body; the server
   * reads ``Upload-Offset`` + the raw body (no multipart). On a gap the
   * server throws ``ApiError(409, {code:"offset_mismatch", expected_offset})``
   * and the caller re-slices from ``expected_offset``.
   */
  putChunk(
    sessionId: string,
    fileIndex: number,
    offset: number,
    chunk: Blob,
  ): Promise<SessionFileState> {
    const headers = new Headers();
    headers.set("content-type", "application/octet-stream");
    headers.set("Upload-Offset", String(offset));
    return request<SessionFileState>(`/api/upload/sessions/${sessionId}/files/${fileIndex}`, {
      method: "PATCH",
      body: chunk,
      headers,
    });
  },
  commit(sessionId: string): Promise<import("../jobs").JobOut> {
    return request<import("../jobs").JobOut>(`/api/upload/sessions/${sessionId}/commit`, {
      method: "POST",
    });
  },
  abort(sessionId: string): Promise<void> {
    return request<void>(`/api/upload/sessions/${sessionId}`, { method: "DELETE" });
  },
};

// -------- volume binary parse --------

export interface PackedVolumeHeader {
  nx: number;
  ny: number;
  nz: number;
  spacing: [number, number, number];
  valueRange: [number, number];
  /** Real DICOM patient-space geometry, recovered from the
   *  ``X-Volume-*`` response headers (the blob's 32-byte binary header
   *  is frozen and carries none of it). ``origin`` is the LPS position
   *  of voxel (0,0,0); ``direction`` is the 9-float
   *  [rowCos, colCos, sliceCos] matrix in Cornerstone3D order;
   *  ``frameOfReferenceUid`` is the DICOM FoR. All optional: legacy
   *  packs (and series predating the geometry column) omit them and the
   *  viewer falls back to an identity frame. */
  origin?: [number, number, number];
  direction?: [number, number, number, number, number, number, number, number, number];
  frameOfReferenceUid?: string;
}

/** Parse one ``X-Volume-*`` header of N comma-separated floats. Returns
 *  ``undefined`` when the header is absent or malformed so the caller
 *  can fall back to the identity frame rather than feed Cornerstone
 *  NaNs. Exported so the viewer page's bespoke volume decoder reads the
 *  same geometry without duplicating the parse. */
export function parseFloatVector(raw: string | null, expected: number): number[] | undefined {
  if (!raw) return undefined;
  const parts = raw.split(",").map((s) => Number.parseFloat(s.trim()));
  if (parts.length !== expected || parts.some((n) => !Number.isFinite(n))) {
    return undefined;
  }
  return parts;
}

export async function fetchVolume(
  seriesId: string,
  opts?: {
    earlFwhmMm?: number;
    /** Optional per-chunk progress hook. ``loaded`` = bytes received,
     *  ``total`` = bytes the server announced via ``Content-Length``
     *  (0 if absent — chunked transfer encoding or upstream omission).
     *  ``phase`` lets callers distinguish "still downloading" from the
     *  short post-fetch decode where we materialise the Float32Array.
     */
    onProgress?: (info: {
      loaded: number;
      total: number;
      phase: "download" | "decode";
    }) => void;
    signal?: AbortSignal;
    /** Fetch the 1/8 low-res preview (``volume-preview.raw``) instead of the
     *  full ``volume.raw``. The preview carries NO X-Volume-* geometry, so
     *  ``header.origin/direction/frameOfReferenceUid`` come back undefined —
     *  fine for the transient first-paint placeholder. */
    preview?: boolean;
  },
): Promise<{
  header: PackedVolumeHeader;
  scalars: Float32Array;
}> {
  const onProgress = opts?.onProgress;
  const url = opts?.preview
    ? studiesApi.volumePreviewUrl(seriesId)
    : studiesApi.volumeUrl(seriesId, opts);
  const resp = await fetch(url, {
    headers: (() => {
      const h = new Headers();
      const t = getStoredToken();
      if (t) h.set("authorization", `Bearer ${t}`);
      return h;
    })(),
    signal: opts?.signal,
  });
  if (!resp.ok) throw new ApiError(resp.status, await resp.text());
  // Real patient-space geometry travels out-of-band in X-Volume-* headers
  // (the blob's binary header is frozen). Read them before consuming the
  // body. Absent/partial → undefined, and makeLocalVolume keeps the
  // identity-frame fallback.
  const originHdr = parseFloatVector(resp.headers.get("x-volume-origin"), 3);
  const directionHdr = parseFloatVector(resp.headers.get("x-volume-direction"), 9);
  const frameOfReferenceUid = resp.headers.get("x-volume-frame-of-reference") || undefined;
  // Stream the body so the caller can render a progress bar; fall back
  // to a plain ``arrayBuffer()`` when the platform doesn't expose a
  // readable body (older Safari, some test environments).
  let buf: ArrayBuffer;
  const total = Number(resp.headers.get("content-length") ?? 0) || 0;
  // ``getReader()`` locks the response body. Calling it when no
  // ``onProgress`` callback was supplied — the common case — would
  // leave the body locked AND unused, and the subsequent
  // ``resp.arrayBuffer()`` fallback throws "body stream is locked",
  // which the deep-link callers swallow silently. Only acquire the
  // reader when the streaming path is going to consume it.
  const reader = onProgress ? resp.body?.getReader?.() : null;
  if (reader && onProgress) {
    const chunks: Uint8Array[] = [];
    let loaded = 0;
    onProgress({ loaded: 0, total, phase: "download" });
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) {
        chunks.push(value);
        loaded += value.byteLength;
        onProgress({ loaded, total, phase: "download" });
      }
    }
    // Concatenate into one contiguous buffer so the DataView /
    // Float32Array views below see a single backing store.
    let merged: Uint8Array;
    if (chunks.length === 1) {
      merged = chunks[0];
    } else {
      merged = new Uint8Array(loaded);
      let off = 0;
      for (const c of chunks) {
        merged.set(c, off);
        off += c.byteLength;
      }
    }
    // ``Uint8Array.buffer`` is typed as ``ArrayBufferLike`` (covers
    // SharedArrayBuffer too) but our ``new Uint8Array(loaded)`` always
    // allocates a plain ``ArrayBuffer``. Slice into a fresh buffer so
    // the DataView / Float32Array views below get a non-shared store.
    const sliced = (merged.buffer as ArrayBuffer).slice(
      merged.byteOffset,
      merged.byteOffset + merged.byteLength,
    );
    buf = sliced;
  } else {
    buf = await resp.arrayBuffer();
  }
  onProgress?.({ loaded: total, total, phase: "decode" });
  const dv = new DataView(buf);
  const nx = dv.getUint32(0, true);
  const ny = dv.getUint32(4, true);
  const nz = dv.getUint32(8, true);
  const sx = dv.getFloat32(12, true);
  const sy = dv.getFloat32(16, true);
  const sz = dv.getFloat32(20, true);
  const vmin = dv.getFloat32(24, true);
  const vmax = dv.getFloat32(28, true);
  const scalars = new Float32Array(buf, 32, nx * ny * nz);
  return {
    header: {
      nx,
      ny,
      nz,
      spacing: [sx, sy, sz],
      valueRange: [vmin, vmax],
      origin: originHdr as [number, number, number] | undefined,
      direction: directionHdr as PackedVolumeHeader["direction"],
      frameOfReferenceUid,
    },
    scalars,
  };
}

// ---------------------------------------------------------------------------
// Transparency (F11.2) — public aggregate platform stats, no auth required.
// ---------------------------------------------------------------------------

// Public slice served by GET /api/transparency (no auth): aggregate
// study counts + governance only. Community size, sharing activity, and
// LLM activity are NOT exposed here — they live on the admin superset.
export type TransparencyPublicOut = {
  generated_at: string;
  version: string;
  studies: {
    total: number;
    by_tier: Record<"t1" | "t2" | "t3" | "t4", number>;
    public: number;
    by_modality: Record<string, number>;
  };
  governance: {
    license: string;
  };
};

// Admin superset served by GET /api/transparency/admin (require_admin):
// the public slice plus the non-public operational counts.
export type TransparencyOut = TransparencyPublicOut & {
  users: { total: number };
  sharing: {
    grants_active: number;
    grants_deidentified: number;
    grants_commercial: number;
  };
  llm: {
    consultations_total: number;
    summaries_total: number;
  };
};

export const transparencyApi = {
  get(): Promise<TransparencyPublicOut> {
    return request<TransparencyPublicOut>("/api/transparency");
  },
  getAdmin(): Promise<TransparencyOut> {
    return request<TransparencyOut>("/api/transparency/admin");
  },
};

// "My data in datasets" — the contributor-sovereignty view (a5c3f73e).
// Aggregate + storage-isolated: no study/patient id or storage location.
export interface MyDataset {
  dataset_id: string;
  status: "open" | "frozen" | "stale";
  my_study_count: number;
  study_count: number;
  contributor_count: number;
  tiers: string[];
  created_at: string;
}

export const myDataApi = {
  datasets(): Promise<MyDataset[]> {
    return request<MyDataset[]>("/api/me/datasets");
  },
};

// ---------------------------------------------------------------------------
// BYOK (F7.1) — user-supplied LLM API keys.
// ---------------------------------------------------------------------------

export type BYOKProvider = "anthropic";

export interface APIKeyListOut {
  provider: BYOKProvider;
  granted_at: string;
  last_used_at: string | null;
}

export interface APIKeyOut extends APIKeyListOut {
  key_tail: string;
}

export const byokApi = {
  list(): Promise<APIKeyListOut[]> {
    return request<APIKeyListOut[]>("/api/settings/api-keys");
  },
  save(provider: BYOKProvider, apiKey: string): Promise<APIKeyOut> {
    return request<APIKeyOut>(`/api/settings/api-keys/${provider}`, {
      method: "PUT",
      json: { api_key: apiKey },
    });
  },
  revoke(provider: BYOKProvider): Promise<void> {
    return request<void>(`/api/settings/api-keys/${provider}`, { method: "DELETE" });
  },
};

// ---------------------------------------------------------------------------
// Credit wallet (F7.3) — user balance + admin top-up.
// ---------------------------------------------------------------------------

export interface WalletBalanceOut {
  balance_cents: number;
  balance_usd: number;
}

export interface AdminTopupIn {
  user_subject_id: string;
  amount_cents: number;
  /** Caller-supplied idempotency key. Repeating the same key is a
   * safe no-op so a network retry does not double-credit. */
  idempotency_key: string;
  /** Optional free-text rationale shown in the ledger audit trail. */
  reason?: string;
}

export interface AdminTopupOut {
  ledger_row_id: string;
  balance_after_cents: number;
}

export const creditsApi = {
  balance(): Promise<WalletBalanceOut> {
    return request<WalletBalanceOut>("/api/me/credits");
  },
  adminTopup(body: AdminTopupIn): Promise<AdminTopupOut> {
    return request<AdminTopupOut>("/api/admin/credits/topup", {
      method: "POST",
      json: body,
    });
  },
};

// -------- F12 versioning: history + proposals --------

export interface CommitOut {
  commit_hash: string;
  parent_hashes: string[];
  tree_hash: string;
  author_subject_id: string | null;
  author_kind: "human" | "agent" | "system" | "link";
  // Resolved display_name of the author. Null only for migrated /
  // synthetic commits that have no author subject.
  author_display_name: string | null;
  model_id: string | null;
  provider: string | null;
  agent_token_id: string | null;
  // Direct FK to agent_assistants (modern per-assistant client_secret
  // flow). Legacy commits resolve through agent_token_id; new ones
  // pin this column directly. Surfaced in the audit UI in advanced
  // mode so reviewers can correlate a commit to a specific assistant
  // even when the human-readable label was renamed since.
  agent_assistant_id?: string | null;
  // Resolved label of the AI assistant when either agent_assistant_id
  // or agent_token_id resolves to an assistant row. Clinicians see
  // this label; model_id is shown only in advanced mode.
  agent_assistant_label: string | null;
  // Set when the commit was authored via an anonymous share link.
  // The revision-history UI uses these to render a "modality A" badge.
  share_link_id?: string | null;
  share_link_label?: string | null;
  share_link_recipient?: string | null;
  branch_at_creation: string | null;
  message: string;
  created_at: string;
}

export interface HistoryOut {
  patient_id: string;
  ref_name: string;
  head_commit: string | null;
  commits: CommitOut[];
}

export interface RefOut {
  ref_name: string;
  head_commit: string;
  is_locked: boolean;
}

export interface MultiBranchHistoryOut {
  patient_id: string;
  refs: RefOut[];
  commits: CommitOut[];
}

export interface RefLogEntryOut {
  id: string;
  ref_name: string;
  from_commit: string | null;
  to_commit: string;
  op_kind: "init" | "commit" | "merge" | "reset" | "revert" | "rebase" | "delete";
  actor_subject_id: string | null;
  reason: string | null;
  created_at: string;
}

export interface DiffEntryOut {
  entity_kind: string;
  entity_id: string;
  change: "added" | "removed" | "modified";
  hash_a: string | null;
  hash_b: string | null;
}

export interface RevertResultOut {
  commit_hash: string;
  branch_ref: string;
}

export interface RevertConflictItem {
  entity_kind: string;
  entity_id: string;
  head_hash: string | null;
  target_hash: string | null;
}

export interface RevertConflictDetail {
  code: "revert_conflict";
  message: string;
  conflicts: RevertConflictItem[];
}

export const historyApi = {
  list: (patientId: string, params: { ref?: string; limit?: number } = {}) =>
    request<HistoryOut>(
      `/api/patients/${patientId}/history${qs(params as Record<string, QSValue>)}`,
    ),
  /**
   * Branch-aware variant of ``list``: returns commits from every ref
   * (main + each ``consultation/<id>``) merged into a single
   * time-sorted list. The UI uses ``commit.branch_at_creation`` to
   * paint per-branch lanes / chips.
   */
  listAll: (patientId: string, params: { per_ref_limit?: number } = {}) =>
    request<MultiBranchHistoryOut>(
      `/api/patients/${patientId}/history/all${qs(params as Record<string, QSValue>)}`,
    ),
  atCommit: (patientId: string, commitHash: string, entity_kind?: string) =>
    request<Record<string, Record<string, Record<string, unknown>>>>(
      `/api/patients/${patientId}/at/${commitHash}${qs({ entity_kind })}`,
    ),
  diff: (patientId: string, fromCommit: string, toCommit: string) =>
    request<DiffEntryOut[]>(
      `/api/patients/${patientId}/diff${qs({ from: fromCommit, to: toCommit })}`,
    ),
  refLog: (patientId: string, params: { ref?: string; limit?: number } = {}) =>
    request<RefLogEntryOut[]>(
      `/api/patients/${patientId}/ref-log${qs(params as Record<string, QSValue>)}`,
    ),
  /**
   * Append a revert commit on the routed branch (main for owners,
   * consultation for non-owners). Throws ApiError(409) with a
   * RevertConflictDetail body when one or more affected entities have
   * been modified between the target commit and the branch head; the
   * UI is expected to fall back to per-entity restoreEntity calls.
   */
  revert: (
    patientId: string,
    commitHash: string,
    body: { message: string; consultation_id?: string },
  ) =>
    request<RevertResultOut>(`/api/patients/${patientId}/revert/${commitHash}`, {
      method: "POST",
      json: body,
    }),
  /**
   * Restore one (entity_kind, entity_id) to its state at source_commit_hash.
   * Granular alternative to revert; no conflict detection by design.
   */
  restoreEntity: (
    patientId: string,
    body: {
      source_commit_hash: string;
      entity_kind: string;
      entity_id: string;
      message: string;
      consultation_id?: string;
    },
  ) =>
    request<RevertResultOut>(`/api/patients/${patientId}/restore-entity`, {
      method: "POST",
      json: body,
    }),
};

// -------- F12 proposals (PR-style review) --------

export interface ConflictOut {
  id: string;
  entity_kind: string;
  entity_id: string;
  base_object_hash: string | null;
  source_object_hash: string | null;
  target_object_hash: string | null;
  conflict_kind: "add_add" | "edit_edit" | "edit_delete" | "delete_edit";
  // ``auto_merge`` is server-generated when the three-way text merger
  // succeeded; reviewers can still override it with the explicit
  // take_source / take_target / manual paths.
  resolution: "take_source" | "take_target" | "manual" | "auto_merge" | null;
  resolved_object_hash: string | null;
  resolved_by_subject_id: string | null;
  resolved_at: string | null;
}

export interface ProposalOut {
  id: string;
  patient_id: string;
  consultation_id: string | null;
  source_ref_name: string;
  target_ref_name: string;
  source_head_commit: string;
  target_head_commit: string;
  base_commit: string | null;
  proposer_subject_id: string;
  title: string;
  description: string | null;
  status: "open" | "approved" | "rejected" | "merged" | "withdrawn" | "superseded";
  conflict_count: number;
  merge_commit: string | null;
  reviewed_by_subject_id: string | null;
  reviewed_at: string | null;
  review_decision: "approve" | "request_changes" | "reject" | null;
  review_notes: string | null;
  created_at: string;
  closed_at: string | null;
  conflicts: ConflictOut[];
}

export const proposalsApi = {
  list: (patientId: string, status?: ProposalOut["status"]) =>
    request<ProposalOut[]>(`/api/patients/${patientId}/proposals${qs({ status })}`),
  detail: (proposalId: string) => request<ProposalOut>(`/api/proposals/${proposalId}`),
  resolveConflict: (
    proposalId: string,
    conflictId: string,
    body: {
      kind: "take_source" | "take_target" | "manual";
      payload?: Record<string, unknown>;
    },
  ) =>
    request<ConflictOut>(`/api/proposals/${proposalId}/conflicts/${conflictId}/resolve`, {
      method: "POST",
      json: body,
    }),
  merge: (proposalId: string, review_notes?: string) =>
    request<ProposalOut>(`/api/proposals/${proposalId}/merge`, {
      method: "POST",
      json: { review_notes },
    }),
  withdraw: (proposalId: string, reason?: string) =>
    request<ProposalOut>(`/api/proposals/${proposalId}/withdraw`, {
      method: "POST",
      json: { reason },
    }),
  /**
   * Owner / admin rejects the proposal. Distinct from withdraw: the
   * proposal ends in status='rejected' (not 'withdrawn'), and a
   * non-empty review_notes is required by the backend.
   */
  reject: (proposalId: string, review_notes: string) =>
    request<ProposalOut>(`/api/proposals/${proposalId}/reject`, {
      method: "POST",
      json: { review_notes },
    }),
};

// -------- F12.4 publish to OpenData --------

export interface PublishOut {
  public_patient_id: string;
  public_main_commit: string;
  cloned_clinical_notes: number;
  redaction_count: number;
}

export const publishApi = {
  toOpenData: (patientId: string, pseudonym?: string) =>
    request<PublishOut>(`/api/patients/${patientId}/publish`, {
      method: "POST",
      json: { pseudonym },
    }),
};

// -------- Markers (unified viewer ephemera + fiducials + bookmarks) --------

export type MarkerKind =
  | "measurement.distance"
  | "measurement.angle"
  | "measurement.area"
  | "measurement.ellipse"
  | "measurement.freehand"
  | "measurement.arrow"
  | "measurement.text"
  | "measurement.probe"
  | "measurement.bbox"
  | "measurement.sphere"
  | "bbox.lesion"
  | "bbox.exclusion"
  | "fiducial"
  | "reading-note"
  | "text-overlay";

export interface Marker {
  id: string;
  patient_id: string;
  target_kind: "study" | "series" | "instance";
  target_id: string;
  kind: MarkerKind;
  geometry: Record<string, unknown> | null;
  body: string | null;
  computed: Record<string, unknown> | null;
  author_subject_id: string | null;
  author_kind: "human" | "agent" | "system";
  model_id: string | null;
  provider: string | null;
  created_at: string;
  updated_at: string;
}

export interface MarkerListParams {
  target_kind?: "study" | "series" | "instance";
  target_id?: string;
  kind?: MarkerKind;
  limit?: number;
}

export const markersApi = {
  list: (patientId: string, params: MarkerListParams = {}) => {
    const q: Record<string, QSValue> = {};
    if (params.target_kind) q.target_kind = params.target_kind;
    if (params.target_id) q.target_id = params.target_id;
    if (params.kind) q.kind = params.kind;
    if (params.limit) q.limit = params.limit;
    return request<Marker[]>(`/api/patients/${patientId}/markers${qs(q)}`);
  },
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
  ) =>
    request<Marker>(`/api/patients/${patientId}/markers`, {
      method: "POST",
      json: input,
    }),
  update: (
    markerId: string,
    input: {
      geometry?: Record<string, unknown> | null;
      body?: string | null;
      computed?: Record<string, unknown> | null;
    },
  ) =>
    request<Marker>(`/api/markers/${markerId}`, {
      method: "PATCH",
      json: input,
    }),
  remove: (markerId: string) => request<void>(`/api/markers/${markerId}`, { method: "DELETE" }),
  exportUrl: (studyId: string, format: "json" | "sr") =>
    `${API_BASE_URL}/api/studies/${studyId}/markers/export?format=${format}`,
  importFile: async (studyId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const token = getStoredToken();
    const headers = new Headers();
    if (token) headers.set("authorization", `Bearer ${token}`);
    const resp = await fetch(`${API_BASE_URL}/api/studies/${studyId}/markers/import`, {
      method: "POST",
      body: fd,
      headers,
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new ApiError(resp.status, detail);
    }
    return resp.json() as Promise<{ imported: number }>;
  },
};

// -------- Findings (structured, coded clinical reperti) --------

export type FindingLaterality = "left" | "right" | "bilateral" | "midline";
export type FindingStatus = "candidate" | "confirmed" | "retracted";
export type FindingGeometryRole = "measurement" | "bbox" | "mask" | "fiducial";

export interface FindingVocabTerm {
  id: string;
  key: string;
  display: string;
  code_system: string | null;
  code: string | null;
}
export interface FindingTypeTerm extends FindingVocabTerm {
  category: string;
}
export interface AnatomySiteTerm extends FindingVocabTerm {
  parent_id: string | null;
  laterality_applicable: boolean;
}
export interface FindingVocab {
  finding_types: FindingTypeTerm[];
  anatomy_sites: AnatomySiteTerm[];
  morphology_terms: FindingVocabTerm[];
}

export interface FindingGeometryRef {
  id: string;
  marker_id: string | null;
  segmentation_id: string | null;
  role: FindingGeometryRole;
}

export interface FindingMeasurements {
  longest_diameter_mm?: number | null;
  short_axis_mm?: number | null;
  volume_ml?: number | null;
  suv_max?: number | null;
  suv_peak?: number | null;
  suv_mean?: number | null;
  hu_mean?: number | null;
  hu_std?: number | null;
}

export interface Finding extends FindingMeasurements {
  id: string;
  patient_id: string;
  study_id: string;
  series_id: string | null;
  frame_of_reference_uid: string | null;
  finding_type_id: string;
  type: string;
  anatomy_site_id: string | null;
  anatomy: string | null;
  laterality: FindingLaterality | null;
  morphology: string[];
  bbox_lps: Record<string, unknown> | null;
  status: FindingStatus;
  confidence: number | null;
  description: string | null;
  author_subject_id: string | null;
  author_kind: "human" | "agent" | "system";
  model_id: string | null;
  provider: string | null;
  etag: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
  geometry: FindingGeometryRef[];
}

export interface FindingCreateInput extends FindingMeasurements {
  study_id: string;
  series_id?: string | null;
  frame_of_reference_uid?: string | null;
  type: string;
  anatomy?: string | null;
  laterality?: FindingLaterality | null;
  morphology?: string[];
  bbox_lps?: Record<string, unknown> | null;
  status?: FindingStatus;
  confidence?: number | null;
  description?: string | null;
  geometry_refs?: Array<{
    marker_id?: string;
    segmentation_id?: string;
    role: FindingGeometryRole;
  }>;
}

export interface FindingUpdateInput extends FindingMeasurements {
  type?: string;
  anatomy?: string | null;
  laterality?: FindingLaterality | null;
  morphology?: string[];
  bbox_lps?: Record<string, unknown> | null;
  status?: FindingStatus;
  confidence?: number | null;
  description?: string | null;
}

export interface FindingSearchParams {
  study_id?: string;
  type?: string;
  anatomy?: string;
  laterality?: FindingLaterality;
  morphology?: string[];
  status?: FindingStatus;
  min_diameter_mm?: number;
  max_diameter_mm?: number;
  min_volume_ml?: number;
  min_suv_max?: number;
  scope?: "all" | "mine" | "public";
  include_deleted?: boolean;
  limit?: number;
}

export const findingsApi = {
  /** The controlled vocabularies (type / anatomy / morphology slugs). */
  getVocab: () => request<FindingVocab>("/api/findings/vocab"),

  /** List a patient's findings (optionally filtered by study + attributes). */
  list: (patientId: string, params: FindingSearchParams = {}) =>
    request<Finding[]>(
      `/api/patients/${patientId}/findings${qs(params as Record<string, QSValue>)}`,
    ),

  /** Corpus-wide structured search across every readable study. */
  search: (params: FindingSearchParams = {}) =>
    request<Finding[]>(`/api/findings/search${qs(params as Record<string, QSValue>)}`),

  get: (findingId: string) => request<Finding>(`/api/findings/${findingId}`),

  create: (
    patientId: string,
    input: FindingCreateInput,
    opts: { idempotencyKey?: string; dryRun?: boolean } = {},
  ) => {
    const headers = new Headers();
    if (opts.idempotencyKey) headers.set("idempotency-key", opts.idempotencyKey);
    const suffix = opts.dryRun ? "?dry_run=true" : "";
    return request<Finding>(`/api/patients/${patientId}/findings${suffix}`, {
      method: "POST",
      json: input,
      headers,
    });
  },

  update: (findingId: string, input: FindingUpdateInput, etag?: string) => {
    const headers = new Headers();
    if (etag) headers.set("if-match", etag.startsWith('"') ? etag : `"${etag}"`);
    return request<Finding>(`/api/findings/${findingId}`, {
      method: "PATCH",
      json: input,
      headers,
    });
  },

  remove: (findingId: string, etag?: string) => {
    const headers = new Headers();
    if (etag) headers.set("if-match", etag.startsWith('"') ? etag : `"${etag}"`);
    return request<void>(`/api/findings/${findingId}`, { method: "DELETE", headers });
  },
};

// -------- App settings (admin-tunable runtime configuration) --------

export interface AppSetting {
  key: string;
  value: unknown;
  scope: "public" | "admin";
  description: string | null;
  updated_at: string;
  updated_by_subject_id: string | null;
}

export const settingsApi = {
  listPublic: () => request<AppSetting[]>("/api/settings/public"),
  listAll: () => request<AppSetting[]>("/api/admin/settings"),
  upsert: (
    key: string,
    input: {
      value: unknown;
      description?: string | null;
      scope?: "public" | "admin" | null;
    },
  ) =>
    request<AppSetting>(`/api/admin/settings/${encodeURIComponent(key)}`, {
      method: "PATCH",
      json: input,
    }),
};

// -------- Admin user management --------

export interface AdminUser {
  subject_id: string;
  email: string;
  is_admin: boolean;
  is_active: boolean;
  blocked_at: string | null;
  blocked_reason: string | null;
  email_verified_at: string | null;
  mfa_enabled_at: string | null;
  /** Per-user override; null means inherit ``effective_storage_quota_bytes``. */
  storage_quota_bytes: number | null;
  effective_storage_quota_bytes: number;
  storage_used_bytes: number;
  /** Per-user override; null means inherit the platform default. */
  max_concurrent_jobs: number | null;
  active_job_count: number;
  /** Current LLM wallet balance in cents. Populated by the admin
   * endpoint so the dashboard renders both quotas in one place. */
  wallet_balance_cents: number;
  created_at: string;
}

export interface AdminUserListPage {
  items: AdminUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminUserUpdate {
  storage_quota_bytes?: number | null;
  max_concurrent_jobs?: number | null;
  is_active?: boolean | null;
  blocked_reason?: string | null;
  is_admin?: boolean | null;
  clear_storage_quota?: boolean;
  clear_max_concurrent_jobs?: boolean;
}

export interface PlatformDefaults {
  storage_free_tier_bytes: number;
}

export const adminUsersApi = {
  list: (params: { q?: string; blocked?: boolean; limit?: number; offset?: number } = {}) =>
    request<AdminUserListPage>(`/api/admin/users${qs(params as Record<string, QSValue>)}`),
  detail: (subjectId: string) => request<AdminUser>(`/api/admin/users/${subjectId}`),
  update: (subjectId: string, body: AdminUserUpdate) =>
    request<AdminUser>(`/api/admin/users/${subjectId}`, {
      method: "PATCH",
      json: body,
    }),
  remove: (subjectId: string) =>
    request<void>(`/api/admin/users/${subjectId}`, { method: "DELETE" }),
  platformDefaults: () => request<PlatformDefaults>("/api/admin/platform-defaults"),
};

// -------- LLM rate cards (multi-vendor billing, admin-only) --------

export interface LLMRateCard {
  model_id: string;
  provider: string;
  display_name: string;
  input_usd_per_mtok: number;
  output_usd_per_mtok: number;
  cache_read_usd_per_mtok: number;
  cache_creation_usd_per_mtok: number;
  /** Per-model markup percent. ``null`` = inherit tier default. */
  markup_pct: number | null;
  tier_hint: "free" | "standard" | "premium";
  is_active: boolean;
  is_in_house: boolean;
  notes: string | null;
  updated_at: string;
  updated_by_subject_id: string | null;
}

export interface LLMRateCardUpsert {
  model_id: string;
  provider: string;
  display_name: string;
  input_usd_per_mtok: number;
  output_usd_per_mtok: number;
  cache_read_usd_per_mtok?: number;
  cache_creation_usd_per_mtok?: number;
  markup_pct?: number | null;
  tier_hint?: "free" | "standard" | "premium";
  is_active?: boolean;
  is_in_house?: boolean;
  notes?: string | null;
}

export interface LLMProviderStatus {
  name: string;
  configured: boolean;
  description: string;
  note: string | null;
}

export interface LLMTierDefault {
  tier: "free" | "standard" | "premium";
  provider_kind: string;
  model_id: string;
  is_callable: boolean;
}

export interface LLMProviderStatusBundle {
  providers: LLMProviderStatus[];
  tier_defaults: LLMTierDefault[];
}

export const adminLlmRatesApi = {
  list: (onlyActive = false) => {
    const q = onlyActive ? "?only_active=true" : "";
    return request<LLMRateCard[]>(`/api/admin/llm-rates${q}`);
  },
  upsert: (modelId: string, body: LLMRateCardUpsert) =>
    request<LLMRateCard>(`/api/admin/llm-rates/${encodeURIComponent(modelId)}`, {
      method: "PUT",
      json: body,
    }),
  remove: (modelId: string) =>
    request<void>(`/api/admin/llm-rates/${encodeURIComponent(modelId)}`, {
      method: "DELETE",
    }),
  refresh: () =>
    request<{ active_rows_loaded: number }>("/api/admin/llm-rates/refresh", {
      method: "POST",
    }),
  providerStatus: () => request<LLMProviderStatusBundle>("/api/admin/llm-rates/provider-status"),
};

// ---------------------------------------------------------------------------
// Admin LLM prompts — per-locale system prompt overrides backed by
// ``app_settings`` keys ``qna.system_prompt.<locale>``. The default
// (frozen in backend code) is returned alongside the current effective
// text so the UI can diff and offer a restore.
// ---------------------------------------------------------------------------

export interface LlmPromptEntry {
  locale: string;
  default_text: string;
  current_text: string;
  is_override: boolean;
  updated_at: string | null;
  updated_by_subject_id: string | null;
}

export const adminLlmPromptsApi = {
  list: () => request<LlmPromptEntry[]>("/api/admin/llm-prompts"),
  update: (locale: string, value: string) =>
    request<LlmPromptEntry>(`/api/admin/llm-prompts/${encodeURIComponent(locale)}`, {
      method: "PUT",
      json: { value },
    }),
  reset: (locale: string) =>
    request<LlmPromptEntry>(`/api/admin/llm-prompts/${encodeURIComponent(locale)}`, {
      method: "DELETE",
    }),
};

// -------- Document catalog (kinds / provenances / authorities) --------
//
// Single source of truth for the dropdown options that back the
// ``documents`` table FK columns. The frontend used to hard-code three
// parallel lists which drifted from the DB seed; a user could pick a
// kind that did not exist in ``document_kinds`` and the PATCH would
// fail with a 500. See ``backend/src/bvphoenix/api/document_catalog.py``.

export interface DocumentKindEntry {
  id: string;
  /** ``{it: "Referto Radiologico", en: "Radiology Report"}`` etc. */
  display_name: Record<string, string>;
  description: string | null;
  is_active: boolean;
  sort_order: number;
  loinc_code: string | null;
  fhir_resource: string | null;
}

export interface DocumentProvenanceEntry {
  id: string;
  display_name: Record<string, string>;
  description: string | null;
  is_active: boolean;
  sort_order: number;
  is_digital: boolean;
  is_imaging: boolean;
}

export interface DocumentAuthorityEntry {
  id: string;
  display_name: Record<string, string>;
  description: string | null;
  is_active: boolean;
  sort_order: number;
  trust_score: number;
}

export interface DocumentCatalog {
  kinds: DocumentKindEntry[];
  provenances: DocumentProvenanceEntry[];
  authorities: DocumentAuthorityEntry[];
}

export const documentCatalogApi = {
  list: () => request<DocumentCatalog>("/api/document-catalog"),
};

// ---------------------------------------------------------------------------
// Patient Q&A (M5/M6 of the patient Q&A plan).
// ---------------------------------------------------------------------------

export type QnaTier = "free" | "standard" | "premium";

export interface QnaCitation {
  kind: string; // 'document' | 'event' | 'clinical_note' | 'summary' | 'report_content' | 'chunk'
  ref_id: string;
  /** Human-readable label populated server-side from the target row
   *  (e.g. ``Relazione conclusiva 10/04/2026``). Null when the row
   *  has no title column (chunks, summaries) or the lookup missed. */
  title?: string | null;
  /** ISO date (YYYY-MM-DD) extracted from the row's authoritative
   *  date field — event_date, document_date, signed_at, etc. Null
   *  when the row has no meaningful date. */
  date?: string | null;
  /** Literal snippet the LLM cited inside ``[kind:UUID "snippet"]``
   *  markers. The preview pane uses it to highlight the matched span
   *  in the body so the user sees immediately why the source was
   *  cited. Null when the model didn't emit a quote payload. */
  quote?: string | null;
}

export interface QnaToolCall {
  name: string;
  duration_ms: number;
  is_error: boolean;
  result_chars: number;
}

export interface QnaAnswerOut {
  answer_md: string;
  citations: QnaCitation[];
  used_tools: string[];
  iterations: number;
  stop_reason: string;
  tier: QnaTier;
  model_id: string | null;
  usage: Record<string, number>;
  tool_calls: QnaToolCall[];
  effective_tier: QnaTier;
  downgraded: boolean;
  balance_cents: number;
  request_id: string;
}

export interface QnaInsufficientCredits {
  detail: "insufficient_credits";
  balance_cents: number;
  estimated_max_cost_cents: number;
  fallback_available: "free";
  top_up_url: string;
}

export interface QnaChunkHit {
  chunk_id: string;
  source_kind: "document" | "clinical_note" | "summary" | "report_content";
  source_id: string;
  page: number | null;
  excerpt: string;
  score: number;
  author_kind: "human" | "agent" | "system" | "unknown";
  authority_id: string | null;
  document_kind_id: string | null;
}

export interface QnaChunkSearchOut {
  q: string;
  k: number;
  patient_id: string;
  hits: QnaChunkHit[];
}

/** SSE event payload union mirrored from ``backend/api/qna.py``. */
export type QnaSSEEvent =
  | { event: "tier"; data: { tier: QnaTier; downgraded: boolean; request_id: string } }
  | { event: "citation"; data: QnaCitation }
  | { event: "text_delta"; data: { delta: string } }
  | {
      event: "done";
      data: {
        stop_reason: string;
        model_id: string | null;
        usage: Record<string, number>;
        iterations: number;
        used_tools: string[];
        request_id: string;
      };
    }
  | { event: "error"; data: { code: string; message: string } };

/**
 * Open an SSE stream against ``POST /api/patients/{id}/ask``.
 *
 * Parses the text stream chunk-by-chunk into discrete events. Returns
 * the underlying ``Response`` so the caller can read non-stream status
 * codes (notably 402) before iterating.
 */
export async function qnaAskStream(
  patientId: string,
  body: { query: string; lang?: "it" | "en"; model_override?: string | null },
  signal?: AbortSignal,
): Promise<{ response: Response; events: AsyncIterable<QnaSSEEvent> }> {
  const headers = new Headers({
    "content-type": "application/json",
    accept: "text/event-stream",
  });
  const token = getStoredToken();
  if (token) headers.set("authorization", `Bearer ${token}`);

  const response = await fetch(`${API_BASE_URL}/api/patients/${patientId}/ask`, {
    method: "POST",
    headers,
    body: JSON.stringify({ lang: "it", ...body }),
    cache: "no-store",
    signal,
  });

  return { response, events: parseSSE(response) };
}

async function* parseSSE(response: Response): AsyncIterable<QnaSSEEvent> {
  // 402 / 4xx replies arrive as JSON, NOT as SSE. The caller handles
  // those via ``response.status`` / ``response.json()`` before
  // iterating; if we ever land here on a non-2xx the body is empty.
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line ("\n\n"). Each frame
      // is a sequence of "field: value\n" lines. We only consume
      // ``event:`` and ``data:``; ``id:`` and ``retry:`` are ignored.
      while (true) {
        const sep = buffer.indexOf("\n\n");
        if (sep < 0) break;
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const ev = parseFrame(frame);
        if (ev) yield ev;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseFrame(frame: string): QnaSSEEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(dataLines.join("\n"));
  } catch {
    return null;
  }
  return { event, data: parsed } as QnaSSEEvent;
}

// User-facing tier preference. Backend: src/bvphoenix/api/ai_tier.py.
export interface AiTierStatus {
  effective_tier: QnaTier;
  user_override: QnaTier | null;
  workspace_default: QnaTier;
  allow_user_override: boolean;
}

export const aiTierApi = {
  status(): Promise<AiTierStatus> {
    return request<AiTierStatus>("/api/me/ai-tier");
  },
  set(tier: QnaTier | null): Promise<AiTierStatus> {
    return request<AiTierStatus>("/api/me/ai-tier", {
      method: "PUT",
      json: { tier },
    });
  },
};

export const qnaApi = {
  /** Block-and-return JSON path. Throws ``ApiError`` (with status=402)
   *  when the wallet gate refuses the call. */
  ask(patientId: string, body: { query: string; lang?: "it" | "en" }): Promise<QnaAnswerOut> {
    return request<QnaAnswerOut>(`/api/patients/${patientId}/ask`, {
      method: "POST",
      json: { lang: "it", ...body },
    });
  },
  /** Open the SSE stream — see :func:`qnaAskStream`. */
  askStream: qnaAskStream,
  /** Sub-document chunk search. Used by the freemium FE path and by
   *  any caller that needs raw retrieval results without LLM. */
  searchChunks(
    patientId: string,
    params: {
      q: string;
      k?: number;
      source_kind?: string[];
      author_kind?: string[];
      exclude_ai?: boolean;
      authority_id?: string[];
      document_kind_id?: string[];
      since?: string;
      until?: string;
      source_id?: string;
    },
  ): Promise<QnaChunkSearchOut> {
    const query = new URLSearchParams();
    query.set("q", params.q);
    if (params.k !== undefined) query.set("k", String(params.k));
    if (params.exclude_ai) query.set("exclude_ai", "true");
    for (const key of ["source_kind", "author_kind", "authority_id", "document_kind_id"] as const) {
      const arr = params[key];
      if (arr) for (const v of arr) query.append(key, v);
    }
    for (const key of ["since", "until", "source_id"] as const) {
      const v = params[key];
      if (v) query.set(key, v);
    }
    return request<QnaChunkSearchOut>(
      `/api/patients/${patientId}/search/chunks?${query.toString()}`,
    );
  },
};

// ---------------------------------------------------------------------------
// Storage quota — per-user usage + cap.
// ---------------------------------------------------------------------------

export interface StorageTopPatient {
  patient_id: string;
  display_name: string | null;
  bytes_used: number;
}

export interface StorageUsageOut {
  bytes_used: number;
  bytes_quota: number;
  quota_gb: number;
  used_gb: number;
  /** 0..999.9 — UI bar caps at 100 visually, but >100 indicates an
   *  admin lowered the quota retroactively below the existing usage. */
  percent: number;
  is_workspace_default: boolean;
  top_patients: StorageTopPatient[];
}

export const storageApi = {
  usage(): Promise<StorageUsageOut> {
    return request<StorageUsageOut>("/api/me/storage");
  },
};

// ---------------------------------------------------------------------------
// Wallet sponsorships
// ---------------------------------------------------------------------------

export type SponsorshipScopeKind = "patient" | "consultation" | "organization" | "global";

export interface SponsorshipDefaultsOut {
  default_cap_cents: number;
  ceiling_cents: number | null;
  scope_kinds: SponsorshipScopeKind[];
}

export interface SponsorshipOut {
  id: string;
  sponsor_subject_id: string;
  sponsored_subject_id: string;
  scope_kind: SponsorshipScopeKind;
  scope_id: string | null;
  cap_cents: number;
  spent_cents: number;
  remaining_cents: number;
  valid_from: string;
  valid_until: string | null;
  revoked_at: string | null;
  purpose: string | null;
  created_at: string;
}

export interface SponsorshipCreateIn {
  sponsored_subject_id: string;
  scope_kind: SponsorshipScopeKind;
  scope_id: string | null;
  cap_cents?: number;
  valid_until?: string | null;
  purpose?: string | null;
}

export const sponsorshipsApi = {
  defaults(): Promise<SponsorshipDefaultsOut> {
    return request<SponsorshipDefaultsOut>("/api/me/sponsorships/defaults");
  },
  emitted(includeRevoked = false): Promise<SponsorshipOut[]> {
    const qs = includeRevoked ? "?include_revoked=true" : "";
    return request<SponsorshipOut[]>(`/api/me/sponsorships/emitted${qs}`);
  },
  received(includeRevoked = false): Promise<SponsorshipOut[]> {
    const qs = includeRevoked ? "?include_revoked=true" : "";
    return request<SponsorshipOut[]>(`/api/me/sponsorships/received${qs}`);
  },
  create(body: SponsorshipCreateIn): Promise<SponsorshipOut> {
    return request<SponsorshipOut>("/api/me/sponsorships", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },
  updateCap(id: string, capCents: number): Promise<SponsorshipOut> {
    return request<SponsorshipOut>(`/api/me/sponsorships/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ cap_cents: capCents }),
    });
  },
  revoke(id: string): Promise<SponsorshipOut> {
    return request<SponsorshipOut>(`/api/me/sponsorships/${id}`, {
      method: "DELETE",
    });
  },
};

// -------- build info --------

/**
 * Build identity baked into the backend image at build time and
 * exposed by GET /api/version. The same values are surfaced on
 * /settings so an operator can confirm at a glance which release
 * is live, link the SHA to the corresponding GitHub commit, and copy
 * the version/SHA for a bug report.
 */
export interface BuildInfo {
  service: string;
  version: string;
  git_sha: string;
  git_sha_short: string;
  build_date: string;
  python_version: string;
}

export const versionApi = {
  get(): Promise<BuildInfo> {
    return request<BuildInfo>("/api/version");
  },
};

// ---------------------------------------------------------------------------
// Longitudinal tumour comparison: lesion tracks, response assessments,
// registrations.
// ---------------------------------------------------------------------------

function _idemHeaders(): Headers {
  const h = new Headers();
  h.set("idempotency-key", crypto.randomUUID());
  return h;
}

function _ifMatchHeaders(etag?: string): Headers | undefined {
  if (!etag) return undefined;
  const h = new Headers();
  h.set("if-match", `"${etag}"`);
  return h;
}

export type LesionTrackPointKind = "human" | "agent" | "system";

export interface LesionTrackPoint {
  id: string;
  finding_id: string;
  is_baseline: boolean;
  timepoint_date: string | null;
  registration_id: string | null;
  linked_by_kind: LesionTrackPointKind;
  confidence: number | null;
  created_at: string;
}

export type RecistRole = "target" | "non_target" | "new" | "not_evaluable";

export interface LesionTrack {
  id: string;
  patient_id: string;
  label: string;
  anatomy_site_id: string | null;
  anatomy: string | null;
  laterality: string | null;
  finding_type_id: string | null;
  type: string | null;
  recist_role: RecistRole | null;
  status: "active" | "resolved" | "retracted";
  description: string | null;
  author_subject_id: string | null;
  author_kind: "human" | "agent" | "system";
  model_id: string | null;
  provider: string | null;
  etag: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
  points: LesionTrackPoint[];
}

export type GrowthDirection = "increase" | "decrease" | "stable" | "unknown";

export interface TrajectoryTimepoint {
  point_id: string;
  finding_id: string;
  measured_on: string | null;
  is_baseline: boolean;
  volume_ml: number | null;
  longest_diameter_mm: number | null;
  short_axis_mm: number | null;
  suv_max: number | null;
  delta_from_baseline: Record<string, number | null> | null;
  delta_from_previous: Record<string, number | null> | null;
  direction: GrowthDirection;
}

export interface LesionTrajectory {
  baseline: Record<string, unknown> | null;
  latest: Record<string, unknown> | null;
  timepoints: TrajectoryTimepoint[];
  summary: {
    n_timepoints: number;
    span_days: number | null;
    volume_pct_change_total: number | null;
    diameter_pct_change_total: number | null;
    doubling_time_days: number | null;
    overall_direction: GrowthDirection;
  } | null;
}

export interface LesionTrackCreateInput {
  label: string;
  type?: string;
  anatomy?: string;
  laterality?: string;
  recist_role?: RecistRole;
  status?: string;
  description?: string;
  baseline_finding_id?: string;
}

export const lesionTracksApi = {
  list: (patientId: string, params: Record<string, QSValue> = {}) =>
    request<LesionTrack[]>(`/api/patients/${patientId}/lesion-tracks${qs(params)}`),
  get: (id: string) => request<LesionTrack>(`/api/lesion-tracks/${id}`),
  create: (patientId: string, input: LesionTrackCreateInput) =>
    request<LesionTrack>(`/api/patients/${patientId}/lesion-tracks`, {
      method: "POST",
      json: input,
      headers: _idemHeaders(),
    }),
  update: (id: string, input: Record<string, unknown>, etag?: string) =>
    request<LesionTrack>(`/api/lesion-tracks/${id}`, {
      method: "PATCH",
      json: input,
      headers: _ifMatchHeaders(etag),
    }),
  remove: (id: string, etag?: string) =>
    request<void>(`/api/lesion-tracks/${id}`, {
      method: "DELETE",
      headers: _ifMatchHeaders(etag),
    }),
  addPoint: (
    id: string,
    input: { finding_id: string; is_baseline?: boolean; registration_id?: string },
  ) => request<LesionTrack>(`/api/lesion-tracks/${id}/points`, { method: "POST", json: input }),
  removePoint: (id: string, pointId: string) =>
    request<void>(`/api/lesion-tracks/${id}/points/${pointId}`, { method: "DELETE" }),
  trajectory: (id: string) => request<LesionTrajectory>(`/api/lesion-tracks/${id}/trajectory`),
  propagate: (id: string, input: { followup_series_id: string; refine?: boolean }) =>
    request<{ status: string; job_id?: string; track_id: string }>(
      `/api/lesion-tracks/${id}/propagate`,
      { method: "POST", json: input },
    ),
};

export type ResponseCategory = "CR" | "PR" | "SD" | "PD" | "NE";

export interface ResponseAssessment {
  id: string;
  patient_id: string;
  assessment_date: string | null;
  baseline_study_id: string | null;
  current_study_id: string | null;
  criterion: "recist_1_1" | "volumetric" | "percist";
  target_sum_mm: number | null;
  baseline_sum_mm: number | null;
  nadir_sum_mm: number | null;
  target_sum_pct_change: number | null;
  volume_total_ml: number | null;
  volume_pct_change: number | null;
  category: ResponseCategory;
  new_lesions: boolean;
  non_target_status: string | null;
  basis: Record<string, unknown> | null;
  notes: string | null;
  author_subject_id: string | null;
  author_kind: "human" | "agent" | "system";
  model_id: string | null;
  provider: string | null;
  etag: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export const responseAssessmentsApi = {
  list: (patientId: string, params: Record<string, QSValue> = {}) =>
    request<ResponseAssessment[]>(`/api/patients/${patientId}/response-assessments${qs(params)}`),
  get: (id: string) => request<ResponseAssessment>(`/api/response-assessments/${id}`),
  create: (
    patientId: string,
    input: {
      current_study_id: string;
      baseline_study_id?: string;
      criterion?: string;
      notes?: string;
    },
  ) =>
    request<ResponseAssessment>(`/api/patients/${patientId}/response-assessments`, {
      method: "POST",
      json: input,
      headers: _idemHeaders(),
    }),
  recompute: (id: string) =>
    request<ResponseAssessment>(`/api/response-assessments/${id}/recompute`, {
      method: "POST",
      json: {},
    }),
  update: (id: string, input: Record<string, unknown>, etag?: string) =>
    request<ResponseAssessment>(`/api/response-assessments/${id}`, {
      method: "PATCH",
      json: input,
      headers: _ifMatchHeaders(etag),
    }),
  remove: (id: string, etag?: string) =>
    request<void>(`/api/response-assessments/${id}`, {
      method: "DELETE",
      headers: _ifMatchHeaders(etag),
    }),
};

export interface Registration {
  id: string;
  fixed_series_id: string;
  moving_series_id: string;
  kind: "rigid" | "demons";
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  job_id: string | null;
  download_url: string | null;
  // ``result_meta.lps_matrix`` is the 4x4 fixed→moving LPS map (rigid only),
  // used to synchronise crosshairs across two studies.
  result_meta: { lps_matrix?: number[][]; lps_maps?: string } & Record<string, unknown>;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  // Live progress mirrored from the linked Job so the viewer shows real
  // status instead of a blind spinner: stage in queued|loading|registering|uploading.
  stage?: string | null;
  progress_done?: number | null;
  progress_total?: number | null;
}

export const registrationsApi = {
  create: (input: {
    fixed_series_id: string;
    moving_series_id: string;
    kind?: "rigid" | "demons";
  }) =>
    request<Registration>("/api/registrations", {
      method: "POST",
      json: { kind: "rigid", ...input },
    }),
  get: (id: string) => request<Registration>(`/api/registrations/${id}`),
  cancel: (id: string) =>
    request<Registration>(`/api/registrations/${id}/cancel`, { method: "POST" }),
};
