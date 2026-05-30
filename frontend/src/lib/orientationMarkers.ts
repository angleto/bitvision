// Anatomical orientation markers for the viewer panes.
//
// Every diagnostic PACS paints L/R / A/P / S/I letters at the edges of
// each pane so the reader can never mistake laterality or orientation —
// a flipped or mis-oriented reformat that goes unnoticed is a sentinel
// (wrong-side) event. These pure helpers turn a Cornerstone viewport
// camera into the four edge letters and the transform-state flags.
//
// Coordinate system: after the volume keystone the Cornerstone world
// space IS the DICOM patient LPS frame, so the camera's ``viewUp`` /
// ``viewPlaneNormal`` are already anatomical vectors. LPS axes:
//   +X = patient Left,  -X = Right
//   +Y = patient Posterior, -Y = Anterior
//   +Z = patient Superior (head), -Z = Inferior (feet)
//
// IMPORTANT: only show these when the volume carries REAL geometry. On a
// legacy identity-frame pack the "world" is a fabricated frame and any
// letter would be an assumption — the caller must gate on that.

export type Vec3 = [number, number, number];

function normalize(v: Vec3): Vec3 {
  const n = Math.hypot(v[0], v[1], v[2]);
  if (n < 1e-9) return [0, 0, 0];
  return [v[0] / n, v[1] / n, v[2] / n];
}

function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function negate(v: Vec3): Vec3 {
  return [-v[0], -v[1], -v[2]];
}

/**
 * Map an LPS direction vector to its anatomical label, concatenating the
 * contributing axes from most to least dominant (so an oblique reformat
 * reads e.g. "AL" or "SPR"), mirroring the standard PACS / DICOM
 * ``getOrientationStringLPS`` behaviour.
 */
export function lpsDirectionToString(
  v: Vec3,
  opts: { threshold?: number; maxLetters?: number } = {},
): string {
  const threshold = opts.threshold ?? 0.2;
  const maxLetters = opts.maxLetters ?? 3;
  const labels: [string, string, string] = [
    v[0] < 0 ? "R" : "L",
    v[1] < 0 ? "A" : "P",
    v[2] < 0 ? "I" : "S",
  ];
  const abs: Vec3 = [Math.abs(v[0]), Math.abs(v[1]), Math.abs(v[2])];
  const order = [0, 1, 2].sort((a, b) => abs[b] - abs[a]);
  let out = "";
  let n = 0;
  for (const i of order) {
    if (abs[i] >= threshold && n < maxLetters) {
      out += labels[i];
      n += 1;
    }
  }
  // Always emit at least the dominant axis (degenerate / tiny vectors).
  return out || labels[order[0]];
}

export interface EdgeLetters {
  top: string;
  bottom: string;
  left: string;
  right: string;
}

export interface OrientationCamera {
  viewUp?: Vec3;
  viewPlaneNormal?: Vec3;
  flipHorizontal?: boolean;
  flipVertical?: boolean;
  rotation?: number;
}

/**
 * Compute the four edge letters for a pane from its camera.
 *
 * Screen mapping: ``viewUp`` is the on-screen up direction; screen-right
 * = ``viewUp × viewPlaneNormal`` (right-handed, normal points toward the
 * viewer). ``flipHorizontal`` / ``flipVertical`` mirror the respective
 * screen axis, so the letters track the pixels even after a flip.
 * Returns ``null`` when the camera lacks the vectors.
 */
export function cameraEdgeLetters(camera: OrientationCamera): EdgeLetters | null {
  if (!camera.viewUp || !camera.viewPlaneNormal) return null;
  let topDir = normalize(camera.viewUp);
  let rightDir = normalize(cross(camera.viewUp, camera.viewPlaneNormal));
  if (camera.flipVertical) topDir = negate(topDir);
  if (camera.flipHorizontal) rightDir = negate(rightDir);
  return {
    top: lpsDirectionToString(topDir),
    bottom: lpsDirectionToString(negate(topDir)),
    right: lpsDirectionToString(rightDir),
    left: lpsDirectionToString(negate(rightDir)),
  };
}

export interface TransformFlags {
  flipped: boolean;
  rotated: boolean;
}

/**
 * Whether a pane deviates from its default presentation. The reader must
 * know when what they see is flipped or rotated relative to the
 * acquisition, since that changes how the on-image letters relate to a
 * naive "left of screen = patient right" assumption.
 */
export function cameraTransformFlags(camera: OrientationCamera): TransformFlags {
  const rot = camera.rotation ?? 0;
  const norm = ((rot % 360) + 360) % 360;
  const rotated = Math.min(norm, 360 - norm) > 1; // > ~1° off the default
  return {
    flipped: !!camera.flipHorizontal || !!camera.flipVertical,
    rotated,
  };
}
