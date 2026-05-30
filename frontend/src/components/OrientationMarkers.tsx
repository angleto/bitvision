"use client";

// Anatomical edge letters (L/R, A/P, S/I) + transform-state badge for a
// single viewer pane. Every diagnostic PACS paints these so the reader
// can never mistake laterality or orientation; the badge warns when the
// pane is flipped / rotated / inverted relative to the acquisition, which
// changes how the letters relate to a naive screen-side assumption.
//
// The letters come from ``cameraEdgeLetters`` (pure, recomputed on every
// CAMERA_MODIFIED). Render only when the volume carries REAL geometry —
// on a legacy identity-frame pack a letter would be an assumption, and a
// wrong-but-confident letter is worse than none.

import type { EdgeLetters, TransformFlags } from "@/lib/orientationMarkers";

const LETTER_BASE: React.CSSProperties = {
  position: "absolute",
  color: "#fde047", // amber-300: high contrast on black, distinct from the cyan/red/green axis chrome
  fontFamily: "ui-monospace, monospace",
  fontSize: "0.72rem",
  fontWeight: 700,
  pointerEvents: "none",
  textShadow: "0 1px 2px rgba(0,0,0,0.9)",
  letterSpacing: "0.05em",
  zIndex: 2,
};

export interface OrientationMarkersProps {
  letters: EdgeLetters | null;
  flags: TransformFlags;
  inverted?: boolean;
}

export default function OrientationMarkers({ letters, flags, inverted }: OrientationMarkersProps) {
  if (!letters) return null;
  const badges = [
    flags.flipped ? "FLIP" : null,
    flags.rotated ? "ROT" : null,
    inverted ? "INV" : null,
  ].filter(Boolean);
  return (
    <>
      <span style={{ ...LETTER_BASE, top: 2, left: "50%", transform: "translateX(-50%)" }}>
        {letters.top}
      </span>
      <span style={{ ...LETTER_BASE, bottom: 2, left: "50%", transform: "translateX(-50%)" }}>
        {letters.bottom}
      </span>
      <span style={{ ...LETTER_BASE, left: 3, top: "50%", transform: "translateY(-50%)" }}>
        {letters.left}
      </span>
      <span style={{ ...LETTER_BASE, right: 3, top: "50%", transform: "translateY(-50%)" }}>
        {letters.right}
      </span>
      {badges.length > 0 && (
        <span
          style={{
            position: "absolute",
            bottom: 20,
            left: "50%",
            transform: "translateX(-50%)",
            color: "#fca5a5", // red-300: warns this pane deviates from the acquisition
            fontFamily: "ui-monospace, monospace",
            fontSize: "0.6rem",
            fontWeight: 700,
            pointerEvents: "none",
            textShadow: "0 1px 2px rgba(0,0,0,0.9)",
            letterSpacing: "0.08em",
            zIndex: 2,
          }}
        >
          {badges.join(" · ")}
        </span>
      )}
    </>
  );
}
