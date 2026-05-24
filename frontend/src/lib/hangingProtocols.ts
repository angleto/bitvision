/**
 * Hanging protocols — preset viewport layouts for medical imaging review.
 *
 * A hanging protocol defines a grid layout and which planes/views to
 * populate each cell with. Radiologists use them to jump straight to a
 * familiar arrangement when opening a study.
 */

export type LayoutId = "1x1" | "1x2" | "2x2" | "2x3" | "3x3";

export type Plane = "axial" | "sagittal" | "coronal" | "3d" | "mip";

export interface HangingProtocol {
  id: string;
  label: string;
  layout: LayoutId;
  /** Planes assigned to each cell, in row-major order. */
  planes: Plane[];
  /** When set, on protocol activation the viewer auto-loads the first
   *  fusion candidate of this modality as overlay. Used by PET-CT review:
   *  open the PET, the CT companion is loaded automatically beneath. */
  autoFuseModality?: string;
}

/** Grid dimensions (cols, rows) for each layout id. */
export const LAYOUT_DIMS: Record<LayoutId, [number, number]> = {
  "1x1": [1, 1],
  "1x2": [2, 1],
  "2x2": [2, 2],
  "2x3": [3, 2],
  "3x3": [3, 3],
};

export const HANGING_PROTOCOLS: HangingProtocol[] = [
  {
    id: "ct-mpr-3d",
    label: "CT · MPR + 3D (2×2)",
    layout: "2x2",
    planes: ["axial", "sagittal", "coronal", "3d"],
  },
  {
    id: "ct-pt-fused",
    label: "CT · MPR, PT auto-fuse (2×2)",
    layout: "2x2",
    planes: ["axial", "sagittal", "coronal", "3d"],
    autoFuseModality: "PT",
  },
  {
    id: "ct-pt-mip",
    label: "CT · MPR + PT-MIP, PT auto-fuse (2×2)",
    layout: "2x2",
    // CT primary with PT fusion overlay; the fourth pane is the
    // rotating MIP, which the layout knows to source from the PT
    // (fusion) volume rather than the CT (primary). Standard
    // PET-CT review workflow.
    planes: ["axial", "sagittal", "coronal", "mip"],
    autoFuseModality: "PT",
  },
  {
    id: "pt-ct-fused",
    label: "PT · MPR + 3D, CT auto-fuse (2×2)",
    layout: "2x2",
    planes: ["axial", "sagittal", "coronal", "3d"],
    autoFuseModality: "CT",
  },
  {
    id: "pt-ct-mip",
    label: "PT · MPR + MIP, CT auto-fuse (2×2)",
    layout: "2x2",
    planes: ["axial", "sagittal", "coronal", "mip"],
    autoFuseModality: "CT",
  },
  {
    id: "mr-axial-sagittal",
    label: "MR · Axial + Sagittal (1×2)",
    layout: "1x2",
    planes: ["axial", "sagittal"],
  },
  {
    id: "xr-single",
    label: "XR · Single view (1×1)",
    layout: "1x1",
    planes: ["axial"],
  },
  {
    id: "axial-only",
    label: "Axial only (1×1)",
    layout: "1x1",
    planes: ["axial"],
  },
  {
    id: "mpr-triple",
    label: "MPR triple (1×2 + coronal)",
    layout: "2x2",
    planes: ["axial", "sagittal", "coronal"],
  },
  {
    id: "compare-2x3",
    label: "Compare (2×3)",
    layout: "2x3",
    planes: ["axial", "sagittal", "coronal", "3d"],
  },
  {
    id: "grid-3x3",
    label: "Grid 3×3",
    layout: "3x3",
    planes: ["axial", "sagittal", "coronal", "3d"],
  },
];

/**
 * Plane the slice was acquired on, as reported by
 * ``DisplayMetadata.primary_plane``. ``unknown`` is the routing hint
 * for non-volumetric / 2D-only series (single SC, scout, structured
 * report, etc.); the caller should open the 2D viewer instead of MPR.
 */
export type PrimaryPlane = "axial" | "sagittal" | "coronal" | "oblique" | "unknown";

export interface ProtocolHints {
  /** Number of received instances in the series. ``<= 1`` skips MPR
   *  and forces a single-pane layout. */
  instanceCount?: number | null;
  /** Backend-derived acquisition plane. When provided and not
   *  ``"unknown"``, the layout's plane order is reshuffled so the
   *  acquisition plane is the primary cell (sagittal-acquired spine
   *  MR opens with sagittal as the big pane). */
  primaryPlane?: PrimaryPlane | null;
}

