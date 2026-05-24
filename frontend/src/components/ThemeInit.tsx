"use client";

import { useEffect } from "react";

import { initTheme } from "@/lib/themeToggle";

/**
 * Mounts ``initTheme()`` once on the client at app boot and tears down its
 * matchMedia listener on unmount. Used in the root layout so every route
 * applies the user's stored theme — without this, the ``.dark`` class on
 * ``<html>`` only got managed by ``ThemeToggle``, which is mounted only
 * inside the DICOM viewer. Visiting the viewer added the class and
 * navigating back to another page (e.g. ``/patients/[id]``) left the
 * page stuck in dark chrome until a hard reload reset the DOM.
 */
export default function ThemeInit() {
  useEffect(() => {
    const cleanup = initTheme();
    return cleanup;
  }, []);
  return null;
}
