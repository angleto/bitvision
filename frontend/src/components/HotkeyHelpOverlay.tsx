"use client";

import NativeDialog from "@/components/NativeDialog";
import type { HotkeyBinding } from "@/lib/hotkeys";
import { formatHotkey } from "@/lib/hotkeys";

interface Section {
  title: string;
  bindings: HotkeyBinding[];
}

interface Props {
  open: boolean;
  onClose: () => void;
  sections: Section[];
}

// Modal overlay listing every registered shortcut grouped by section.
// Rendered by the viewer page when the user hits "?".
export default function HotkeyHelpOverlay({ open, onClose, sections }: Props) {
  return (
    <NativeDialog
      open={open}
      onClose={onClose}
      ariaLabel="Keyboard shortcuts"
      className="bv-dialog"
    >
      <div
        role="document"
        style={{
          background: "#121722",
          color: "#ddd",
          border: "1px solid #2a2f3b",
          borderRadius: 8,
          padding: "1.25rem 1.5rem",
          minWidth: 420,
          maxWidth: 720,
          maxHeight: "80vh",
          overflowY: "auto",
          boxShadow: "0 20px 60px rgba(0,0,0,0.6)",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "0.75rem",
          }}
        >
          <h2 style={{ margin: 0, fontSize: "1.05rem", color: "#e96b1f" }}>Keyboard shortcuts</h2>
          <button
            type="button"
            onClick={onClose}
            className="viewer-btn"
            style={{ fontSize: "0.75rem" }}
            aria-label="Close"
          >
            Close (Esc)
          </button>
        </div>
        {sections.map((section) => (
          <div key={section.title} style={{ marginBottom: "0.9rem" }}>
            <div
              style={{
                fontSize: "0.72rem",
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                color: "#8a93a3",
                marginBottom: "0.35rem",
              }}
            >
              {section.title}
            </div>
            <table style={{ width: "100%", fontSize: "0.8rem", borderCollapse: "collapse" }}>
              <tbody>
                {section.bindings.map((b, idx) => (
                  <tr key={`${b.key}-${idx}`}>
                    <td style={{ padding: "0.15rem 0.5rem 0.15rem 0", width: 140 }}>
                      <kbd
                        style={{
                          fontFamily: "monospace",
                          fontSize: "0.75rem",
                          background: "#1c2230",
                          border: "1px solid #2a2f3b",
                          borderRadius: 3,
                          padding: "0.1rem 0.4rem",
                          color: "#cfd6e4",
                        }}
                      >
                        {formatHotkey(b)}
                      </kbd>
                    </td>
                    <td style={{ padding: "0.15rem 0", color: "#bbb" }}>
                      {b.description ?? "(no description)"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        <div
          style={{
            fontSize: "0.7rem",
            color: "#666",
            marginTop: "0.5rem",
            borderTop: "1px solid #2a2f3b",
            paddingTop: "0.5rem",
          }}
        >
          Shortcuts are inactive while typing in an input field.
        </div>
      </div>
    </NativeDialog>
  );
}

export type { Section as HotkeyHelpSection };
