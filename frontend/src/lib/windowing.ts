// Advanced auto-windowing and per-modality W/L presets for the viewer.
//
// Three tiers of "what window should we use?" — ordered by preference:
//   1. suggestedFromDicom(series)   — DICOM tags WC/WW, the radiologist's
//      own window from the acquisition site.
//   2. modalityDefaults(modality)   — a list of clinical presets filtered
//      by modality + body part.
//   3. computeAutoWL(volume)        — robust percentile clipping over the
//      voxel histogram when nothing else is available.
//
// No numpy, no extra libs — raw Float32Array loops. Histogram uses a
// fixed 1024-bin approximation which is fine for 12–16 bit CT/MR data
// and keeps the cost roughly O(N) with one extra O(bins) scan.

export interface WLPreset {
  label: string;
  wc: number;
  ww: number;
}

export interface WLValue {
  wc: number;
  ww: number;
}

// Only the fields we actually read — kept loose so callers don't need
// to import the whole Series type.
export interface SeriesLike {
  modality?: string | null;
  body_part_examined?: string | null;
  suggested_wc?: number | null;
  suggested_ww?: number | null;
}

/**
 * Compute a sensible default window from the voxel histogram.
 *
 * Uses percentile clipping (1% .. 99%) so a handful of metal artifacts
 * or air voxels can't blow out the dynamic range. Returns NaN-safe
 * defaults for empty / constant-valued volumes.
 *
 * For PT (PET), the diagnostic signal lives in the *upper tail* of
 * the histogram — normal tissue is uniform background, lesions are
 * the bright outliers. The standard 1-99 window flattens contrast
 * across the whole body and lesions blend in with normal tissue.
 * When ``modality === "PT"``, we instead window from the 50th
 * percentile (median active tissue) to the 99.5th (lesion peak),
 * exactly the trick a clinical PACS uses for its "PT Hot Body" view.
 */
export function computeAutoWL(volume: Float32Array, modality?: string): WLValue {
  if (!volume || volume.length === 0) {
    return { wc: 0, ww: 1 };
  }

  // First pass: true min / max. We need the full range before we can
  // size the histogram bins.
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (let i = 0; i < volume.length; i++) {
    const v = volume[i];
    if (v < min) min = v;
    if (v > max) max = v;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
    return { wc: (min + max) / 2 || 0, ww: 1 };
  }

  // Second pass: 1024-bin histogram.
  const BINS = 1024;
  const scale = BINS / (max - min);
  const hist = new Uint32Array(BINS);
  for (let i = 0; i < volume.length; i++) {
    // Clamp on the edge: (v - min) * scale can equal BINS at the max.
    let b = Math.floor((volume[i] - min) * scale);
    if (b < 0) b = 0;
    else if (b >= BINS) b = BINS - 1;
    hist[b]++;
  }

  // Third pass: walk CDF to find percentile bin edges. PT skews to
  // the upper tail because the diagnostic value sits in the lesion
  // peak; everything else is uniform "warm body" background.
  const isPT = (modality || "").toUpperCase() === "PT";
  const total = volume.length;
  const lowTarget = total * (isPT ? 0.5 : 0.01);
  const highTarget = total * (isPT ? 0.995 : 0.99);
  let running = 0;
  let lowBin = 0;
  let highBin = BINS - 1;
  for (let b = 0; b < BINS; b++) {
    running += hist[b];
    if (running >= lowTarget) {
      lowBin = b;
      break;
    }
  }
  running = 0;
  for (let b = 0; b < BINS; b++) {
    running += hist[b];
    if (running >= highTarget) {
      highBin = b;
      break;
    }
  }

  const lo = min + lowBin / scale;
  const hi = min + (highBin + 1) / scale;
  const ww = Math.max(1, hi - lo);
  const wc = (lo + hi) / 2;
  return { wc, ww };
}

