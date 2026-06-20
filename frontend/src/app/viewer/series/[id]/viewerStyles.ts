// Shared by WLPresetBar.tsx. Lifted out of page.tsx so it stops
// being a circular dependency target.

// Slightly larger than the old 0.65rem/0.15rem so the dense W/L-preset grid
// is legible/clickable (≈30px tall on desktop) without ballooning; touch
// devices get the 44px min via the ``@media (pointer: coarse)`` rule on
// ``.viewer-btn`` in globals.css.
export const WL_BTN_STYLE = { fontSize: "0.72rem", padding: "0.3rem 0.52rem" };
