"use client";

import { type PointerEvent as ReactPointerEvent, useCallback, useRef } from "react";

/**
 * React hook providing touch/trackpad gestures for a canvas container:
 *   - Pinch-zoom (two fingers)
 *   - Two-finger pan
 *   - Double-tap → fit / reset
 *
 * Uses Pointer Events so it works with touch, pen, and trackpad (when the
 * browser synthesizes pointer events). Returns spread-able handlers to attach
 * to the container element.
 *
 * The hook is opt-in and does NOT interfere with mouse drag/zoom logic already
 * implemented in the viewport: it only activates once 2+ pointers are down, or
 * when a fast double-tap is detected.
 */

export interface TouchGestureCallbacks {
  /** Called with multiplicative zoom factor (e.g. 1.05 or 0.95) around an (x,y) point (container-relative). */
  onPinch?: (factor: number, center: { x: number; y: number }) => void;
  /** Called with incremental pan deltas in pixels. */
  onPan?: (dx: number, dy: number) => void;
  /** Called on double-tap / double-click — used to "fit to window" or reset. */
  onDoubleTap?: (center: { x: number; y: number }) => void;
  /** If false, the hook returns no-op handlers (useful for conditional mount). */
  enabled?: boolean;
}

export interface TouchGestureHandlers {
  onPointerDown: (e: ReactPointerEvent<HTMLElement>) => void;
  onPointerMove: (e: ReactPointerEvent<HTMLElement>) => void;
  onPointerUp: (e: ReactPointerEvent<HTMLElement>) => void;
  onPointerCancel: (e: ReactPointerEvent<HTMLElement>) => void;
}

interface PointerSnapshot {
  id: number;
  x: number;
  y: number;
}

const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_DIST = 30;

// Hoisted (pure function — no state/closure capture) so its identity is
// stable across renders; the useCallback hooks below can omit it from
// their dep arrays without a stale-closure risk.
function containerCoords(e: ReactPointerEvent<HTMLElement>) {
  const rect = e.currentTarget.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

export function useTouchGestures(cb: TouchGestureCallbacks): TouchGestureHandlers {
  const pointersRef = useRef<Map<number, PointerSnapshot>>(new Map());
  const lastDistRef = useRef<number | null>(null);
  const lastCenterRef = useRef<{ x: number; y: number } | null>(null);
  const lastTapRef = useRef<{ t: number; x: number; y: number } | null>(null);

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (!cb.enabled) return;
      // Only act on touch (and pen, which some stylus devices report).
      if (e.pointerType !== "touch" && e.pointerType !== "pen") return;
      const { x, y } = containerCoords(e);
      pointersRef.current.set(e.pointerId, { id: e.pointerId, x, y });

      // Reset pinch baselines when we reach 2 pointers.
      if (pointersRef.current.size === 2) {
        const pts = Array.from(pointersRef.current.values());
        lastDistRef.current = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        lastCenterRef.current = {
          x: (pts[0].x + pts[1].x) / 2,
          y: (pts[0].y + pts[1].y) / 2,
        };
      }

      // Double-tap detection (single-finger).
      if (pointersRef.current.size === 1) {
        const now = performance.now();
        const last = lastTapRef.current;
        if (
          last &&
          now - last.t < DOUBLE_TAP_MS &&
          Math.hypot(x - last.x, y - last.y) < DOUBLE_TAP_DIST
        ) {
          cb.onDoubleTap?.({ x, y });
          lastTapRef.current = null;
        } else {
          lastTapRef.current = { t: now, x, y };
        }
      }
    },
    [cb],
  );

  const onPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (!cb.enabled) return;
      if (e.pointerType !== "touch" && e.pointerType !== "pen") return;
      const existing = pointersRef.current.get(e.pointerId);
      if (!existing) return;
      const { x, y } = containerCoords(e);
      pointersRef.current.set(e.pointerId, { id: e.pointerId, x, y });

      if (pointersRef.current.size >= 2) {
        const pts = Array.from(pointersRef.current.values()).slice(0, 2);
        const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        const center = {
          x: (pts[0].x + pts[1].x) / 2,
          y: (pts[0].y + pts[1].y) / 2,
        };
        if (lastDistRef.current != null && dist > 0) {
          const factor = dist / lastDistRef.current;
          if (factor !== 1 && Number.isFinite(factor)) {
            cb.onPinch?.(factor, center);
          }
        }
        if (lastCenterRef.current) {
          const dx = center.x - lastCenterRef.current.x;
          const dy = center.y - lastCenterRef.current.y;
          if (dx !== 0 || dy !== 0) cb.onPan?.(dx, dy);
        }
        lastDistRef.current = dist;
        lastCenterRef.current = center;
      }
    },
    [cb],
  );

  const clearPointer = useCallback((id: number) => {
    pointersRef.current.delete(id);
    if (pointersRef.current.size < 2) {
      lastDistRef.current = null;
      lastCenterRef.current = null;
    }
  }, []);

  const onPointerUp = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (e.pointerType !== "touch" && e.pointerType !== "pen") return;
      clearPointer(e.pointerId);
    },
    [clearPointer],
  );

  const onPointerCancel = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (e.pointerType !== "touch" && e.pointerType !== "pen") return;
      clearPointer(e.pointerId);
    },
    [clearPointer],
  );

  return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel };
}
