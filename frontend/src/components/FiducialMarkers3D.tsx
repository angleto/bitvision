"use client";

/**
 * 3D fiducial landmark management.
 *
 * Two concerns live here:
 *   1. State: a list of labelled points in voxel coordinates that lives
 *      in client-side viewport state (no persistence yet — ties into
 *      Unit V2's viewport-state model when that lands). Exposed via the
 *      ``useFiducials`` hook below.
 *   2. UI: a sidebar panel that lets the user add the current crosshair
 *      as a fiducial, rename labels, remove points, and export the set
 *      as JSON.
 *
 * Rendering of the markers themselves as 3D glyphs inside the VolumeViewer
 * stays vtk-native: ``FiducialGlyphs`` below owns a ``vtkSphereSource`` +
 * ``vtkGlyph3DMapper`` pair and mounts them on the same renderer the
 * volume uses. The viewer doesn't need to know about fiducials — it
 * just exposes ``grw`` through its ref, and we mount / unmount
 * separately.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export interface Fiducial {
  id: string;
  label: string;
  position: [number, number, number]; // voxel coordinates, ints
  color: [number, number, number];
}

const FIDUCIAL_COLORS: [number, number, number][] = [
  [1, 0.2, 0.2],
  [0.2, 1, 0.4],
  [0.3, 0.5, 1],
  [1, 0.8, 0.2],
  [0.9, 0.3, 0.9],
  [0.2, 0.9, 0.9],
];

function newId(): string {
  // Sufficient for client-side state; collisions are not a correctness
  // issue since fiducials aren't persisted across sessions.
  return Math.random().toString(36).slice(2, 10);
}

export function useFiducials() {
  const [fiducials, setFiducials] = useState<Fiducial[]>([]);

  const add = useCallback((position: [number, number, number], label?: string) => {
    setFiducials((prev) => {
      const idx = prev.length;
      const color = FIDUCIAL_COLORS[idx % FIDUCIAL_COLORS.length];
      const finalLabel = label?.trim() || `F${idx + 1}`;
      return [...prev, { id: newId(), label: finalLabel, position, color }];
    });
  }, []);

  const remove = useCallback((id: string) => {
    setFiducials((prev) => prev.filter((f) => f.id !== id));
  }, []);

  const rename = useCallback((id: string, label: string) => {
    setFiducials((prev) => prev.map((f) => (f.id === id ? { ...f, label } : f)));
  }, []);

  const clear = useCallback(() => setFiducials([]), []);

  return { fiducials, add, remove, rename, clear };
}

interface PanelProps {
  fiducials: Fiducial[];
  crosshair: [number, number, number];
  spacing: [number, number, number];
  onAdd: (position: [number, number, number], label?: string) => void;
  onRemove: (id: string) => void;
  onRename: (id: string, label: string) => void;
  onClear: () => void;
  onJumpTo?: (position: [number, number, number]) => void;
}

/**
 * Sidebar UI for managing fiducials. Kept separate from the renderer
 * so either can be used in isolation during tests.
 */
