"use client";

// Read-only SVG overlay that draws non-cornerstone markers
// (bbox.lesion, fiducial, text-overlay) on top of a Cornerstone
// axial viewport. The cornerstone-managed annotations (Length,
// EllipticalROI, RectangleROI, …) are drawn by Cornerstone itself
// and do NOT come through this overlay — see the ``csToolName`` gate
// in ``MarkerListPanel.markerRow``.
//
// What this fixes
// ---------------
//
// Pre-v3.5.1, a marker stored with ``kind="bbox.lesion"`` (written
// by an MCP agent via ``find_hot_spots`` → ``write_annotation``) had
// no visual on the canvas: the only feedback the user got from the
// marker list panel was the crosshair shifting to the centroid, and
// no rectangle was ever drawn. v3.5.1 closes that gap by projecting
// the voxel geometry through the viewport camera (voxel → world →
// canvas) and rendering an SVG outline pinned to the canvas at the
// correct screen coords.
//
// Projection
// ----------
//
// 1. Voxel ``[i, j, k]`` is in the SOURCE series' index space (the
//    marker's ``target_id`` series, which may be the PET fusion when
//    the user is viewing CT primary). The overlay needs the matching
//    vtk ``imageData`` to call ``indexToWorld``.
// 2. World coords ``[wx, wy, wz]`` are universal across co-registered
//    volumes (CT + PET share the FrameOfReferenceUID forced by
//    CornerstoneMPRLayout).
// 3. ``viewport.worldToCanvas`` projects world → canvas pixel coords
//    that match the ``<canvas>`` element drawn by Cornerstone — and,
//    since the canvas fills the viewport ``<div>`` via ``inset: 0``,
//    they're also valid SVG coords against an absolute-positioned
//    overlay placed on the same div.
//
// Visibility
// ----------
//
// On the axial pane we hide a bbox when the current slice is
// completely outside the [min_z, max_z] interval (pure waste of
// pixels). When the crosshair is inside the bbox z-range we draw at
// full opacity; otherwise we'd draw nothing (deferring sag/cor to
// future work).
//
// Focus highlight
// ---------------
//
// ``focusedMarkerId`` flips the stroke to a thicker pulse animation
// keyed by ``.bv-marker-focused`` in globals.css. Auto-clear is the
// caller's responsibility (page-level ``setFocusedMarker(null)`` on
// a timer).

import * as cs from "@cornerstonejs/core";
import { useEffect, useState } from "react";

import type { Marker } from "@/lib/api";
import { overlayStroke } from "@/lib/findingColors";

export interface MarkerOverlayItem {
  id: string;
  kind: string;
  /** FindingType.category of the finding this marker is linked to (via
   *  Finding.geometry.marker_id), when any. Drives the stroke colour so it
   *  reflects the CLASS; ``null`` falls back to the geometry-kind colour. */
  category?: string | null;
  // Source series id — the imageData lookup picks ``primaryImageData``
  // when this matches ``primarySeriesId``, otherwise ``fusionImageData``.
  targetSeriesId: string;
  geometry: {
    min_ijk?: [number, number, number];
    max_ijk?: [number, number, number];
    anchor?: [number, number, number];
    point?: [number, number, number];
    points?: number[][];
  };
  label?: string | null;
  // ``true`` when this marker is drawn by Cornerstone itself
  // (Length, RectangleROI, …); the overlay skips those to avoid
  // double-rendering. The page already filters before passing
  // markers in, but the prop is kept as a safety belt.
  managedByCornerstone?: boolean;
}

// vtkImageData has the minimal surface we touch — kept here as a
// structural type so we don't drag the full vtk.js typings into the
// overlay's bundle.
interface VtkImageDataLike {
  indexToWorld: (ijk: number[], outWorld?: number[]) => number[];
}

