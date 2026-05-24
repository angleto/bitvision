"use client";

import { type CSSProperties, type ReactNode, useState } from "react";

interface Props {
  title: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  headerRight?: ReactNode;
  style?: CSSProperties;
}

export default function CollapsiblePanel({
  title,
  defaultOpen = true,
  children,
  headerRight,
  style,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section
      style={{
        marginTop: "1rem",
        border: "1px solid var(--bv-card-border)",
        borderRadius: "var(--bv-r-md)",
        background: "var(--bv-card-bg)",
        ...style,
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        style={{
          all: "unset",
          boxSizing: "border-box",
          width: "100%",
          cursor: "pointer",
          padding: "0.7rem 1rem",
          display: "flex",
          alignItems: "center",
          gap: "0.6rem",
        }}
      >
        <span
          aria-hidden
          style={{
            display: "inline-block",
            transition: "transform 0.15s ease",
            transform: open ? "rotate(90deg)" : "rotate(0deg)",
            color: "var(--bv-muted)",
            fontSize: "0.8rem",
          }}
        >
          ▸
        </span>
        <span style={{ fontWeight: 600, flex: 1, minWidth: 0 }}>{title}</span>
        {headerRight && (
          <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
            {headerRight}
          </span>
        )}
      </button>
      {open && <div style={{ padding: "0 1rem 1rem" }}>{children}</div>}
    </section>
  );
}
