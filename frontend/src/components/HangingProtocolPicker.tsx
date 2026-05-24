"use client";

import { HANGING_PROTOCOLS, type HangingProtocol } from "@/lib/hangingProtocols";

interface Props {
  value: string;
  modality?: string | null;
  onChange: (protocol: HangingProtocol) => void;
}

/**
 * Dropdown that lets the radiologist pick a preset hanging protocol.
 * The selected protocol's (layout, plane assignments) is passed back
 * to the parent via `onChange` so it can reconfigure the viewports.
 */
export default function HangingProtocolPicker({ value, modality, onChange }: Props) {
  const hint = modality ? `modality: ${modality.toUpperCase()}` : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      <select
        value={value}
        onChange={(e) => {
          const proto = HANGING_PROTOCOLS.find((p) => p.id === e.target.value);
          if (proto) onChange(proto);
        }}
        style={{
          background: "#1c2230",
          color: "#ddd",
          border: "1px solid #444",
          borderRadius: 4,
          padding: "0.3rem 0.4rem",
          fontSize: "0.75rem",
          width: "100%",
        }}
        title="Hanging protocol"
      >
        {HANGING_PROTOCOLS.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
          </option>
        ))}
      </select>
      {hint && (
        <span className="meta" style={{ fontSize: "0.65rem", color: "#888" }}>
          {hint}
        </span>
      )}
    </div>
  );
}
