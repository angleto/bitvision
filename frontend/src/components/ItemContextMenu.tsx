"use client";

// Right-click context menu for Drive-style item cards. Position is absolute
// at the cursor; Esc / outside click / any action closes it. Menu entries are
// filtered per `item.kind` — e.g. "Rename" only appears for folders and
// documents, "Move to…" / "Delete" are hidden when `item.is_root` is true.

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef } from "react";

import type { ItemKind } from "@/lib/api";

export type ItemAction = "open" | "share" | "download" | "rename" | "move" | "delete";

export interface ContextMenuItem {
  id: string;
  kind: ItemKind;
  name: string;
  is_root?: boolean;
}

interface Props {
  item: ContextMenuItem;
  x: number;
  y: number;
  onAction: (action: ItemAction, item: ContextMenuItem) => void;
  onClose: () => void;
}

interface Entry {
  action: ItemAction;
  label: string;
  danger?: boolean;
}

function buildEntries(item: ContextMenuItem, t: (key: string) => string): Entry[] {
  const out: Entry[] = [
    { action: "open", label: t("open") },
    { action: "share", label: t("share") },
    { action: "download", label: t("download") },
  ];
  if (item.kind === "folder" || item.kind === "document") {
    out.push({ action: "rename", label: t("rename") });
  }
  if (!item.is_root) {
    out.push({ action: "move", label: t("moveTo") });
    out.push({ action: "delete", label: t("delete"), danger: true });
  }
  return out;
}

export default function ItemContextMenu({ item, x, y, onAction, onClose }: Props) {
  const t = useTranslations("itemContext");
  const ref = useRef<HTMLDivElement | null>(null);
  const entries = useMemo(() => buildEntries(item, t), [item, t]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    window.addEventListener("keydown", onKey);
    // Use capture so we react even if a parent calls stopPropagation().
    window.addEventListener("mousedown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown, true);
    };
  }, [onClose]);

  // Clamp inside viewport so the menu doesn't clip off the right/bottom edge.
  const { left, top } = useMemo(() => {
    const w = 200;
    const h = Math.max(40, entries.length * 34 + 8);
    const vw = typeof window !== "undefined" ? window.innerWidth : 1024;
    const vh = typeof window !== "undefined" ? window.innerHeight : 768;
    return {
      left: Math.min(x, vw - w - 4),
      top: Math.min(y, vh - h - 4),
    };
  }, [x, y, entries.length]);

  return (
    <div
      ref={ref}
      role="menu"
      aria-label={t("ariaLabel", { name: item.name })}
      style={{
        position: "fixed",
        left,
        top,
        minWidth: 200,
        background: "var(--bv-card-bg, #fff)",
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        borderRadius: 8,
        boxShadow: "0 6px 24px rgba(0,0,0,0.15)",
        padding: "4px 0",
        zIndex: 1000,
      }}
    >
      {entries.map((e) => (
        <button
          key={e.action}
          role="menuitem"
          type="button"
          onClick={() => {
            onAction(e.action, item);
            onClose();
          }}
          style={{
            display: "block",
            width: "100%",
            textAlign: "left",
            background: "transparent",
            color: e.danger ? "#b42318" : "var(--bv-fg, #111)",
            border: "none",
            padding: "0.45rem 0.9rem",
            fontSize: "0.9rem",
            borderRadius: 0,
            cursor: "pointer",
          }}
          onMouseEnter={(ev) => {
            (ev.currentTarget as HTMLButtonElement).style.background = "rgba(233,107,31,0.1)";
          }}
          onMouseLeave={(ev) => {
            (ev.currentTarget as HTMLButtonElement).style.background = "transparent";
          }}
        >
          {e.label}
        </button>
      ))}
    </div>
  );
}