interface Props {
  /** The axis-pane wrapper div the Cornerstone canvas is mounted in.
   *  Used both as the camera-modified event source (Cornerstone
   *  dispatches CAMERA_MODIFIED on the element first, then bubbles
   *  to ``cs.eventTarget``) and as the visual anchor — the SVG sits
   *  ``position: absolute, inset: 0`` over the same element. */
  containerRef: React.RefObject<HTMLDivElement | null>;
  /** Cornerstone viewport id (e.g. ``axial-…``). */
  viewportId: string;
  /** Cornerstone rendering engine — supplied by the parent so the
   *  overlay does not depend on the engine-id lookup. */
  engine: cs.RenderingEngine | null;
  primarySeriesId: string;
  primaryImageData: VtkImageDataLike | null;
  fusionSeriesId?: string | null;
  fusionImageData?: VtkImageDataLike | null;
  markers: MarkerOverlayItem[];
  /** Axis the overlay is rendered on. Today we only project on
   *  ``axial`` (the marker panel jumps the axial crosshair); sag/cor
   *  remain empty overlays kept ready for a future round. */
  axis: "axial" | "sagittal" | "coronal";
  focusedMarkerId: string | null;
  onMarkerClick?: (id: string) => void;
}

/** Map a Cornerstone-rendered marker to the overlay skip-list. */
export function markerIsManagedByCornerstone(m: Marker): boolean {
  // The reverse map of the csToLegacy mapping in
  // CornerstoneMPRLayout: any kind that legacy-persists from a
  // Cornerstone tool is drawn by Cornerstone, and the overlay must
  // NOT render it (avoids a double outline).
  const cornerstoneKinds = new Set([
    "measurement.distance",
    "measurement.bbox",
    "measurement.ellipse",
    "measurement.angle",
    "measurement.text",
    "sphere",
    "freehand",
    "probe",
  ]);
  return cornerstoneKinds.has(m.kind);
}

export function markerToOverlayItem(m: Marker): MarkerOverlayItem {
  const g = (m.geometry ?? {}) as MarkerOverlayItem["geometry"];
  return {
    id: m.id,
    kind: m.kind,
    targetSeriesId: m.target_kind === "series" ? m.target_id : "",
    geometry: g,
    label: m.body ?? null,
    managedByCornerstone: markerIsManagedByCornerstone(m),
  };
}

function pickImageData(
  item: MarkerOverlayItem,
  primarySeriesId: string,
  primaryImageData: VtkImageDataLike | null,
  fusionSeriesId: string | null | undefined,
  fusionImageData: VtkImageDataLike | null | undefined,
): VtkImageDataLike | null {
  if (item.targetSeriesId && fusionSeriesId && item.targetSeriesId === fusionSeriesId) {
    return fusionImageData ?? null;
  }
  // Default to primary even when targetSeriesId is empty (study-scoped
  // marker) — better to land in the wrong volume than to skip rendering.
  if (!item.targetSeriesId || item.targetSeriesId === primarySeriesId) {
    return primaryImageData;
  }
  return null;
}

interface ProjectedBox {
  id: string;
  kind: string;
  category: string | null;
  label: string | null;
  focused: boolean;
  // Canvas-coord rectangle (already screen-space). null for shapes
  // that aren't bbox (e.g. fiducial → ``point`` below).
  rect: { x: number; y: number; w: number; h: number } | null;
  point: { x: number; y: number } | null;
}

