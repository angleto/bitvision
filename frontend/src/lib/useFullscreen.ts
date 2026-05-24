"use client";

import { type RefObject, useCallback, useEffect, useState } from "react";

/**
 * React hook wrapping the Fullscreen API.
 *
 * Tracks fullscreen state for the given element (or document if no ref given).
 * Exposes `enter`, `exit`, `toggle` helpers and a boolean `isFullscreen`.
 *
 * Safe on SSR: only touches `document`/`window` inside effects.
 */
export interface UseFullscreenResult {
  isFullscreen: boolean;
  isSupported: boolean;
  enter: () => Promise<void>;
  exit: () => Promise<void>;
  toggle: () => Promise<void>;
}

// Vendor-prefixed helpers for older Safari/iOS.
type FsDocument = Document & {
  webkitFullscreenElement?: Element | null;
  webkitExitFullscreen?: () => Promise<void>;
  mozFullScreenElement?: Element | null;
  mozCancelFullScreen?: () => Promise<void>;
  msFullscreenElement?: Element | null;
  msExitFullscreen?: () => Promise<void>;
};

type FsElement = HTMLElement & {
  webkitRequestFullscreen?: () => Promise<void>;
  mozRequestFullScreen?: () => Promise<void>;
  msRequestFullscreen?: () => Promise<void>;
};

function getFullscreenElement(): Element | null {
  if (typeof document === "undefined") return null;
  const d = document as FsDocument;
  return (
    document.fullscreenElement ??
    d.webkitFullscreenElement ??
    d.mozFullScreenElement ??
    d.msFullscreenElement ??
    null
  );
}

async function requestFs(el: HTMLElement): Promise<void> {
  const e = el as FsElement;
  if (e.requestFullscreen) return e.requestFullscreen();
  if (e.webkitRequestFullscreen) return e.webkitRequestFullscreen();
  if (e.mozRequestFullScreen) return e.mozRequestFullScreen();
  if (e.msRequestFullscreen) return e.msRequestFullscreen();
  throw new Error("Fullscreen API not supported");
}

async function exitFs(): Promise<void> {
  const d = document as FsDocument;
  if (document.exitFullscreen) return document.exitFullscreen();
  if (d.webkitExitFullscreen) return d.webkitExitFullscreen();
  if (d.mozCancelFullScreen) return d.mozCancelFullScreen();
  if (d.msExitFullscreen) return d.msExitFullscreen();
}

export function useFullscreen(target?: RefObject<HTMLElement | null>): UseFullscreenResult {
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [isSupported, setIsSupported] = useState(false);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const d = document as FsDocument;
    setIsSupported(
      !!(
        document.documentElement.requestFullscreen ||
        (document.documentElement as FsElement).webkitRequestFullscreen ||
        (document.documentElement as FsElement).mozRequestFullScreen ||
        (document.documentElement as FsElement).msRequestFullscreen
      ),
    );

    const onChange = () => setIsFullscreen(getFullscreenElement() != null);
    onChange();
    const events = [
      "fullscreenchange",
      "webkitfullscreenchange",
      "mozfullscreenchange",
      "MSFullscreenChange",
    ];
    for (const ev of events) document.addEventListener(ev, onChange);
    return () => {
      for (const ev of events) document.removeEventListener(ev, onChange);
    };
  }, []);

  const enter = useCallback(async () => {
    const el = target?.current ?? document.documentElement;
    if (!el) return;
    try {
      await requestFs(el as HTMLElement);
    } catch {
      // swallow — user may have denied or API unsupported
    }
  }, [target]);

  const exit = useCallback(async () => {
    if (getFullscreenElement() == null) return;
    try {
      await exitFs();
    } catch {
      // swallow
    }
  }, []);

  const toggle = useCallback(async () => {
    if (getFullscreenElement() == null) {
      await enter();
    } else {
      await exit();
    }
  }, [enter, exit]);

  return { isFullscreen, isSupported, enter, exit, toggle };
}
