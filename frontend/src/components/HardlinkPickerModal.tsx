"use client";

// Folder picker modal that adds an existing resource (today: documents)
// as a hardlink to a target folder via ``POST /folders/{id}/items``
// (``foldersApi.addItem``). Distinct from "move" semantics — the
// resource keeps every existing folder placement and acquires one
// more, so the user can have the same document filed under several
// folders simultaneously. After 0088 every live document already has
// a folder containment, so this gesture creates the second + Nth
// hardlink and surfaces the chain-link badge on the card.
//
// The modal pulls all patient-scoped folders via
// ``foldersApi.list({ patientId })`` and filters out:
//   * the materialised root row (``is_root=true``) — invisible to the
//     user; path ``/`` opens its contents directly.
//   * the current folder where the resource already lives, when known.
// Hierarchy is rendered as an indented tree so the user can spot
// nested targets without losing the parent context.

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import NativeDialog from "@/components/NativeDialog";
import { ApiError, type FolderSummary, foldersApi } from "@/lib/api";

interface Props {
  open: boolean;
  patientId: string;
  /**
   * Folder the resource is currently in. Hidden from the picker so
   * the user can't add a duplicate hardlink to the same folder
   * (which would 409 on the unique constraint anyway).
   */
  excludeFolderId?: string | null;
  /**
   * Display name of the resource being linked. Used in the modal
   * heading so the user knows what's about to be linked.
   */
  resourceName: string;
  onClose: () => void;
  onPick: (folder: FolderSummary) => void | Promise<void>;
}

interface FolderTreeNode {
  folder: FolderSummary;
  depth: number;
}

function flattenTree(folders: FolderSummary[], excludeFolderId?: string | null): FolderTreeNode[] {
  // Build parent → children index, then DFS from "top-level user folders"
  // (those whose parent is the materialised root or null when missing).
  // The root row itself is excluded earlier; its direct children become
  // the visible top level.
  const childrenOf = new Map<string | null, FolderSummary[]>();
  for (const f of folders) {
    const key = f.parent_folder_id ?? null;
    const arr = childrenOf.get(key) ?? [];
    arr.push(f);
    childrenOf.set(key, arr);
  }
  // Identify which parent ids are roots from this listing's POV. The
  // listing already excludes the materialised root, so any folder
  // whose ``parent_folder_id`` references a row not present in the
  // listing is a "top-level user folder" we want to render at depth 0.
  const ids = new Set(folders.map((f) => f.id));
  const out: FolderTreeNode[] = [];
  function walk(nodes: FolderSummary[], depth: number) {
    const sorted = nodes.slice().sort((a, b) => a.name.localeCompare(b.name));
    for (const f of sorted) {
      if (f.id === excludeFolderId) continue;
      out.push({ folder: f, depth });
      const kids = childrenOf.get(f.id) ?? [];
      if (kids.length > 0) walk(kids, depth + 1);
    }
  }
  const topLevel = folders.filter(
    (f) => f.parent_folder_id === null || !ids.has(f.parent_folder_id ?? ""),
  );
  walk(topLevel, 0);
  return out;
}

export default function HardlinkPickerModal({
  open,
  patientId,
  excludeFolderId,
  resourceName,
  onClose,
  onPick,
}: Props) {
  const t = useTranslations("fascicolo.hardlinkPicker");
  const [folders, setFolders] = useState<FolderSummary[] | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setFolders(null);
    setLoadErr(null);
    let cancelled = false;
    foldersApi
      .list({ patientId })
      .then((rows) => {
        if (cancelled) return;
        setFolders(rows.filter((f) => !f.is_root));
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadErr(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [open, patientId]);

  const tree = useMemo(
    () => (folders ? flattenTree(folders, excludeFolderId) : []),
    [folders, excludeFolderId],
  );

  if (!open) return null;

  const handlePick = async (f: FolderSummary) => {
    setBusyId(f.id);
    try {
      await onPick(f);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <NativeDialog open={open} onClose={onClose} ariaLabel={t("title")} className="bv-dialog">
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, #0f172a)",
          borderRadius: 10,
          minWidth: 360,
          maxWidth: 480,
          maxHeight: "70vh",
          display: "flex",
          flexDirection: "column",
          padding: "1rem",
          boxShadow: "var(--bv-shadow-3, 0 12px 28px rgba(15,23,42,0.4))",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1rem", fontWeight: 600 }}>{t("title")}</h2>
        <p
          style={{
            margin: "0.25rem 0 0.75rem",
            color: "var(--bv-muted, #667085)",
            fontSize: "0.85rem",
          }}
        >
          {t("subtitle", { name: resourceName })}
        </p>
        <div
          style={{
            flex: 1,
            overflow: "auto",
            border: "1px solid var(--bv-card-border, #e5e7eb)",
            borderRadius: 6,
          }}
        >
          {loadErr && (
            <div style={{ padding: "0.75rem", color: "var(--bv-danger, #b91c1c)" }}>{loadErr}</div>
          )}
          {!folders && !loadErr && (
            <div style={{ padding: "0.75rem", color: "var(--bv-muted, #667085)" }}>
              {t("loading")}
            </div>
          )}
          {folders && tree.length === 0 && (
            <div style={{ padding: "0.75rem", color: "var(--bv-muted, #667085)" }}>
              {t("empty")}
            </div>
          )}
          {tree.map(({ folder, depth }) => (
            <button
              key={folder.id}
              type="button"
              onClick={() => handlePick(folder)}
              disabled={busyId !== null}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: `0.5rem 0.75rem 0.5rem ${0.75 + depth * 0.9}rem`,
                background: "transparent",
                border: "none",
                borderBottom: "1px solid var(--bv-card-border, #e5e7eb)",
                color: "inherit",
                cursor: busyId === folder.id ? "wait" : "pointer",
                fontSize: "0.9rem",
                opacity: busyId !== null && busyId !== folder.id ? 0.55 : 1,
              }}
            >
              <span aria-hidden style={{ marginRight: "0.4rem", color: "#e96b1f" }}>
                📁
              </span>
              {folder.name}
            </button>
          ))}
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            marginTop: "0.75rem",
          }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={busyId !== null}
            style={{
              padding: "0.4rem 0.9rem",
              borderRadius: 6,
              border: "1px solid var(--bv-card-border, #e5e7eb)",
              background: "var(--bv-card-bg, #fff)",
              color: "var(--bv-fg, #0f172a)",
              cursor: "pointer",
              fontSize: "0.85rem",
            }}
          >
            {t("cancel")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}
