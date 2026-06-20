"use client";

// One-shot Cornerstone3D bootstrap. Cornerstone's ``init()`` is
// idempotent but has noticeable cost (registers a webworker pool,
// initialises the WASM codecs, configures GPU detection); calling
// it once at app start avoids repeating the work on every viewer
// mount.
//
// ``ensureInit`` is also where we register tools so the CrosshairsTool
// is available to any tool group the consumer creates afterwards.

import * as cs from "@cornerstonejs/core";
import * as csTools from "@cornerstonejs/tools";

let initPromise: Promise<void> | null = null;

export async function ensureCornerstoneInit(): Promise<void> {
  if (initPromise) return initPromise;
  initPromise = (async () => {
    if (!cs.isCornerstoneInitialized()) {
      await cs.init();
    }
    await csTools.init();
    // Register the tools we plan to use. Tool registration is
    // global; tool groups bind specific tools to specific viewports
    // at use time. Idempotent — addTool throws if the tool was
    // already registered, so we swallow the throw.
    for (const tool of [
      csTools.CrosshairsTool,
      csTools.WindowLevelTool,
      csTools.ZoomTool,
      csTools.PanTool,
      csTools.StackScrollTool,
      // Trackball rotate — used by the rotating-MIP viewport for
      // primary-mouse drag-to-orbit. Without registration, the MIP
      // tool group's ``setToolActive`` warns and the user can't
      // rotate the projection at all.
      csTools.TrackballRotateTool,
      // Measurement tools — bound by ``CornerstoneMPRLayout`` to
      // the active tool group. Drawing happens against the volume's
      // frame of reference, so a Length placed in axial appears
      // (correctly projected) in sagittal / coronal too.
      csTools.LengthTool,
      csTools.AngleTool,
      csTools.EllipticalROITool,
      csTools.RectangleROITool,
      // CircleROI is the 2D-circle annotation tool. The viewer
      // surfaces it as the "Sphere" measurement: a circle drawn on
      // a single MPR slice is interpreted by the backend as the
      // equatorial cross-section of a 3D sphere (PERCIST 1.0 §4.3
      // liver reference, 3 cm diameter).
      csTools.CircleROITool,
      csTools.ArrowAnnotateTool,
      csTools.ProbeTool,
      csTools.PlanarFreehandROITool,
      csTools.BidirectionalTool,
    ]) {
      try {
        csTools.addTool(tool);
      } catch {
        /* already registered */
      }
    }
  })();
  return initPromise;
}

/** Purge dell'intera cache volumetrica Cornerstone3D + IImageVolume cache.
 *  Da chiamare sul cleanup di route change del viewer (cambio seriesId)
 *  per garantire che la cache non venga MAI letta inter-paziente,
 *  inter-studio, inter-serie.
 *
 *  Le voci della cache sono già keyed per ``seriesId`` (i ``volumeId``
 *  hanno la forma ``bvp-vol-{primary,fusion}:<seriesId>``), quindi i
 *  lookup con un seriesId diverso non potrebbero comunque colpire
 *  l'entry di un altro paziente. Ma per requisito esplicito della
 *  policy di isolamento storage e per chiudere edge case di timing
 *  (back-button, hot-reload dev, redeploy mid-sessione, leak via
 *  vtkOpenGLTexture residue), purgiamo la cache esplicitamente.
 *
 *  ``purgeCache`` di CS3D fa: rilascia tutti i ``IImageVolume``
 *  registrati, libera le ``vtkOpenGLTexture`` GPU associate, vuota
 *  la cache image-loader. È sicuro chiamarla anche se la cache è
 *  già vuota. Idempotente. */
export function purgeCornerstoneCache(): void {
  try {
    cs.cache.purgeCache();
  } catch {
    /* swallow: stripped CS build without purgeCache is best-effort */
  }
}
