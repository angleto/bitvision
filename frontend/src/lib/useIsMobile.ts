"use client";

import { useEffect, useState } from "react";

/**
 * SSR-safe `matchMedia` hook returning whether the viewport currently
 * matches a max-width breakpoint. Defaults to 768px which is the
 * tablet/phone cutoff used across the app (mirrors the existing
 * `@media (max-width: 1000px)` documents grid breakpoint, but tighter
 * because the viewer in particular needs a phone-grade UI under 768px
 * and a desktop-grade UI above).
 *
 * The first render returns `false` so the SSR markup matches the
 * desktop layout; the effect upgrades to the real value on the first
 * client tick. Callers that want to gate Cornerstone mounting on the
 * answer should also tolerate a `false → true` transition without
 * leaking GPU resources (the viewer components do — they unmount their
 * rendering engines in their cleanup effects).
 */
export function useIsMobile(maxWidthPx = 768): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(`(max-width: ${maxWidthPx}px)`);
    const update = () => setIsMobile(mq.matches);
    update();
    // Safari < 14 only supports addListener / removeListener.
    if (mq.addEventListener) {
      mq.addEventListener("change", update);
      return () => mq.removeEventListener("change", update);
    }
    mq.addListener(update);
    return () => mq.removeListener(update);
  }, [maxWidthPx]);

  return isMobile;
}
