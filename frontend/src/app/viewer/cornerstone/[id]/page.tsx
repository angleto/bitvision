"use client";

// Spike route for the Cornerstone3D MPR migration. Mount any series
// at ``/viewer/cornerstone/<series-id>`` and the new
// ``CornerstoneMPRPanel`` takes over with the OHIF-style crosshair
// widget, oblique reformat, fusion overlay (when ``?fusion=<id>``
// is supplied) — features the hand-rolled ``MPRViewport`` doesn't
// have.
//
// This route intentionally bypasses the existing viewer chrome
// (sidebar, hanging-protocol picker, measurements panel) so we can
// validate Cornerstone in isolation. The migration plan is to fold
// it back into the main viewer page once feature-complete.

import dynamic from "next/dynamic";
import { useParams, useSearchParams } from "next/navigation";

import BrowserSupportGate from "@/components/BrowserSupportGate";

const CornerstoneMPRPanel = dynamic(() => import("@/components/CornerstoneMPRPanel"), {
  ssr: false,
});

export default function CornerstoneMPRTestPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const fusion = search.get("fusion");

  return (
    <main
      style={{
        margin: 0,
        padding: 0,
        height: "calc(100vh - 56px)",
        background: "#000",
      }}
    >
      <BrowserSupportGate>
        <CornerstoneMPRPanel primarySeriesId={params.id} fusionSeriesId={fusion || null} />
      </BrowserSupportGate>
    </main>
  );
}
