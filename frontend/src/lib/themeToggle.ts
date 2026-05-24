"use client";

/**
 * Theme preference management.
 *
 * Modes:
 *   - "light"  → force light
 *   - "dark"   → force dark (current default look)
 *   - "auto"   → follow `prefers-color-scheme`
 *
 * Persists to localStorage under STORAGE_KEY. Applies the `.dark` class on
 * `<html>` so Tailwind's `dark:` variants + CSS variables both react.
 */

export type ThemeMode = "light" | "dark" | "auto";

export const STORAGE_KEY = "bv-theme";
// Default to light: it matches the chrome on the rest of the app and
// avoids the "viewer leaks dark class onto patients page" bug. Users
// who prefer dark cycle the toggle and the choice is persisted.
const DEFAULT_MODE: ThemeMode = "light";

const listeners = new Set<(m: ThemeMode) => void>();

export function getStoredTheme(): ThemeMode {
  if (typeof window === "undefined") return DEFAULT_MODE;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "auto") return v;
  } catch {
    // localStorage may be unavailable (private mode, SSR)
  }
  return DEFAULT_MODE;
}

export function resolveTheme(mode: ThemeMode): "light" | "dark" {
  if (mode === "auto") {
    if (typeof window === "undefined" || !window.matchMedia) return "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return mode;
}

export function applyTheme(mode: ThemeMode): void {
  if (typeof document === "undefined") return;
  const resolved = resolveTheme(mode);
  const root = document.documentElement;
  if (resolved === "dark") {
    root.classList.add("dark");
    root.dataset.theme = "dark";
  } else {
    root.classList.remove("dark");
    root.dataset.theme = "light";
  }
}

export function setTheme(mode: ThemeMode): void {
  try {
    if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // ignore
  }
  applyTheme(mode);
  for (const cb of listeners) cb(mode);
}

export function subscribeTheme(cb: (m: ThemeMode) => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/**
 * Call once on client boot to apply the stored/auto theme and wire up
 * `prefers-color-scheme` changes when mode is "auto".
 */
export function initTheme(): () => void {
  if (typeof window === "undefined") return () => undefined;
  const stored = getStoredTheme();
  applyTheme(stored);

  const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
  const onMqChange = () => {
    if (getStoredTheme() === "auto") applyTheme("auto");
  };
  mq?.addEventListener?.("change", onMqChange);
  return () => mq?.removeEventListener?.("change", onMqChange);
}