/**
 * Pick a sensible default protocol for a series. The primary signal is
 * the modality; when ``hints.primaryPlane`` is supplied (post
 * display-metadata), the MPR plane order is rewritten so the
 * acquisition plane drives the big pane. Single-slice / non-volumetric
 * series collapse to a 1×1 ``xr-single`` layout regardless of modality.
 */
export function pickDefaultProtocol(
  modality: string | null | undefined,
  hints: ProtocolHints = {},
): HangingProtocol {
  const mod = (modality ?? "").toUpperCase().trim();
  const { instanceCount, primaryPlane } = hints;
  // Single-slice / non-volumetric → flat 1×1. Same shape we already
  // give to plain radiographs: there is no z to step through, so a
  // multi-pane MPR layout would just waste screen real-estate.
  if (primaryPlane === "unknown" || (typeof instanceCount === "number" && instanceCount <= 1)) {
    return byId("xr-single");
  }
  // XR family — plain radiographs, CR, DX, mammography.
  if (["XR", "CR", "DX", "MG", "RF"].includes(mod)) {
    return byId("xr-single");
  }
  let proto: HangingProtocol;
  if (mod === "MR" || mod === "MRI") {
    proto = byId("mr-axial-sagittal");
  } else if (mod === "PT" || mod === "PET" || mod === "NM") {
    // PT primary: default to MPR + 3D, CT auto-fuses underneath.
    //
    // Was MPR + MIP (``pt-ct-mip``) until beta.81 chased a residual
    // race in the shared-engine MIP path: ``CornerstoneMPRLayout``
    // ran setVolumesForViewports twice on the shared engine (once
    // primary-only, once with fusion when it landed) and the engine
    // state from the first pass left the MIP white at first mount,
    // sistemato solo da toggle off+on. Quattro fix successivi
    // (beta.72 fusionPending gate, beta.73 kick rAF, beta.74 shared
    // vtkOpenGLTexture, beta.75 single-frame resize) hanno chiuso
    // sintomi diversi ma non la doppia-passata sul MPR; la quinta
    // iterazione (beta.81 fusionExpected URL prop) ha gattato la MIP
    // ma il MPR continua a doppiare. Promuoviamo ``pt-ct-fused``
    // (3D pane invece di MIP) come default così il primo render è
    // sempre corretto. La variante MIP resta selezionabile dal
    // protocol picker per chi la vuole esplicitamente.
    proto = byId("pt-ct-fused");
  } else if (mod === "CT") {
    // CT primary: when the study has a PT sibling (typical Whole-
    // Body PET/CT or oncologic restaging), auto-fuse PT and show
    // the 3D rendering in the fourth pane. Switched from
    // ``ct-pt-mip`` (MIP fourth pane) for the same shared-engine
    // race documented above on PT primary; ``ct-pt-mip`` stays
    // selectable from the protocol picker.
    proto = byId("ct-pt-fused");
  } else {
    proto = byId("axial-only");
  }
  // If the slice was acquired on a non-axial plane, promote that
  // plane to the primary cell. The default ``planes`` order assumes
  // axial primary; ``reorderForPrimary`` is a no-op when the protocol
  // already opens on the supplied plane (or when the plane is unknown
  // / oblique — we keep axial primary in those cases).
  return reorderForPrimary(proto, primaryPlane ?? null);
}

/**
 * Return ``proto`` with its ``planes`` array reshuffled so the first
 * cell renders ``primary``. Other cells retain their original order
 * minus the promoted plane. Non-plane cells (3d, mip) are left in
 * place. Returns ``proto`` unchanged when ``primary`` isn't axial /
 * sagittal / coronal, or when it isn't in the protocol at all.
 */
export function reorderForPrimary(
  proto: HangingProtocol,
  primary: PrimaryPlane | null,
): HangingProtocol {
  if (primary !== "sagittal" && primary !== "coronal") {
    // axial / oblique / unknown / null → keep the protocol's natural
    // order. Oblique acquisitions are still reviewed off the axial
    // reformat (the oblique pane is opt-in via the toolbar).
    return proto;
  }
  if (!proto.planes.includes(primary)) return proto;
  const rest = proto.planes.filter((p) => p !== primary);
  return { ...proto, planes: [primary, ...rest] };
}

function byId(id: string): HangingProtocol {
  const p = HANGING_PROTOCOLS.find((x) => x.id === id);
  if (!p) throw new Error(`unknown hanging protocol: ${id}`);
  return p;
}
