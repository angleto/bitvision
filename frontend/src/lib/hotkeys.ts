"use client";

// Central keyboard-shortcut dispatcher. Components declare their bindings
// via useHotkeys(); the hook attaches a single window-level keydown listener,
// skips events originating in text inputs / contentEditable nodes, and
// prevents browser defaults for consumed keys.

import { useEffect, useRef } from "react";

export type HotkeyScope = "viewer" | "global";

export interface HotkeyBinding {
  /** KeyboardEvent.key value, e.g. "ArrowUp", "+", "r", "?", " ". */
  key: string;
  handler: (event: KeyboardEvent) => void;
  /** Optional scope tag (purely descriptive; all bindings are active). */
  when?: HotkeyScope;
  /** Tri-state: undefined = don't care, boolean = exact match required. Ctrl also matches Cmd on macOS. */
  ctrl?: boolean;
  shift?: boolean;
  alt?: boolean;
  /** Short label for the help overlay. */
  description?: string;
  disabled?: boolean;
  /** Default behavior calls preventDefault + stopPropagation on match. */
  allowDefault?: boolean;
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (target.isContentEditable) return true;
  return false;
}

function isShiftProducedChar(key: string): boolean {
  // Printable chars that typically require Shift on US layouts. We treat
  // shift as "don't care" for these so users can hit them naturally.
  return key.length === 1 && /[!@#$%^&*()_+{}|:"<>?~]/.test(key);
}

function matches(binding: HotkeyBinding, event: KeyboardEvent): boolean {
  if (binding.disabled) return false;
  const hasCtrl = event.ctrlKey || event.metaKey;
  // Tri-state: undefined = "don't care", boolean = exact match required.
  if (binding.ctrl !== undefined && binding.ctrl !== hasCtrl) return false;
  if (binding.alt !== undefined && binding.alt !== event.altKey) return false;
  if (binding.shift !== undefined) {
    if (binding.shift !== event.shiftKey) return false;
  } else if (!isShiftProducedChar(binding.key)) {
    // For plain keys, require shift to be off unless the binding key is a
    // Shift-produced character (like "+" or "?"), which implicitly needs it.
    if (event.shiftKey) return false;
  }
  // Case-insensitive for single-letter keys, exact for named keys.
  if (binding.key.length === 1) {
    return binding.key.toLowerCase() === event.key.toLowerCase();
  }
  return binding.key === event.key;
}

/**
 * Register keyboard shortcuts on the window while the component is mounted.
 * Bindings are read via ref so handler identity changes between renders
 * do not require re-attaching the listener.
 */
export function useHotkeys(bindings: HotkeyBinding[]): void {
  const ref = useRef(bindings);
  ref.current = bindings;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTypingTarget(event.target)) return;
      for (const binding of ref.current) {
        if (matches(binding, event)) {
          if (!binding.allowDefault) {
            event.preventDefault();
            event.stopPropagation();
          }
          binding.handler(event);
          return;
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}

/** Format a binding as a human-readable shortcut string, e.g. "Ctrl+Shift+K". */
export function formatHotkey(binding: HotkeyBinding): string {
  const parts: string[] = [];
  if (binding.ctrl) parts.push("Ctrl");
  if (binding.alt) parts.push("Alt");
  if (binding.shift) parts.push("Shift");
  const key =
    binding.key === " "
      ? "Space"
      : binding.key === "ArrowUp"
        ? "↑"
        : binding.key === "ArrowDown"
          ? "↓"
          : binding.key === "ArrowLeft"
            ? "←"
            : binding.key === "ArrowRight"
              ? "→"
              : binding.key.length === 1
                ? binding.key.toUpperCase()
                : binding.key;
  parts.push(key);
  return parts.join("+");
}

// Custom-event channel used by the viewer page to broadcast zoom/reset-view
// commands to every MPRViewport without prop drilling.
export const VIEWPORT_ZOOM_EVENT = "bitvision:viewport-zoom";
export const VIEWPORT_RESET_VIEW_EVENT = "bitvision:viewport-reset-view";

export interface ViewportZoomDetail {
  /** Multiplicative factor. 1.1 zooms in, 0.9 zooms out. */
  factor: number;
}

export function dispatchViewportZoom(factor: number): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<ViewportZoomDetail>(VIEWPORT_ZOOM_EVENT, { detail: { factor } }),
  );
}

export function dispatchViewportResetView(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(VIEWPORT_RESET_VIEW_EVENT));
}
