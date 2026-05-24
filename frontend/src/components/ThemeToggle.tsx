"use client";

import { useEffect, useState } from "react";

import {
  type ThemeMode,
  getStoredTheme,
  initTheme,
  setTheme,
  subscribeTheme,
} from "@/lib/themeToggle";

interface Props {
  className?: string;
}

const MODES: ThemeMode[] = ["light", "dark", "auto"];

const LABELS: Record<ThemeMode, { icon: string; label: string }> = {
  light: { icon: "☀", label: "Light" },
  dark: { icon: "☾", label: "Dark" },
  auto: { icon: "◐", label: "Auto" },
};

/**
 * Click cycles light → dark → auto.
 * Mounts `initTheme()` on first render so the stored mode survives reloads.
 */
export default function ThemeToggle({ className }: Props) {
  const [mode, setMode] = useState<ThemeMode>("dark");

  useEffect(() => {
    const cleanup = initTheme();
    setMode(getStoredTheme());
    const unsub = subscribeTheme((m) => setMode(m));
    return () => {
      cleanup();
      unsub();
    };
  }, []);

  const cycle = () => {
    const idx = MODES.indexOf(mode);
    setTheme(MODES[(idx + 1) % MODES.length]);
  };

  const { icon, label } = LABELS[mode];

  return (
    <button
      type="button"
      onClick={cycle}
      aria-label={`Theme: ${label}. Click to change.`}
      title={`Theme: ${label} (click to cycle)`}
      className={className ?? "viewer-chip-btn"}
    >
      <span aria-hidden>{icon}</span>
    </button>
  );
}