export default function FiducialPanel({
  fiducials,
  crosshair,
  spacing,
  onAdd,
  onRemove,
  onRename,
  onClear,
  onJumpTo,
}: PanelProps) {
  const [nextLabel, setNextLabel] = useState("");

  const addAtCrosshair = useCallback(() => {
    onAdd(crosshair, nextLabel);
    setNextLabel("");
  }, [crosshair, nextLabel, onAdd]);

  const exportJson = useCallback(() => {
    const payload = fiducials.map((f) => ({
      label: f.label,
      voxel: f.position,
      mm: [f.position[0] * spacing[0], f.position[1] * spacing[1], f.position[2] * spacing[2]],
    }));
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "fiducials.json";
    a.click();
    URL.revokeObjectURL(url);
  }, [fiducials, spacing]);

  return (
    <>
      <h2>Fiducials ({fiducials.length})</h2>
      <div className="card" style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
        <div className="meta" style={{ fontSize: "0.7rem" }}>
          Crosshair: ({crosshair[0]}, {crosshair[1]}, {crosshair[2]})
        </div>
        <div style={{ display: "flex", gap: "0.3rem" }}>
          <input
            type="text"
            value={nextLabel}
            onChange={(e) => setNextLabel(e.target.value)}
            placeholder="label (optional)"
            style={{
              flex: 1,
              background: "#111",
              border: "1px solid #444",
              color: "#ddd",
              padding: "0.2rem 0.4rem",
              fontSize: "0.7rem",
            }}
          />
          <button type="button" className="viewer-btn" onClick={addAtCrosshair}>
            + Add here
          </button>
        </div>
        {fiducials.length > 0 && (
          <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
            <button type="button" className="viewer-btn" onClick={exportJson}>
              Export JSON
            </button>
            <button
              type="button"
              className="viewer-btn"
              onClick={onClear}
              style={{ color: "#f66" }}
            >
              Clear all
            </button>
          </div>
        )}
      </div>
      {fiducials.length > 0 && (
        <div
          style={{ display: "flex", flexDirection: "column", gap: "0.15rem", marginTop: "0.3rem" }}
        >
          {fiducials.map((f) => (
            <div
              key={f.id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
                padding: "0.2rem 0.35rem",
                border: "1px solid #2a2f3b",
                borderRadius: 3,
              }}
            >
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: `rgb(${f.color.map((c) => Math.round(c * 255)).join(",")})`,
                }}
              />
              <input
                type="text"
                value={f.label}
                onChange={(e) => onRename(f.id, e.target.value)}
                style={{
                  flex: 1,
                  background: "transparent",
                  border: "none",
                  color: "#ddd",
                  fontSize: "0.75rem",
                  padding: 0,
                }}
              />
              <span className="meta" style={{ fontSize: "0.6rem", fontFamily: "monospace" }}>
                {f.position.join(",")}
              </span>
              {onJumpTo && (
                <button
                  type="button"
                  onClick={() => onJumpTo(f.position)}
                  style={{
                    background: "none",
                    border: "none",
                    color: "#9cb9e6",
                    cursor: "pointer",
                    fontSize: "0.7rem",
                  }}
                  title="Jump crosshair to this point"
                >
                  ⇒
                </button>
              )}
              <button
                type="button"
                onClick={() => onRemove(f.id)}
                style={{
                  background: "none",
                  border: "none",
                  color: "#f66",
                  cursor: "pointer",
                  fontSize: "0.8rem",
                  padding: "0 4px",
                }}
                title="Delete"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ---- 3D renderer attachment ----

type RenderWindowLike = {
  getRenderer: () => {
    addActor: (actor: unknown) => void;
    removeActor: (actor: unknown) => void;
  };
  getRenderWindow: () => { render: () => void };
};

interface GlyphsProps {
  fiducials: Fiducial[];
  spacing: [number, number, number];
  // Passing the raw generic render window rather than a typed handle
  // keeps this component decoupled from VolumeViewer's internals.
  getRenderWindow: () => RenderWindowLike | null;
  radiusVoxels?: number;
}

/**
 * Mounts sphere glyphs into the supplied vtk renderer for each
 * fiducial. The glyph set is rebuilt on every change — the list is
 * small (< 50 in practice), and incremental updates aren't worth the
 * extra bookkeeping.
 */
export function FiducialGlyphs({
  fiducials,
  spacing,
  getRenderWindow,
  radiusVoxels = 3,
}: GlyphsProps) {
  const actorsRef = useRef<unknown[]>([]);

  // Stable key so we don't rebuild when an unrelated parent re-renders.
  const fidKey = useMemo(
    () => fiducials.map((f) => `${f.id}:${f.position.join(",")}:${f.color.join(",")}`).join("|"),
    [fiducials],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  useEffect(() => {
    let cancelled = false;
    const localActors: unknown[] = [];

    (async () => {
      const rw = getRenderWindow();
      if (!rw) return;
      const [{ default: vtkSphereSource }, { default: vtkMapper }, { default: vtkActor }] =
        await Promise.all([
          import("@kitware/vtk.js/Filters/Sources/SphereSource"),
          import("@kitware/vtk.js/Rendering/Core/Mapper"),
          import("@kitware/vtk.js/Rendering/Core/Actor"),
        ]);
      if (cancelled) return;

      const renderer = rw.getRenderer();
      // Tear down previous actors.
      for (const a of actorsRef.current) {
        renderer.removeActor(a);
        // @ts-expect-error vtk objects expose .delete()
        a.delete?.();
      }
      actorsRef.current = [];

      const [sx, sy, sz] = spacing;
      const radiusMm = radiusVoxels * Math.min(sx, sy, sz);

      for (const f of fiducials) {
        const src = vtkSphereSource.newInstance({
          radius: radiusMm,
          thetaResolution: 16,
          phiResolution: 16,
          center: [f.position[0] * sx, f.position[1] * sy, f.position[2] * sz],
        });
        const mapper = vtkMapper.newInstance();
        mapper.setInputConnection(src.getOutputPort());
        const actor = vtkActor.newInstance();
        actor.setMapper(mapper);
        actor.getProperty().setColor(f.color[0], f.color[1], f.color[2]);
        actor.getProperty().setAmbient(0.3);
        actor.getProperty().setDiffuse(0.8);
        renderer.addActor(actor);
        localActors.push(actor);
      }
      actorsRef.current = localActors;
      rw.getRenderWindow().render();
    })();

    return () => {
      cancelled = true;
      const rw = getRenderWindow();
      if (!rw) return;
      const renderer = rw.getRenderer();
      for (const a of localActors) {
        renderer.removeActor(a);
        // @ts-expect-error vtk objects expose .delete()
        a.delete?.();
      }
      rw.getRenderWindow().render();
    };
  }, [fidKey, spacing[0], spacing[1], spacing[2], radiusVoxels]);

  return null;
}
