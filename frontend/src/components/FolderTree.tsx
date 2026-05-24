"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";

import { ApiError, type TreeNode, patientTreeApi } from "@/lib/api";

interface Props {
  patientId: string;
  currentPath: string;
  onNavigate: (path: string) => void;
}

/**
 * Left-pane folder tree. Renders the root listing and lazily expands folders
 * on click. Highlights `currentPath`. Non-folder leaves are not rendered here
 * (this is the navigation tree, not the content pane).
 */
export default function FolderTree({ patientId, currentPath, onNavigate }: Props) {
  const tFasc = useTranslations("fascicolo");
  const [root, setRoot] = useState<TreeNode[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const loadRoot = useCallback(async () => {
    try {
      const data = await patientTreeApi.tree(patientId, "/");
      setRoot(data.nodes.filter((n) => n.type === "folder"));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
    }
  }, [patientId]);

  useEffect(() => {
    loadRoot();
  }, [loadRoot]);

  if (err)
    return (
      <aside style={asideStyle} className="bv-folder-tree">
        <p className="error">{err}</p>
      </aside>
    );
  if (!root)
    return (
      <aside style={asideStyle} className="bv-folder-tree">
        <p className="meta">Loading...</p>
      </aside>
    );

  return (
    <aside style={asideStyle} aria-label="folder tree" className="bv-folder-tree">
      <TreeRow
        node={{
          id: "__root__",
          type: "folder",
          name: tFasc("treeRootLabel"),
          path: "/",
          parent_path: null,
          target_id: null,
          item_count: null,
          size_bytes: null,
          mime_type: null,
          date: null,
        }}
        depth={0}
        patientId={patientId}
        currentPath={currentPath}
        onNavigate={onNavigate}
        initialChildren={root}
        initialExpanded={true}
      />
    </aside>
  );
}

const asideStyle: React.CSSProperties = {
  minWidth: 200,
  width: 220,
  flexShrink: 0,
  background: "var(--bv-card-bg, #fff)",
  border: "1px solid var(--bv-card-border, #e5e7eb)",
  borderRadius: 10,
  padding: "0.5rem",
  overflowY: "auto",
  alignSelf: "flex-start",
  position: "sticky",
  top: "1rem",
  maxHeight: "calc(100vh - 7rem)",
};

interface RowProps {
  node: TreeNode;
  depth: number;
  patientId: string;
  currentPath: string;
  onNavigate: (path: string) => void;
  /** If supplied, used as initial children without another fetch. */
  initialChildren?: TreeNode[];
  initialExpanded?: boolean;
}

function TreeRow({
  node,
  depth,
  patientId,
  currentPath,
  onNavigate,
  initialChildren,
  initialExpanded = false,
}: RowProps) {
  const [expanded, setExpanded] = useState(initialExpanded);
  const [children, setChildren] = useState<TreeNode[] | null>(initialChildren ?? null);
  const [loading, setLoading] = useState(false);

  const fetchChildren = useCallback(async () => {
    setLoading(true);
    try {
      const data = await patientTreeApi.tree(patientId, node.path);
      setChildren(data.nodes.filter((n) => n.type === "folder"));
    } catch {
      setChildren([]);
    } finally {
      setLoading(false);
    }
  }, [patientId, node.path]);

  // Auto-expand + lazy-load ancestors of the current path so the user can
  // always see where they are in the tree after a deep-link navigation.
  useEffect(() => {
    const isAncestor =
      node.path !== "/" && (currentPath === node.path || currentPath.startsWith(`${node.path}/`));
    if (isAncestor && !expanded) setExpanded(true);
    if (isAncestor && children === null && !loading) fetchChildren();
  }, [currentPath, node.path, expanded, children, loading, fetchChildren]);

  async function toggle() {
    const next = !expanded;
    setExpanded(next);
    if (next && children === null) await fetchChildren();
  }

  const isActive = currentPath === node.path;
  const hasChildren = (children?.length ?? 0) > 0 || children === null;

  return (
    <div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.25rem",
          padding: "0.25rem 0.35rem",
          paddingLeft: `${0.35 + depth * 0.9}rem`,
          borderRadius: 4,
          cursor: "pointer",
          background: isActive ? "#fff7ef" : "transparent",
          color: isActive ? "#e96b1f" : "inherit",
          fontSize: "0.88rem",
          fontWeight: isActive ? 600 : 400,
        }}
      >
        <button
          type="button"
          onClick={toggle}
          aria-label={expanded ? "collapse" : "expand"}
          style={{
            background: "transparent",
            border: "none",
            color: "inherit",
            padding: 0,
            width: 14,
            height: 14,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            cursor: "pointer",
          }}
        >
          {hasChildren ? (expanded ? "\u25BE" : "\u25B8") : " "}
        </button>
        <button
          type="button"
          onClick={() => onNavigate(node.path)}
          style={{
            flex: 1,
            textAlign: "left",
            background: "transparent",
            border: "none",
            color: "inherit",
            font: "inherit",
            fontWeight: "inherit",
            padding: "0.1rem 0",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            gap: "0.35rem",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          <FolderIcon />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{node.name}</span>
          {typeof node.item_count === "number" && node.item_count > 0 && (
            <span style={{ color: "#94a3b8", fontSize: "0.75rem" }}>({node.item_count})</span>
          )}
        </button>
      </div>
      {expanded && (
        <div>
          {loading && (
            <div
              className="meta"
              style={{ paddingLeft: `${1.6 + depth * 0.9}rem`, fontSize: "0.8rem" }}
            >
              Loading...
            </div>
          )}
          {children?.map((c) => (
            <TreeRow
              key={c.id}
              node={c}
              depth={depth + 1}
              patientId={patientId}
              currentPath={currentPath}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FolderIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
    </svg>
  );
}
