"use client";

import { type FormEvent, useCallback, useState } from "react";

import type { VolumeData } from "./VolumeViewer";

export interface SegmentationMask {
  data: Uint8Array;
  color: [number, number, number];
}

interface Props {
  volume: VolumeData;
  onMaskChange: (mask: SegmentationMask | null) => void;
}

export default function SegmentationControls({ volume, onMaskChange }: Props) {
  const [lo, setLo] = useState(
    Math.round(volume.range[0] + (volume.range[1] - volume.range[0]) * 0.4),
  );
  const [hi, setHi] = useState(Math.round(volume.range[1]));
  const [color, setColor] = useState("#ff4444");
  const [active, setActive] = useState(false);

  const apply = useCallback(() => {
    const n = volume.scalars.length;
    const mask = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      mask[i] = volume.scalars[i] >= lo && volume.scalars[i] <= hi ? 1 : 0;
    }
    const r = Number.parseInt(color.slice(1, 3), 16) / 255;
    const g = Number.parseInt(color.slice(3, 5), 16) / 255;
    const b = Number.parseInt(color.slice(5, 7), 16) / 255;
    onMaskChange({ data: mask, color: [r, g, b] });
    setActive(true);
  }, [volume, lo, hi, color, onMaskChange]);

  const clear = useCallback(() => {
    onMaskChange(null);
    setActive(false);
  }, [onMaskChange]);

  const rangeMin = Math.floor(volume.range[0]);
  const rangeMax = Math.ceil(volume.range[1]);

  return (
    <>
      <h2>Segmentation</h2>
      <div className="card">
        <label className="meta" style={{ display: "block", fontSize: "0.7rem" }}>
          Threshold min: {lo}
          <input
            type="range"
            min={rangeMin}
            max={rangeMax}
            value={lo}
            onChange={(e) => setLo(Number(e.target.value))}
          />
        </label>
        <label className="meta" style={{ display: "block", fontSize: "0.7rem" }}>
          Threshold max: {hi}
          <input
            type="range"
            min={rangeMin}
            max={rangeMax}
            value={hi}
            onChange={(e) => setHi(Number(e.target.value))}
          />
        </label>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginTop: "0.5rem" }}>
          <label className="meta" style={{ fontSize: "0.7rem" }}>
            Color
            <input
              type="color"
              value={color}
              onChange={(e) => setColor(e.target.value)}
              style={{ marginLeft: "0.3rem", width: 28, height: 22, padding: 0, border: "none" }}
            />
          </label>
          <button type="button" className="viewer-btn" onClick={apply}>
            Apply
          </button>
          {active && (
            <button type="button" className="viewer-btn" onClick={clear}>
              Clear
            </button>
          )}
        </div>
      </div>
    </>
  );
}