function projectBboxOnAxial(
  imageData: VtkImageDataLike,
  viewport: cs.Types.IVolumeViewport,
  min_ijk: [number, number, number],
  max_ijk: [number, number, number],
): { x: number; y: number; w: number; h: number } | null {
  // Project the 4 axial-plane corners and take the bounding box of
  // their canvas projections. Cornerstone's ``worldToCanvas`` returns
  // null/undefined when the world point is behind the camera, so we
  // bail if any corner fails.
  // The voxel index passed to indexToWorld must be a fresh array
  // (vtk mutates it in some versions).
  const z = Math.round((min_ijk[2] + max_ijk[2]) / 2);
  const corners: number[][] = [
    [min_ijk[0], min_ijk[1], z],
    [max_ijk[0], min_ijk[1], z],
    [max_ijk[0], max_ijk[1], z],
    [min_ijk[0], max_ijk[1], z],
  ];
  const xs: number[] = [];
  const ys: number[] = [];
  for (const ijk of corners) {
    const world = imageData.indexToWorld([...ijk]);
    const canvas = viewport.worldToCanvas(world as cs.Types.Point3);
    if (!canvas) return null;
    xs.push(canvas[0]);
    ys.push(canvas[1]);
  }
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const maxX = Math.max(...xs);
  const maxY = Math.max(...ys);
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function projectPointOnAxial(
  imageData: VtkImageDataLike,
  viewport: cs.Types.IVolumeViewport,
  ijk: [number, number, number],
): { x: number; y: number } | null {
  const world = imageData.indexToWorld([...ijk]);
  const canvas = viewport.worldToCanvas(world as cs.Types.Point3);
  if (!canvas) return null;
  return { x: canvas[0], y: canvas[1] };
}

function currentAxialSliceIndex(
  imageData: VtkImageDataLike | null,
  viewport: cs.Types.IVolumeViewport,
): number | null {
  if (!imageData) return null;
  const cam = viewport.getCamera();
  if (!cam.focalPoint) return null;
  // Use a synthetic API: indexToWorld inverse is not exposed, but we
  // can map focalPoint to slice by snapping its z to the closest
  // imageData index. Cornerstone v3 has imageData.worldToIndex but
  // vtk.js's worldToIndex requires the matrix transform; we trust
  // the camera's focalPoint Z as the slice z-coordinate and let the
  // bbox-z range filter on world coords directly.
  return cam.focalPoint[2] as number;
}

export default function MarkerOverlay({
  containerRef,
  viewportId,
  engine,
  primarySeriesId,
  primaryImageData,
  fusionSeriesId,
  fusionImageData,
  markers,
  axis,
  focusedMarkerId,
  onMarkerClick,
}: Props) {
  // Bump on every CAMERA_MODIFIED so the SVG re-renders at the new
  // canvas coords. Cornerstone dispatches the event on the element
  // first; subscribing there is cheaper than the global
  // ``cs.eventTarget`` because we only re-render this overlay.
  const [, setTick] = useState(0);
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const handler = () => setTick((x) => x + 1);
    el.addEventListener(cs.Enums.Events.CAMERA_MODIFIED, handler as EventListener);
    return () => {
      el.removeEventListener(cs.Enums.Events.CAMERA_MODIFIED, handler as EventListener);
    };
  }, [containerRef]);

  // Today we only project on axial; sag/cor mount the overlay but
  // skip rendering. The shape gives us room to extend later.
  if (axis !== "axial") return null;
  if (!engine || markers.length === 0) return null;

  const viewport = engine.getViewport(viewportId) as cs.Types.IVolumeViewport | undefined;
  if (!viewport) return null;

  const focalZ = currentAxialSliceIndex(primaryImageData, viewport);
  const projected: ProjectedBox[] = [];
  for (const m of markers) {
    if (m.managedByCornerstone) continue;
    const imgData = pickImageData(
      m,
      primarySeriesId,
      primaryImageData,
      fusionSeriesId,
      fusionImageData,
    );
    if (!imgData) continue;
    const focused = focusedMarkerId === m.id;

    if (m.geometry.min_ijk && m.geometry.max_ijk) {
      const min_ijk = m.geometry.min_ijk as [number, number, number];
      const max_ijk = m.geometry.max_ijk as [number, number, number];
      // Visibility filter: if the slice's world z is far outside
      // the bbox's z range we skip. We compute the bbox's world z
      // range via the same indexToWorld so we don't conflate voxel
      // and world units.
      const wMin = imgData.indexToWorld([...min_ijk]);
      const wMax = imgData.indexToWorld([...max_ijk]);
      if (focalZ !== null) {
        const zLo = Math.min(wMin[2], wMax[2]);
        const zHi = Math.max(wMin[2], wMax[2]);
        const slack = 5; // mm — a hair of tolerance so a centred crosshair still shows the bbox
        if (focalZ < zLo - slack || focalZ > zHi + slack) {
          continue;
        }
      }
      const rect = projectBboxOnAxial(imgData, viewport, min_ijk, max_ijk);
      if (!rect) continue;
      projected.push({
        id: m.id,
        kind: m.kind,
        category: m.category ?? null,
        label: m.label ?? null,
        focused,
        rect,
        point: null,
      });
      continue;
    }
    // Single-point markers: fiducial / text-overlay / probe-like.
    const ijk =
      (m.geometry.anchor as [number, number, number] | undefined) ??
      (m.geometry.point as [number, number, number] | undefined) ??
      (m.geometry.points && m.geometry.points.length > 0
        ? (m.geometry.points[0] as [number, number, number])
        : undefined);
    if (!ijk) continue;
    const point = projectPointOnAxial(imgData, viewport, ijk);
    if (!point) continue;
    projected.push({
      id: m.id,
      kind: m.kind,
      category: m.category ?? null,
      label: m.label ?? null,
      focused,
      rect: null,
      point,
    });
  }

  if (projected.length === 0) return null;

  return (
    <svg
      role="img"
      aria-label="Marker overlay"
      style={{
        position: "absolute",
        inset: 0,
        width: "100%",
        height: "100%",
        // Pass mouse events through to the underlying Cornerstone
        // canvas EXCEPT on the marker shapes themselves (which opt
        // back in via ``pointer-events: auto``). This way zoom/pan
        // gestures keep working through the overlay.
        pointerEvents: "none",
        // Above the Cornerstone canvas (z-index 0) and the corner
        // overlays (z-index 2 for the rotate buttons).
        zIndex: 3,
      }}
    >
      <title>Marker overlay</title>
      {projected.map((p) => {
        // Class colour when the marker is linked to a finding; else the
        // legacy geometry-kind colour (bbox.lesion / fiducial / text-overlay).
        const stroke = overlayStroke({ category: p.category, kind: p.kind });
        const baseStrokeWidth = p.focused ? 3 : 1.5;
        const focusedClass = p.focused ? "bv-marker-focused" : undefined;
        if (p.rect) {
          const { x, y, w, h } = p.rect;
          return (
            <g
              key={p.id}
              className={focusedClass}
              style={{ pointerEvents: "auto", cursor: "pointer" }}
              // biome-ignore lint/a11y/useSemanticElements: SVG <g> cannot be a real <button> without losing the geometry-anchored hit area; the click is a "nice-to-have" for highlighting markers on the canvas — the canonical interaction lives in the marker list panel, which is fully keyboard-accessible.
              role="button"
              tabIndex={0}
              onClick={() => onMarkerClick?.(p.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onMarkerClick?.(p.id);
                }
              }}
            >
              <rect
                x={x}
                y={y}
                width={Math.max(w, 1)}
                height={Math.max(h, 1)}
                fill="none"
                stroke={stroke}
                strokeWidth={baseStrokeWidth}
                strokeDasharray={p.focused ? "none" : "4 3"}
                vectorEffect="non-scaling-stroke"
              />
              {p.label && (
                <text
                  x={x}
                  y={Math.max(y - 4, 10)}
                  fill={stroke}
                  fontSize={11}
                  fontFamily="ui-monospace, monospace"
                  style={{ textShadow: "0 0 4px rgba(0,0,0,0.85)" }}
                >
                  {p.label.length > 36 ? `${p.label.slice(0, 36)}…` : p.label}
                </text>
              )}
            </g>
          );
        }
        if (p.point) {
          const r = p.focused ? 9 : 6;
          return (
            <g
              key={p.id}
              className={focusedClass}
              style={{ pointerEvents: "auto", cursor: "pointer" }}
              // biome-ignore lint/a11y/useSemanticElements: SVG <g> cannot be a real <button> without losing the geometry-anchored hit area; the click is a "nice-to-have" for highlighting markers on the canvas — the canonical interaction lives in the marker list panel, which is fully keyboard-accessible.
              role="button"
              tabIndex={0}
              onClick={() => onMarkerClick?.(p.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onMarkerClick?.(p.id);
                }
              }}
            >
              <circle
                cx={p.point.x}
                cy={p.point.y}
                r={r}
                fill="none"
                stroke={stroke}
                strokeWidth={baseStrokeWidth}
                vectorEffect="non-scaling-stroke"
              />
              <circle cx={p.point.x} cy={p.point.y} r={1.5} fill={stroke} />
              {p.label && (
                <text
                  x={p.point.x + r + 4}
                  y={p.point.y + 4}
                  fill={stroke}
                  fontSize={11}
                  fontFamily="ui-monospace, monospace"
                  style={{ textShadow: "0 0 4px rgba(0,0,0,0.85)" }}
                >
                  {p.label.length > 36 ? `${p.label.slice(0, 36)}…` : p.label}
                </text>
              )}
            </g>
          );
        }
        return null;
      })}
    </svg>
  );
}