// Static tables of clinically standard presets. Values come from the
// published literature (Siemens, GE, Philips documentation) and
// the radiopaedia reference charts — the same numbers a clinical PACS
// ships with out of the box.
const CT_PRESETS: WLPreset[] = [
  { label: "CT Abdomen", wc: 40, ww: 400 },
  { label: "CT Lung", wc: -600, ww: 1500 },
  { label: "CT Bone", wc: 400, ww: 1800 },
  { label: "CT Brain", wc: 40, ww: 80 },
  { label: "CT Soft Tissue", wc: 40, ww: 350 },
  { label: "CT Mediastinum", wc: 50, ww: 350 },
  { label: "CT Angio", wc: 300, ww: 600 },
  // ------------------------------------------------------------------
  // Liver-specific presets, ordered by clinical phase. The plain
  // "CT Liver" entry is the workhorse portal-venous review window
  // (parenchyma vs. portal/hepatic veins). The phase variants give
  // the radiologist one-click access to the windows that match the
  // contrast timing of the acquired phase:
  //   - non-contrast: narrow window centred on parenchyma HU
  //     (~50–55 HU baseline) to spot fat/iron/dense lesions;
  //   - arterial: wider window to catch hyperenhancing HCC / FNH /
  //     hepatic adenoma against the parenchymal background;
  //   - portal venous: standard review window — same numbers as
  //     "CT Liver" — parenchyma 100–110 HU, lesions hypodense;
  //   - delayed / equilibrium: narrower window emphasising washout
  //     and capsular enhancement;
  //   - HCC narrow: very narrow window targeting subtle late-arterial
  //     hyperenhancement of small HCCs (≤2 cm).
  { label: "CT Liver", wc: 60, ww: 160 },
  { label: "CT Liver non-contrast", wc: 50, ww: 150 },
  { label: "CT Liver arterial", wc: 80, ww: 250 },
  { label: "CT Liver portal venous", wc: 60, ww: 160 },
  { label: "CT Liver delayed", wc: 70, ww: 140 },
  { label: "CT Liver narrow (HCC)", wc: 80, ww: 100 },
  // ------------------------------------------------------------------
  // Kidney / renal presets. Multi-phase renal CT is the typical
  // case (CMP / nephrographic / excretory). The "CT Kidney" plain
  // entry is the parenchymal default radiologists land on for a
  // generic abdomen-with-renal-question study.
  //   - corticomedullary: peak cortical enhancement ~30 s post-
  //     injection, broad window to span cortex (~140 HU) vs.
  //     medulla (~70 HU);
  //   - nephrographic: ~80 s, homogeneous parenchyma — narrow
  //     window for lesion detection (RCC stand-out);
  //   - excretory / urographic: contrast in collecting system
  //     (>500 HU), wide window so the cortex isn't blown out;
  //   - stone: very narrow non-contrast window to highlight high-
  //     density calculi against the soft-tissue background.
  { label: "CT Kidney", wc: 50, ww: 350 },
  { label: "CT Kidney corticomedullary", wc: 60, ww: 400 },
  { label: "CT Kidney nephrographic", wc: 50, ww: 200 },
  { label: "CT Kidney excretory", wc: 100, ww: 600 },
  { label: "CT Kidney stone", wc: 40, ww: 400 },
  // ------------------------------------------------------------------
  // Lung / chest variants. The plain "CT Lung" above is the standard
  // parenchymal window. Variants:
  //   - HRCT: high-resolution interstitial review (slightly
  //     brighter centre, broader range);
  //   - emphysema: emphasises low-attenuation foci (centrilobular
  //     bullae) by clipping at -950 HU;
  //   - airways: tight window for tracheobronchial wall thickening
  //     and bronchiectasis.
  { label: "CT Lung HRCT", wc: -600, ww: 1600 },
  { label: "CT Lung emphysema", wc: -800, ww: 600 },
  { label: "CT Lung airways", wc: -450, ww: 1400 },
  // ------------------------------------------------------------------
  // Other commonly-windowed organs.
  //   - Pancreas: narrow window highlighting parenchymal
  //     enhancement vs. peripancreatic fat / vessels in the
  //     pancreatic-phase acquisition;
  //   - Adrenal: like pancreas — small, soft-tissue lesions
  //     against retroperitoneal fat;
  //   - Spleen: parenchyma is homogeneous in portal phase, narrow
  //     window picks up infarcts / lymphoma deposits.
  { label: "CT Pancreas", wc: 40, ww: 350 },
  { label: "CT Adrenal", wc: 40, ww: 350 },
  { label: "CT Spleen", wc: 60, ww: 200 },
];

