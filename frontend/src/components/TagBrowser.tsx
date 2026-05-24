"use client";

/**
 * Tag browser widget — renders ``GET /api/tags/tree`` (namespace-grouped,
 * slash-nested paths) and routes clicks to the unified Patients view
 * with the ``tag`` filter applied: ``/patients?tag=<namespace>:<value>``.
 *
 * Per-tag chips show a provenance dot (manual/auto/imported) so the
 * user can tell where a tag came from. Hovering a chip surfaces the
 * full breakdown in the title attribute.
 */

import Link from "next/link";
import { useMemo, useState } from "react";

import type { TagTree, TagTreeNode } from "@/lib/api";

interface Props {
  tree: TagTree;
}

export default function TagBrowser({ tree }: Props) {
  const [filter, setFilter] = useState("");
  const namespaces = useMemo(
    () => [...tree].sort((a, b) => a.namespace.localeCompare(b.namespace)),
    [tree],
  );

  const needle = filter.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!needle) return namespaces;
    return namespaces
      .map((ns) => {
        const roots = filterNodes(ns.roots, ns.namespace, needle);
        return roots.length > 0 ? { namespace: ns.namespace, roots } : null;
      })
      .filter((x): x is { namespace: string; roots: TagTreeNode[] } => x !== null);
  }, [namespaces, needle]);

  return (
    <div className="tag-browser">
      <input
        type="search"
        placeholder="Filter tags (e.g. lung, CT, ...)"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        style={{ width: "100%", marginBottom: "1rem" }}
      />
      {filtered.length === 0 && <p className="meta">No tags match &ldquo;{filter}&rdquo;.</p>}
      {filtered.map((ns) => (
        <NamespaceGroup key={ns.namespace} namespace={ns.namespace} roots={ns.roots} />
      ))}
    </div>
  );
}

function filterNodes(nodes: TagTreeNode[], namespace: string, needle: string): TagTreeNode[] {
  const out: TagTreeNode[] = [];
  for (const n of nodes) {
    const childrenHit = filterNodes(n.children, namespace, needle);
    const selfHit =
      n.value.toLowerCase().includes(needle) || namespace.toLowerCase().includes(needle);
    if (selfHit || childrenHit.length > 0) {
      out.push({ ...n, children: childrenHit });
    }
  }
  return out;
}

function NamespaceGroup({
  namespace,
  roots,
}: {
  namespace: string;
  roots: TagTreeNode[];
}) {
  const [open, setOpen] = useState(true);
  const total = useMemo(() => sumCounts(roots), [roots]);
  return (
    <div className="card" style={{ padding: "0.75rem 1rem" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          background: "transparent",
          color: "inherit",
          border: "none",
          padding: 0,
          fontSize: "1rem",
          fontWeight: 600,
          display: "flex",
          alignItems: "center",
          gap: "0.35rem",
          width: "100%",
          justifyContent: "flex-start",
        }}
        aria-expanded={open}
      >
        <span style={{ display: "inline-block", width: "0.9rem" }}>{open ? "▾" : "▸"}</span>
        <span>{namespace}</span>
        <span className="meta" style={{ fontWeight: 400 }}>
          · {total}
        </span>
      </button>
      {open && (
        <ul
          style={{
            listStyle: "none",
            margin: "0.5rem 0 0",
            padding: 0,
            display: "flex",
            flexWrap: "wrap",
            gap: "0.4rem",
          }}
        >
          {flattenLeaves(roots).map((leaf) => (
            <li key={leaf.value}>
              <TagChip namespace={namespace} node={leaf} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TagChip({
  namespace,
  node,
}: {
  namespace: string;
  node: TagTreeNode;
}) {
  const dot = pickProvenance(node);
  const tooltip = `${node.count} total · ${node.manual_count} manual · ${node.auto_count} auto${node.imported_count > 0 ? ` · ${node.imported_count} imported` : ""}`;
  return (
    <Link
      href={`/patients?tag=${encodeURIComponent(`${namespace}:${node.value}`)}`}
      className="badge"
      title={tooltip}
      style={{
        textDecoration: "none",
        cursor: "pointer",
        display: "inline-flex",
        alignItems: "center",
        gap: "0.4rem",
      }}
    >
      <span
        aria-hidden
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: dot.color,
        }}
      />
      <span>{node.value}</span>
      <span className="meta" style={{ fontSize: "0.72rem" }}>
        ({node.count})
      </span>
    </Link>
  );
}

function pickProvenance(n: TagTreeNode): { kind: string; color: string } {
  // Decide the dot colour from the dominant source. Manual wins over
  // auto wins over imported when counts tie — human curation is the
  // most informative signal.
  const m = n.manual_count;
  const a = n.auto_count;
  const i = n.imported_count;
  if (m >= a && m >= i && m > 0) return { kind: "manual", color: "#0ea5e9" };
  if (a >= i && a > 0) return { kind: "auto", color: "#f59e0b" };
  if (i > 0) return { kind: "imported", color: "#94a3b8" };
  return { kind: "unknown", color: "#cbd5e1" };
}

function flattenLeaves(nodes: TagTreeNode[]): TagTreeNode[] {
  // Tag values can carry slash hierarchy (e.g. ``lung/upper-lobe``).
  // The chip strip flattens them so the user always sees the full
  // path; the tree shape is preserved in the API for future faceted
  // navigation but is not surfaced here yet.
  const out: TagTreeNode[] = [];
  for (const n of nodes) {
    if (n.count > 0) out.push(n);
    if (n.children.length > 0) out.push(...flattenLeaves(n.children));
  }
  return out.sort((a, b) => b.count - a.count || a.value.localeCompare(b.value));
}

function sumCounts(nodes: TagTreeNode[]): number {
  let s = 0;
  for (const n of nodes) {
    s += n.count;
    if (n.children.length > 0) s += sumCounts(n.children);
  }
  return s;
}
