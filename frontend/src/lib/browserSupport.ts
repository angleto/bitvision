"use client";

/**
 * Capability probe for the volume viewer. Cornerstone3D needs:
 *
 *   - WebGL2 (volumetric rendering, multi-volume actors, MIP)
 *   - WebAssembly (codec workers for compressed DICOM)
 *
 * If either is missing the viewer can't mount at all, so we surface a
 * "browser not supported" message instead of a black canvas + console
 * spam.
 *
 * The probe is best-effort — `getContext("webgl2")` allocates a tiny
 * canvas which we discard immediately. We never cache a positive
 * result across reloads (the user might switch browser between visits)
 * but we cache it within a single page load to avoid repeating the
 * canvas allocation on every re-render of a gated component.
 */

let cached: boolean | null = null;

export function isViewerSupported(): boolean {
  if (typeof window === "undefined") return true;
  if (cached !== null) return cached;

  // WebAssembly is a global on every modern engine; the typeof check
  // is the canonical feature detect.
  const hasWasm = typeof WebAssembly === "object" && typeof WebAssembly.instantiate === "function";

  let hasWebGL2 = false;
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2");
    hasWebGL2 = !!gl;
    // Drop the GL context promptly so we don't leak a tiny resource
    // just to answer the question.
    if (gl) {
      const lose = (gl as WebGL2RenderingContext).getExtension("WEBGL_lose_context");
      lose?.loseContext();
    }
  } catch {
    hasWebGL2 = false;
  }

  cached = hasWasm && hasWebGL2;
  return cached;
}

/** Reset the cached probe — used by tests / dev to retry after the user
 *  changes their browser settings without reloading. */
export function _resetViewerSupportCache(): void {
  cached = null;
}