const MR_PRESETS: WLPreset[] = [
  { label: "MR T1", wc: 500, ww: 1000 },
  { label: "MR T2", wc: 1000, ww: 2000 },
  { label: "MR FLAIR", wc: 400, ww: 800 },
  { label: "MR DWI", wc: 600, ww: 1200 },
];

const PT_PRESETS: WLPreset[] = [
  // SUV-scale presets, applicable when the image's pixel-to-SUV
  // factor is known (vendor-encoded BQML or via a known SUV Scale
  // Factor). For files in raw CNTS without a SUV factor these don't
  // map to anything meaningful — the user should use ``Auto W/L``,
  // which the code paths up the stack flip to a 50-99.5 percentile
  // window on PT data.
  { label: "PT SUV body", wc: 2.5, ww: 5 },
  { label: "PT SUV hot lesions", wc: 5, ww: 8 },
  { label: "PT SUV brain", wc: 7, ww: 14 },
  { label: "PT SUV myocardium", wc: 8, ww: 16 },
];

/**
 * Return the list of presets appropriate for a given modality.
 *
 * ``bodyPart`` is optional and, when provided, promotes the matching
 * preset to the front so the common case is a single click. Unknown
 * modalities return all presets concatenated so the user can still
 * pick manually.
 */
export function modalityDefaults(modality: string, bodyPart?: string): WLPreset[] {
  const m = (modality || "").toUpperCase();
  const bp = (bodyPart || "").toLowerCase();

  let base: WLPreset[];
  if (m === "CT") base = [...CT_PRESETS];
  else if (m === "MR" || m === "MRI") base = [...MR_PRESETS];
  else if (m === "PT" || m === "PET") base = [...PT_PRESETS];
  else base = [...CT_PRESETS, ...MR_PRESETS, ...PT_PRESETS];

  if (!bp) return base;

  // Bubble the most relevant preset to the top. We look for a token
  // from the body-part string in the preset label.
  const priority = (label: string): number => {
    const l = label.toLowerCase();
    if (bp.includes("lung") || bp.includes("chest") || bp.includes("thorax")) {
      if (l === "ct lung") return 0;
      if (l.includes("lung hrct")) return 1;
      if (l.includes("lung emphysema")) return 2;
      if (l.includes("lung airways")) return 3;
      if (l.includes("mediastinum")) return 4;
    }
    if (bp.includes("liver") || bp.includes("hepat")) {
      // Portal-venous workhorse window first, then the phase
      // variants in clinical-acquisition order, then generic
      // abdomen as fallback.
      if (l === "ct liver") return 0;
      if (l.includes("liver portal")) return 1;
      if (l.includes("liver arterial")) return 2;
      if (l.includes("liver delayed")) return 3;
      if (l.includes("liver non-contrast")) return 4;
      if (l.includes("liver narrow")) return 5;
      if (l.includes("abdomen")) return 6;
    }
    if (bp.includes("kidney") || bp.includes("renal")) {
      if (l === "ct kidney") return 0;
      if (l.includes("kidney nephrographic")) return 1;
      if (l.includes("kidney corticomedullary")) return 2;
      if (l.includes("kidney excretory")) return 3;
      if (l.includes("kidney stone")) return 4;
      if (l.includes("abdomen")) return 5;
    }
    if (bp.includes("pancreas") || bp.includes("pancreat")) {
      if (l.includes("pancreas")) return 0;
      if (l.includes("abdomen")) return 1;
    }
    if (bp.includes("adrenal")) {
      if (l.includes("adrenal")) return 0;
      if (l.includes("abdomen")) return 1;
    }
    if (bp.includes("spleen") || bp.includes("splen")) {
      if (l.includes("spleen")) return 0;
      if (l.includes("abdomen")) return 1;
    }
    if (bp.includes("abdomen") || bp.includes("pelvis")) {
      if (l.includes("abdomen")) return 0;
      if (l === "ct liver") return 1;
      if (l === "ct kidney") return 2;
      if (l.includes("pancreas")) return 3;
      if (l.includes("spleen")) return 4;
      if (l.includes("adrenal")) return 5;
    }
    if (bp.includes("head") || bp.includes("brain") || bp.includes("skull")) {
      if (l.includes("brain")) return 0;
    }
    if (bp.includes("bone") || bp.includes("spine") || bp.includes("extremity")) {
      if (l.includes("bone")) return 0;
    }
    if (bp.includes("angio") || bp.includes("vessel") || bp.includes("aorta")) {
      if (l.includes("angio")) return 0;
    }
    return 100;
  };
  base.sort((a, b) => priority(a.label) - priority(b.label));
  return base;
}

// Maps a classified contrast/acquisition phase to the W/L preset whose
// window matches that phase's contrast timing. Region-aware: the same
// phase label windows differently for liver vs kidney protocols. Only
// phases with a dedicated, literature-grounded CT window are mapped;
// phases without one (hepatobiliary = MR Gd-EOB; dynamic; other) return
// null so the caller falls back to ``modalityDefaults`` / ``computeAutoWL``.
const PHASE_PRESET_LABEL: Record<string, { hepatic?: string; renal?: string }> = {
  unenhanced: { hepatic: "CT Liver non-contrast", renal: "CT Kidney" },
  arterial: { hepatic: "CT Liver arterial" },
  portal_venous: { hepatic: "CT Liver portal venous" },
  delayed: { hepatic: "CT Liver delayed" },
  corticomedullary: { renal: "CT Kidney corticomedullary" },
  nephrographic: { renal: "CT Kidney nephrographic" },
  excretory: { renal: "CT Kidney excretory" },
};

const RENAL_RE = /kidney|renal|rene|urogr|uro|nephro|nefro/i;

/**
 * Return the W/L preset that matches a classified contrast phase, or
 * null when the phase has no dedicated CT window. ``bodyPart`` selects
 * the liver vs kidney window family for the same phase label.
 *
 * Used by the multiphase viewer to auto-window each phase pane: an
 * arterial liver series opens on "CT Liver arterial", a nephrographic
 * renal series on "CT Kidney nephrographic", etc.
 */
export function presetForPhase(
  phase: string | null | undefined,
  bodyPart?: string | null,
): WLPreset | null {
  if (!phase) return null;
  const entry = PHASE_PRESET_LABEL[phase];
  if (!entry) return null;
  const region = RENAL_RE.test(bodyPart || "") ? "renal" : "hepatic";
  const label = entry[region] ?? entry.hepatic ?? entry.renal;
  if (!label) return null;
  return CT_PRESETS.find((p) => p.label === label) ?? null;
}

/**
 * Pull the site-suggested WC/WW from series metadata if present.
 *
 * The backend fills these from the middle instance's DICOM WindowCenter
 * (0028,1050) / WindowWidth (0028,1051) tags — see ``studies.py``.
 * Returns ``null`` when the tags were absent (not every dataset has
 * them, and some scanners write garbage we'd rather skip).
 */
export function suggestedFromDicom(series: SeriesLike | null | undefined): WLValue | null {
  if (!series) return null;
  const wc = series.suggested_wc;
  const ww = series.suggested_ww;
  if (wc == null || ww == null) return null;
  if (!Number.isFinite(wc) || !Number.isFinite(ww)) return null;
  if (ww <= 0) return null;
  return { wc, ww };
}
