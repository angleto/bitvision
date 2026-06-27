"use client";

import { useTranslations } from "next-intl";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import Breadcrumb from "@/components/Breadcrumb";
import ContentPane, { type ViewMode } from "@/components/ContentPane";
import CurrentFolderHeader from "@/components/CurrentFolderHeader";
import EditFolderDialog from "@/components/EditFolderDialog";
import ExportFascicoloDialog from "@/components/ExportFascicoloDialog";
import FolderTree from "@/components/FolderTree";
import HardlinkPickerModal from "@/components/HardlinkPickerModal";
import { useModal } from "@/components/ModalHost";
import NativeDialog from "@/components/NativeDialog";
import {
  ApiError,
  type BreadcrumbSegment,
  type BulkItemRef,
  type FolderSummary,
  type ItemKind,
  type TreeListing,
  type TreeNode,
  bulkApi,
  foldersApi,
  patientTreeApi,
} from "@/lib/api";

interface Props {
  patientId: string;
  isOwner: boolean;
}

const VIEW_MODE_KEYS: ReadonlyArray<
  readonly [ViewMode, "viewModeGrid" | "viewModeList" | "viewModeTimeline"]
> = [
  ["grid", "viewModeGrid"],
  ["list", "viewModeList"],
  ["timeline", "viewModeTimeline"],
];

type SortMode = "name" | "updated" | "created";

const SORT_MODE_KEYS: ReadonlyArray<
  readonly [SortMode, "sortByName" | "sortByUpdated" | "sortByCreated"]
> = [
  ["name", "sortByName"],
  ["updated", "sortByUpdated"],
  ["created", "sortByCreated"],
];

// Natural-numeric collator: "1" < "2" < "10". Locale-aware,
// case-insensitive (matches "case 1" and "Case 1" the same way).
const NAME_COLLATOR = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: "base",
});

/**
 * Three-pane (tree | breadcrumb + content) Drive-style layout for the
 * patient fascicolo.
 *
 * Resilience: if the tree endpoint 404s (F2 not yet deployed) we render a
 * single info card instead of the whole UI so the page doesn't look broken.
 */

// Hoisted so the Set identity is stable across renders; useCallback
// deps that reference it then converge to a stable callback.
const MOVABLE_KINDS: ReadonlySet<ItemKind> = new Set(["folder", "study", "document"]);
// ``annotation`` is excluded from BULK_KINDS — the backend bulk
// endpoint doesn't accept it and the UI doesn't expose it as a
// deletable row in this view.
const BULK_KINDS: ReadonlySet<ItemKind> = new Set([
  "folder",
  "study",
  "series",
  "document",
  "report",
  "consultation",
]);

export default function FascicoloDriveLayout({ patientId, isOwner }: Props) {
  const tFasc = useTranslations("fascicolo");
  const tExport = useTranslations("export");
  // Initial folder path comes from ``?path=`` in the URL so deep-links
  // (e.g. "back to the folder I was in" from a document detail page)
  // land the user inside the right folder, not at the patient root.
  // Subsequent in-page navigation is purely client-side.
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const initialPath = searchParams.get("path") || "/";
  const [currentPath, setCurrentPath] = useState(initialPath);
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  // Default sort is creation date (newest first). For a clinical
  // record the natural axis is "when did this enter the fascicolo",
  // not the alphabetical filename. Folders carry ``clinical_date`` so
  // their position reflects the underlying episode date, not just the
  // metadata-edit timestamp.
  const [sortMode, setSortMode] = useState<SortMode>("created");
  // Sort direction is independent of sort field. Default ``desc`` for
  // dates (newest first, the clinically natural orientation) and for
  // names (z→a is unusual but the user can flip with one click).
  // Clicking the active sort field flips direction; clicking a
  // different field selects it without changing direction.
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [listing, setListing] = useState<TreeListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [newFolderBusy, setNewFolderBusy] = useState(false);
  // Resolved parent-folder id of the currently-listed folder.
  // ``null`` at depth ≤ 1 (the parent is the patient root, which
  // has no folder id). At depth ≥ 2 we resolve it via a one-shot
  // ``tree(parentPath)`` call when the listing changes; the
  // parent listing is small (just the breadcrumb + siblings) and
  // the round-trip only fires the rare time the user descends
  // into a sub-folder of a sub-folder.
  const [parentFolderId, setParentFolderId] = useState<string | null>(null);
  const [moveTargetOpen, setMoveTargetOpen] = useState(false);
  // Tracked at the layout level so the Breadcrumb (rendered next to the
  // grid) can light up its crumbs as drop targets while a card is being
  // dragged. ContentPane owns its own draggingId for in-grid interactions
  // but reports drag start/end here too via ``onDragStartId`` /
  // ``onDragEnd`` so the breadcrumb stays in sync.
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null);
  // Folder picker for the "add to another folder" hardlink gesture.
  // ``hardlinkTarget`` carries the document the user clicked the
  // chain-link button on (or the dragged node when Cmd/Alt+drag is
  // used); the picker resolves to a folder id and the layout fires
  // ``foldersApi.addItem`` to create the hardlink.
  const [hardlinkTarget, setHardlinkTarget] = useState<TreeNode | null>(null);
  const modal = useModal();
  // Selection state for batch delete. Keyed by ``TreeNode.id`` —
  // unique even across kinds because the backend uses uuid pks. The
  // ``selectedNodes`` map keeps the original TreeNode around so the
  // batch action knows the kind for each id (folder vs study vs
  // document...).
  const [selectedNodes, setSelectedNodes] = useState<Map<string, TreeNode>>(() => new Map());
  // Edit-folder dialog state. The pencil affordance on a folder card
  // opens a richer two-field form (name + description) instead of the
  // single-field rename prompt used for studies and documents.
  const [editFolderTarget, setEditFolderTarget] = useState<TreeNode | null>(null);
  const [editFolderBusy, setEditFolderBusy] = useState(false);
  const [editFolderErr, setEditFolderErr] = useState<string | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [folderExportId, setFolderExportId] = useState<string | null>(null);

  const loadListing = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const data = await patientTreeApi.tree(patientId, currentPath);
      setListing(data);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "load failed");
      setListing(null);
    } finally {
      setLoading(false);
    }
  }, [patientId, currentPath]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadTick is the explicit "force-refresh" trigger bumped by folder mutations.
  useEffect(() => {
    loadListing();
  }, [loadListing, reloadTick]);

  // ``#item-<uuid>`` rides on back-links emitted by ``BackToFolderLink``
  // (and on browser-back hash pops). When present after a listing
  // load, scroll the matching card into view and flash a short outline
  // so the user sees where they were before they opened the detail
  // page. Without this, returning to a long folder always lands at
  // the top and the clinician has to re-find the row.
  useEffect(() => {
    if (!listing) return;
    const flashFromHash = () => {
      if (typeof window === "undefined") return;
      const hash = window.location.hash;
      const m = hash.match(/^#item-([0-9a-zA-Z_-]+)$/);
      if (!m) return;
      const id = m[1];
      // Defer a frame so ContentPane has had a chance to render the
      // grid/list rows after ``setListing``; otherwise the selector
      // misses on cold load.
      const raf = window.requestAnimationFrame(() => {
        const el = document.querySelector<HTMLElement>(`[data-item-id="${CSS.escape(id)}"]`);
        if (!el) return;
        el.scrollIntoView({ block: "center", behavior: "smooth" });
        el.classList.add("bv-highlight-flash");
        window.setTimeout(() => el.classList.remove("bv-highlight-flash"), 1600);
      });
      return raf;
    };
    const raf = flashFromHash();
    // ``hashchange`` covers back/forward where the URL hash flips
    // without remounting the listing (Next preserves listing state
    // across same-pathname history pops).
    window.addEventListener("hashchange", flashFromHash);
    return () => {
      window.removeEventListener("hashchange", flashFromHash);
      if (raf !== undefined) window.cancelAnimationFrame(raf);
    };
  }, [listing]);

  // ``?folder=<uuid>`` is the canonical input form coming from a
  // ``@folder:UUID`` mention in an evidence note: the editor knows the
  // folder's id but not its current path (the path is derived state and
  // can change when an ancestor is renamed/moved). Resolve the uuid to
  // the live path via ``breadcrumbForItem`` and rewrite the URL to the
  // canonical ``?path=`` form so all downstream logic — ``loadListing``,
  // breadcrumb rendering, ``navigate``, deep-link surfaces — keeps using
  // a single source of truth (path strings). Replace, not push, so the
  // browser back-button still returns to the previous page rather than
  // bouncing between ``?folder=`` and ``?path=`` for the same view.
  useEffect(() => {
    const folderId = searchParams.get("folder");
    if (!folderId) return;
    let cancelled = false;
    patientTreeApi
      .breadcrumbForItem(patientId, "folder", folderId)
      .then((segments) => {
        if (cancelled) return;
        const last = segments[segments.length - 1];
        const resolvedPath = last?.path && last.path !== "/" ? last.path : "/";
        const next = new URLSearchParams(searchParams);
        next.delete("folder");
        if (resolvedPath !== "/") next.set("path", resolvedPath);
        else next.delete("path");
        const qs = next.toString();
        router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
        // Keep the in-memory ``currentPath`` aligned with the URL so the
        // listing fetch + breadcrumb render the resolved folder, not
        // the patient root we initialised from.
        setCurrentPath(resolvedPath);
      })
      .catch(() => {
        // Folder not found / permission denied / cross-patient: drop
        // the param silently and stay at whatever ``?path=`` already
        // resolved (typically the patient root). Surfacing an error
        // here would be noisy: the user got here via a stale mention,
        // not a deliberate URL.
        if (cancelled) return;
        const next = new URLSearchParams(searchParams);
        next.delete("folder");
        const qs = next.toString();
        router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, searchParams, router, pathname]);

  // Resolve the parent folder's id whenever ``currentPath`` changes.
  // Depth 0 (root) → no parent. Depth 1 → parent is patient root
  // (folder id is null, no API call needed). Depth ≥ 2 → fetch the
  // parent listing once to learn its folder_id; the call is small
  // and only fires when the user is actually inside a nested folder.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadTick re-fetches after a folder mutation.
  useEffect(() => {
    if (currentPath === "/" || !currentPath) {
      setParentFolderId(null);
      return;
    }
    const segs = currentPath.split("/").filter(Boolean);
    if (segs.length <= 1) {
      // Parent is the patient root.
      setParentFolderId(null);
      return;
    }
    let cancelled = false;
    const parentPath = `/${segs.slice(0, -1).join("/")}`;
    patientTreeApi
      .tree(patientId, parentPath)
      .then((l) => {
        if (!cancelled) setParentFolderId(l.folder_id);
      })
      .catch(() => {
        if (!cancelled) setParentFolderId(null);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, currentPath, reloadTick]);

  const parentNavigation = (() => {
    if (currentPath === "/" || !currentPath) return undefined;
    const segs = currentPath.split("/").filter(Boolean);
    if (segs.length === 0) return undefined;
    const parentPath = segs.length === 1 ? "/" : `/${segs.slice(0, -1).join("/")}`;
    return { parentPath, parentFolderId };
  })();

  const navigate = useCallback(
    (path: string) => {
      setCurrentPath(path);
      // Mirror the active folder onto ``?path=`` so refresh / back
      // returns to the same folder. The root collapses the param
      // away so a clean ``/patients/<id>`` URL stays clean. Use
      // ``replace`` to avoid stacking a history entry per click.
      const next = new URLSearchParams(searchParams);
      if (path === "/" || !path) next.delete("path");
      else next.set("path", path);
      const qs = next.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  // Resolve current view contents up-front so the selection helpers
  // can close over them. The two values are also re-used below in
  // the JSX, but the binding has to land here for the hooks ordering
  // (callbacks declared later use ``nodes``).
  const breadcrumb: BreadcrumbSegment[] = listing?.breadcrumb ?? [];
  const nodes: TreeNode[] = listing?.nodes ?? [];

  // Visual sort applied before rendering. Drop-target lookup,
  // selection, and batch ops use ``nodes`` (unsorted) which is fine
  // because they're keyed by id — order-independent.
  // Date keying:
  // - "created"  → ``node.date`` (clinical/display date with fallback
  //   to ``created_at``; this is the timestamp shown on the card so
  //   the visual order matches the data the user reads).
  // - "updated"  → ``node.updated_at`` (system-side last-edit).
  const sortedNodes = useMemo(() => {
    const out = [...nodes];
    const dirSign = sortDir === "asc" ? 1 : -1;
    if (sortMode === "name") {
      out.sort((a, b) => dirSign * NAME_COLLATOR.compare(a.name, b.name));
      return out;
    }
    const pickDateKey = (n: TreeNode): string => {
      if (sortMode === "updated") return n.updated_at ?? "";
      return n.date ?? n.created_at ?? "";
    };
    out.sort((a, b) => {
      const A = pickDateKey(a);
      const B = pickDateKey(b);
      // Nodes missing the date sink to the bottom regardless of
      // direction: an undated row is never useful to surface, and
      // flipping it to the top in ascending mode would defeat the
      // affordance.
      if (!A && !B) return 0;
      if (!A) return 1;
      if (!B) return -1;
      return dirSign * A.localeCompare(B);
    });
    return out;
  }, [nodes, sortMode, sortDir]);

  const handleRenameItem = useCallback(
    async (node: TreeNode) => {
      // The dispatch list mirrors the backend whitelist: folder /
      // study / document. Series, reports, consultations don't
      // expose a writable name surface yet (and the pencil button
      // doesn't render for them either, but we re-check here in
      // case the caller wires it differently in the future).
      if (!["folder", "study", "document"].includes(node.type)) return;
      // Folders open the richer edit-metadata dialog (name +
      // description). Studies and documents keep the single-field
      // rename prompt because the backend rename endpoint only
      // accepts a name for those kinds.
      if (node.type === "folder") {
        setEditFolderTarget(node);
        setEditFolderErr(null);
        return;
      }
      const next = await modal.prompt({
        title: tFasc("renameTitle"),
        label: node.type === "study" ? tFasc("renameLabelStudy") : tFasc("renameLabelDocument"),
        defaultValue: node.name,
      });
      const trimmed = next?.trim();
      if (!trimmed || trimmed === node.name) return;
      try {
        const resourceId = node.target_id ?? node.id;
        await patientTreeApi.renameItem(patientId, node.type, resourceId, trimmed);
        setReloadTick((t) => t + 1);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "rename failed");
      }
    },
    [modal, patientId, tFasc],
  );

  const handleEditFolderSubmit = useCallback(
    async (patch: { name: string; description: string | null; createdAt: string | null }) => {
      if (!editFolderTarget) return;
      setEditFolderBusy(true);
      setEditFolderErr(null);
      try {
        const apiPatch: {
          name?: string;
          description: string | null;
          created_at?: string;
        } = {
          description: patch.description,
        };
        if (patch.name !== editFolderTarget.name) {
          apiPatch.name = patch.name;
        }
        if (patch.createdAt) {
          apiPatch.created_at = patch.createdAt;
        }
        await patientTreeApi.updateFolder(patientId, editFolderTarget.id, apiPatch);
        setEditFolderTarget(null);
        setReloadTick((t) => t + 1);
      } catch (e) {
        setEditFolderErr(e instanceof ApiError ? e.message : "save failed");
      } finally {
        setEditFolderBusy(false);
      }
    },
    [editFolderTarget, patientId],
  );

  const handleDeleteFolder = useCallback(
    async (node: TreeNode) => {
      // ``id`` for a folder node is the folder's UUID (see backend
      // patient_tree._folder_node). Children survive: studies/documents
      // re-surface at the patient root.
      const ok = await modal.confirm({
        title: tFasc("deleteFolderTitle"),
        message: tFasc("deleteFolderMessage", { name: node.name }),
        confirmLabel: tFasc("deleteFolderConfirm"),
        destructive: true,
      });
      if (!ok) return;
      try {
        await patientTreeApi.deleteFolder(patientId, node.id);
        setReloadTick((t) => t + 1);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "delete failed");
      }
    },
    [modal, patientId, tFasc],
  );

  const toggleSelection = useCallback((node: TreeNode) => {
    setSelectedNodes((prev) => {
      const next = new Map(prev);
      if (next.has(node.id)) next.delete(node.id);
      else next.set(node.id, node);
      return next;
    });
  }, []);

  const setSelection = useCallback((nodes: TreeNode[]) => {
    setSelectedNodes(new Map(nodes.map((n) => [n.id, n] as const)));
  }, []);

  const clearSelection = useCallback(() => {
    setSelectedNodes(new Map());
  }, []);

  // ``add_item_to_folder`` to a target folder. Translates the
  // FE-side ``parentFolderId === null`` (legacy "root") to "the
  // patient's materialised root" by surfacing 422 from the backend
  // (today the API rejects null target_folder_id; the picker never
  // emits null, so we keep the helper simple).
  const linkResourceToFolder = useCallback(
    async (dragged: TreeNode, targetFolderId: string, _targetFolderName: string) => {
      const kind = dragged.type;
      const id = dragged.target_id ?? dragged.id;
      try {
        await foldersApi.addItem(targetFolderId, kind, id);
        // Reload the listing — the chain-link badge appears on the
        // card as soon as ``folder_count`` crosses 2.
        setReloadTick((t) => t + 1);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "hardlink failed");
      }
    },
    [],
  );

  const handleHardlinkOnNode = useCallback(
    async (dragged: TreeNode, target: TreeNode) => {
      if (target.type !== "folder") return;
      await linkResourceToFolder(dragged, target.id, target.name);
    },
    [linkResourceToFolder],
  );

  const handleHardlinkOnParent = useCallback(
    async (dragged: TreeNode, parentFolderId: string | null) => {
      if (!parentFolderId) {
        // Cmd+drag onto ".." at the patient root has no second
        // folder to link to — fall back to the picker so the user
        // can pick an explicit target.
        setHardlinkTarget(dragged);
        return;
      }
      await linkResourceToFolder(dragged, parentFolderId, tFasc("list.parentFolder"));
    },
    [linkResourceToFolder, tFasc],
  );

  const handleHardlinkPickerPick = useCallback(
    async (folder: FolderSummary) => {
      const target = hardlinkTarget;
      setHardlinkTarget(null);
      if (!target) return;
      await linkResourceToFolder(target, folder.id, folder.name);
    },
    [hardlinkTarget, linkResourceToFolder],
  );

  const handleDropOnParent = useCallback(
    async (dragged: TreeNode, targetFolderId: string | null) => {
      try {
        await patientTreeApi.move(
          patientId,
          dragged.type,
          dragged.target_id ?? dragged.id,
          targetFolderId,
        );
        setReloadTick((t) => t + 1);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "move failed");
      }
    },
    [patientId],
  );

  // Drop a card onto a Breadcrumb crumb → move the resource to that
  // segment's folder_id (or the patient root when ``folder_id`` is
  // null, e.g. dragging onto the home crumb). Reuses the same API
  // path as ``handleDropOnParent``; we look up the dragged node from
  // the current listing so kind / target_id are resolved correctly.
  const handleDropOnBreadcrumb = useCallback(
    async (segment: BreadcrumbSegment, draggedId: string | null, copyMode: boolean) => {
      if (!draggedId) return;
      const dragged = (listing?.nodes ?? []).find((n) => n.id === draggedId);
      if (!dragged) return;
      if (copyMode) {
        // Cmd/Alt+drag onto a crumb = hardlink to that segment's
        // folder. Dropping on the synthetic root crumb has no
        // resolvable folder id, so fall back to the picker.
        if (!segment.id) {
          setHardlinkTarget(dragged);
          return;
        }
        await linkResourceToFolder(dragged, segment.id, segment.name);
        return;
      }
      try {
        await patientTreeApi.move(
          patientId,
          dragged.type,
          dragged.target_id ?? dragged.id,
          segment.id ?? null,
        );
        setDraggingNodeId(null);
        setReloadTick((t) => t + 1);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "move failed");
      }
    },
    [patientId, listing, linkResourceToFolder],
  );

  const handleBatchMove = useCallback(
    async (targetFolderId: string | null) => {
      if (selectedNodes.size === 0) return;
      const movable: { kind: ItemKind; id: string }[] = [];
      for (const node of selectedNodes.values()) {
        const kind = node.type as ItemKind;
        if (!MOVABLE_KINDS.has(kind)) continue;
        const id = kind === "folder" ? node.id : (node.target_id ?? node.id);
        movable.push({ kind, id });
      }
      if (movable.length === 0) {
        setErr(tFasc("batch.moveErrNoneMovable"));
        return;
      }
      // No bulk endpoint for move yet; iterate. Per-item failures are
      // collected and surfaced after the loop so the user sees what
      // moved vs what didn't.
      const failures: string[] = [];
      for (const it of movable) {
        try {
          await patientTreeApi.move(patientId, it.kind, it.id, targetFolderId);
        } catch (e) {
          failures.push(`${it.kind} ${it.id}: ${e instanceof ApiError ? e.message : String(e)}`);
        }
      }
      clearSelection();
      setReloadTick((t) => t + 1);
      if (failures.length > 0) {
        setErr(
          tFasc("batch.moveErrPartial", {
            n: failures.length,
            details: failures.join("; "),
          }),
        );
      }
    },
    // ``MOVABLE_KINDS`` is a module-level Set (hoisted above), so it
    // doesn't belong in the dep list; ``tFasc`` is the i18n function
    // whose identity depends only on the active locale.
    [selectedNodes, clearSelection, patientId, tFasc],
  );

  const selectAllVisible = useCallback(() => {
    setSelectedNodes((prev) => {
      const next = new Map(prev);
      const allSelected = nodes.length > 0 && nodes.every((n) => next.has(n.id));
      if (allSelected) {
        for (const n of nodes) next.delete(n.id);
      } else {
        for (const n of nodes) next.set(n.id, n);
      }
      return next;
    });
  }, [nodes]);

  const handleBatchDelete = useCallback(async () => {
    if (selectedNodes.size === 0) return;
    const items: BulkItemRef[] = [];
    for (const node of selectedNodes.values()) {
      const kind = node.type as ItemKind;
      if (!BULK_KINDS.has(kind)) continue;
      // Folders use ``node.id`` directly; non-folder leaves use the
      // backing target_id since ``node.id`` is the tree-node uuid,
      // not the resource uuid (see backend patient_tree._study_node
      // et al. where ``id`` and ``target_id`` are split).
      const id = kind === "folder" ? node.id : (node.target_id ?? node.id);
      items.push({ id, kind });
    }
    if (items.length === 0) return;
    const ok = await modal.confirm({
      title: tFasc("batch.deleteTitle"),
      message: tFasc("batch.deleteMessage", { n: items.length }),
      destructive: true,
      confirmLabel: tFasc("batch.deleteConfirm"),
    });
    if (!ok) return;
    try {
      await bulkApi.remove({ items });
      clearSelection();
      setReloadTick((t) => t + 1);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "delete failed");
    }
  }, [selectedNodes, modal, clearSelection, tFasc]);

  /**
   * Drag-and-drop reducer.
   *
   *   * dragged on a folder       → move dragged into that folder.
   *   * dragged on another leaf   → ask the user whether to create
   *     a new folder named after the target and group both.
   */
  const handleDropOnNode = useCallback(
    async (dragged: TreeNode, target: TreeNode) => {
      if (dragged.id === target.id) return;
      try {
        if (target.type === "folder") {
          await patientTreeApi.move(
            patientId,
            dragged.type,
            dragged.target_id ?? dragged.id,
            target.id,
          );
          setReloadTick((t) => t + 1);
          return;
        }
        // Both leaves: offer to create a folder named after the
        // target and group both items inside it. Keeps the
        // Finder-style "drop file on file makes folder" gesture
        // explicit so the user doesn't lose track of what just
        // happened.
        const ok = await modal.confirm({
          title: tFasc("batch.groupTitle"),
          message: tFasc("batch.groupMessage", {
            name: target.name,
            dragged: dragged.name,
          }),
          confirmLabel: tFasc("batch.groupConfirm"),
        });
        if (!ok) return;
        // Create the folder under the *current* listing's folder
        // (so the new container lives where the user was browsing
        // when they dropped). ``listing.folder_id`` is null at the
        // patient root.
        const parentFolderId = listing?.folder_id ?? null;
        const newFolder = await patientTreeApi.createFolder(patientId, parentFolderId, target.name);
        await patientTreeApi.move(
          patientId,
          target.type,
          target.target_id ?? target.id,
          newFolder.id,
        );
        await patientTreeApi.move(
          patientId,
          dragged.type,
          dragged.target_id ?? dragged.id,
          newFolder.id,
        );
        setReloadTick((t) => t + 1);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "drop failed");
      }
    },
    [patientId, modal, listing, tFasc],
  );

  // Cross-patient reassign — opens an inline patient picker, then a
  // strong confirmation modal that names both the source and target
  // patients before firing the irreversible request.
  const [reassignOpen, setReassignOpen] = useState(false);
  const handleBatchReassign = useCallback(
    async (target: { id: string; display_name: string }) => {
      if (selectedNodes.size === 0) return;
      const REASSIGNABLE: ReadonlySet<ItemKind> = new Set(["study", "document"]);
      const items: BulkItemRef[] = [];
      for (const node of selectedNodes.values()) {
        const kind = node.type as ItemKind;
        if (!REASSIGNABLE.has(kind)) continue;
        const id = node.target_id ?? node.id;
        items.push({ id, kind });
      }
      if (items.length === 0) {
        setErr(tFasc("batch.reassignErrNone"));
        return;
      }
      const ok = await modal.confirm({
        title: tFasc("batch.reassignTitle"),
        message: tFasc("batch.reassignMessage", {
          n: items.length,
          target: target.display_name,
        }),
        destructive: true,
        confirmLabel: tFasc("batch.reassignConfirm"),
      });
      if (!ok) return;
      try {
        const res = await bulkApi.reassignPatient({
          items,
          target_patient_id: target.id,
        });
        clearSelection();
        setReloadTick((t) => t + 1);
        if (res.failed.length > 0) {
          setErr(
            tFasc("batch.reassignErrPartial", {
              ok: res.succeeded.length,
              ko: res.failed.length,
              details: res.failed.map((f) => `${f.id}: ${f.reason}`).join("; "),
            }),
          );
        }
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "reassign failed");
      }
    },
    [selectedNodes, modal, clearSelection, tFasc],
  );

  async function handleCreateFolder(e: FormEvent) {
    e.preventDefault();
    const name = newFolderName.trim();
    if (!name) return;
    setNewFolderBusy(true);
    try {
      // ``listing.folder_id`` is the canonical id of the folder we're
      // currently inside; null means we're at the patient root and the
      // new folder will be a top-level child.
      const parentFolderId = listing?.folder_id ?? null;
      await patientTreeApi.createFolder(patientId, parentFolderId, name);
      setNewFolderName("");
      setNewFolderOpen(false);
      setReloadTick((t) => t + 1);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "create failed");
    } finally {
      setNewFolderBusy(false);
    }
  }

  return (
    <div
      style={{
        display: "flex",
        gap: "1rem",
        alignItems: "flex-start",
        marginTop: "1rem",
      }}
    >
      <FolderTree patientId={patientId} currentPath={currentPath} onNavigate={navigate} />

      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "0.5rem",
            flexWrap: "wrap",
          }}
        >
          <Breadcrumb
            segments={breadcrumb}
            onNavigate={navigate}
            onDropOnSegment={isOwner ? handleDropOnBreadcrumb : undefined}
            draggingId={draggingNodeId}
          />

          <div
            style={{
              display: "flex",
              gap: "0.35rem",
              alignItems: "center",
              flexWrap: "wrap",
              rowGap: "0.35rem",
            }}
          >
            {VIEW_MODE_KEYS.map(([mode, key]) => (
              <ViewModeButton
                key={mode}
                label={tFasc(key)}
                active={viewMode === mode}
                onClick={() => setViewMode(mode)}
              />
            ))}
            <span
              aria-hidden
              style={{
                width: 1,
                alignSelf: "stretch",
                background: "var(--bv-card-border)",
                margin: "0 0.15rem",
              }}
            />
            <span className="meta" style={{ fontSize: "0.78rem" }}>
              {tFasc("sortLabel")}
            </span>
            {SORT_MODE_KEYS.map(([mode, key]) => (
              <ViewModeButton
                key={mode}
                label={
                  sortMode === mode ? `${tFasc(key)} ${sortDir === "asc" ? "▲" : "▼"}` : tFasc(key)
                }
                active={sortMode === mode}
                onClick={() => {
                  // Click on the active field flips direction; click
                  // on a different field selects it without changing
                  // direction so the user's prior asc/desc preference
                  // is preserved across switches.
                  if (sortMode === mode) {
                    setSortDir((d) => (d === "asc" ? "desc" : "asc"));
                  } else {
                    setSortMode(mode);
                  }
                }}
                title={
                  sortMode === mode
                    ? tFasc("sortDirToggleTitle")
                    : sortDir === "asc"
                      ? tFasc("sortDirAscTitle")
                      : tFasc("sortDirDescTitle")
                }
              />
            ))}
            {isOwner && (
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.85rem" }}
                onClick={() => setNewFolderOpen((v) => !v)}
              >
                {newFolderOpen ? tFasc("newFolderCancel") : tFasc("newFolder")}
              </button>
            )}
            {listing?.folder_id && (
              <button
                type="button"
                className="ghost"
                style={{ fontSize: "0.85rem" }}
                onClick={() => setFolderExportId(listing.folder_id ?? null)}
                title={tExport("openFolderTitle")}
              >
                {tExport("openFolderLabel")}
              </button>
            )}
            <button
              type="button"
              className="ghost"
              style={{ fontSize: "0.85rem" }}
              onClick={() => setExportOpen(true)}
            >
              {tExport("openLabel")}
            </button>
          </div>
        </div>

        {newFolderOpen && isOwner && (
          <form
            onSubmit={handleCreateFolder}
            className="card"
            style={{
              display: "flex",
              gap: "0.5rem",
              alignItems: "center",
              marginTop: "0.5rem",
              marginBottom: "0.75rem",
            }}
          >
            <input
              // biome-ignore lint/a11y/noAutofocus: this input only mounts when the user clicks "Nuova cartella"; focus is the natural continuation of their click.
              autoFocus
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              placeholder={tFasc("newFolderNamePlaceholder")}
              style={{ flex: 1 }}
            />
            <button type="submit" disabled={newFolderBusy || !newFolderName.trim()}>
              {newFolderBusy ? tFasc("newFolderBusy") : tFasc("newFolderCreate")}
            </button>
          </form>
        )}

        {err && <p className="error">{err}</p>}

        {selectedNodes.size > 0 && (
          <div
            className="card"
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              padding: "0.5rem 0.75rem",
              marginTop: "0.5rem",
              background: "var(--bv-warning-soft)",
              borderColor: "color-mix(in srgb, var(--bv-warning) 22%, transparent)",
              color: "var(--bv-warning)",
              flexWrap: "wrap",
            }}
          >
            <strong>{tFasc("batch.selected", { n: selectedNodes.size })}</strong>
            <button
              type="button"
              className="ghost"
              onClick={selectAllVisible}
              style={{ fontSize: "0.85rem" }}
              title={tFasc("batch.selectAllTitle")}
            >
              {tFasc("batch.selectAll")}
            </button>
            <button
              type="button"
              className="ghost"
              onClick={clearSelection}
              style={{ fontSize: "0.85rem" }}
            >
              {tFasc("batch.clearSelection")}
            </button>
            {isOwner && (
              <>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => setMoveTargetOpen(true)}
                  style={{ marginLeft: "auto", fontSize: "0.85rem" }}
                  title={tFasc("batch.moveInFolderTitle")}
                >
                  {tFasc("batch.moveInFolder")}
                </button>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => setReassignOpen(true)}
                  style={{ fontSize: "0.85rem" }}
                  title={tFasc("batch.moveToOtherPatientTitle")}
                >
                  {tFasc("batch.moveToOtherPatient")}
                </button>
                <button
                  type="button"
                  onClick={handleBatchDelete}
                  style={{
                    background: "var(--bv-danger, #b42318)",
                    color: "#fff",
                    border: "1px solid var(--bv-danger, #b42318)",
                    fontSize: "0.85rem",
                  }}
                >
                  {tFasc("batch.deleteSelected")}
                </button>
              </>
            )}
          </div>
        )}

        {listing?.current_folder && <CurrentFolderHeader folder={listing.current_folder} />}

        {loading && !listing ? (
          <p className="meta">Loading...</p>
        ) : (
          <div style={{ marginTop: "0.5rem" }}>
            <ContentPane
              patientId={patientId}
              nodes={sortedNodes}
              viewMode={viewMode}
              onNavigate={navigate}
              onDeleteFolder={isOwner ? handleDeleteFolder : undefined}
              selectedIds={isOwner ? new Set(selectedNodes.keys()) : null}
              onToggleSelection={isOwner ? toggleSelection : undefined}
              onSetSelection={isOwner ? setSelection : undefined}
              onDropOnNode={isOwner ? handleDropOnNode : undefined}
              onDragStartId={setDraggingNodeId}
              onDragEnd={() => setDraggingNodeId(null)}
              onRenameItem={isOwner ? handleRenameItem : undefined}
              parentNavigation={parentNavigation}
              onDropOnParent={isOwner ? handleDropOnParent : undefined}
              onHardlinkItem={isOwner ? (n) => setHardlinkTarget(n) : undefined}
              onHardlinkOnNode={isOwner ? handleHardlinkOnNode : undefined}
              onHardlinkOnParent={isOwner ? handleHardlinkOnParent : undefined}
            />
          </div>
        )}
      </div>
      {reassignOpen && (
        <ReassignTargetModal
          excludePatientId={patientId}
          onPick={(target) => {
            setReassignOpen(false);
            void handleBatchReassign(target);
          }}
          onClose={() => setReassignOpen(false)}
        />
      )}
      {moveTargetOpen && (
        <MoveTargetFolderModal
          patientId={patientId}
          onPick={(folderId) => {
            setMoveTargetOpen(false);
            void handleBatchMove(folderId);
          }}
          onClose={() => setMoveTargetOpen(false)}
        />
      )}
      <EditFolderDialog
        open={editFolderTarget !== null}
        initialName={editFolderTarget?.name ?? ""}
        initialDescription={editFolderTarget?.description ?? null}
        initialCreatedAt={editFolderTarget?.created_at ?? null}
        busy={editFolderBusy}
        err={editFolderErr}
        onSubmit={handleEditFolderSubmit}
        onClose={() => {
          if (editFolderBusy) return;
          setEditFolderTarget(null);
          setEditFolderErr(null);
        }}
      />
      <ExportFascicoloDialog
        patientId={patientId}
        open={exportOpen}
        onClose={() => setExportOpen(false)}
      />
      <ExportFascicoloDialog
        patientId={patientId}
        open={folderExportId !== null}
        onClose={() => setFolderExportId(null)}
        folderId={folderExportId}
      />
      <HardlinkPickerModal
        open={hardlinkTarget !== null}
        patientId={patientId}
        excludeFolderId={listing?.folder_id ?? null}
        resourceName={hardlinkTarget?.name ?? ""}
        onClose={() => setHardlinkTarget(null)}
        onPick={handleHardlinkPickerPick}
      />
    </div>
  );
}

/**
 * Modal that lets the user pick a destination patient for the
 * cross-patient reassign flow. Search-only (we want this to be
 * deliberate; no Finder-style browse for an irreversible op). The
 * source patient is excluded from results so the user can't pick
 * their own fascicolo as the target by mistake.
 */
function ReassignTargetModal({
  excludePatientId,
  onPick,
  onClose,
}: {
  excludePatientId: string;
  onPick: (p: { id: string; display_name: string }) => void;
  onClose: () => void;
}) {
  const tFasc = useTranslations("fascicolo");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<
    { id: string; display_name: string; birth_date: string | null }[]
  >([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (q.trim().length < 2) {
      setResults([]);
      return;
    }
    setBusy(true);
    const handle = setTimeout(async () => {
      try {
        const { patientsApi } = await import("@/lib/api");
        const resp = await patientsApi.list({ q: q.trim(), scope: "all", limit: 20 });
        if (!cancelled) {
          setResults(
            resp.items
              .filter((p) => p.id !== excludePatientId)
              .map((p) => ({
                id: p.id,
                display_name: p.display_name,
                birth_date: p.birth_date,
              })),
          );
        }
      } finally {
        if (!cancelled) setBusy(false);
      }
    }, 220);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [q, excludePatientId]);

  return (
    <NativeDialog
      open
      onClose={onClose}
      ariaLabel={tFasc("batch.reassignDialogLabel")}
      className="bv-dialog"
    >
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          borderRadius: 10,
          boxShadow: "0 18px 42px rgba(0,0,0,0.35)",
          padding: "1rem 1.25rem",
          width: "min(540px, 95%)",
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>{tFasc("batch.reassignDialogTitle")}</h2>
        <p className="meta" style={{ marginTop: 0, fontSize: "0.85rem" }}>
          {tFasc("batch.reassignDialogHint")}
        </p>
        <input
          type="search"
          // biome-ignore lint/a11y/noAutofocus: modal opened by the user; focus on the search field is the modal's reason to exist.
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={tFasc("batch.reassignSearchPlaceholder")}
          style={{ width: "100%" }}
        />
        <div style={{ flex: 1, overflowY: "auto", marginTop: "0.5rem" }}>
          {busy && (
            <p className="meta" style={{ fontSize: "0.8rem" }}>
              …
            </p>
          )}
          {!busy && q.trim().length >= 2 && results.length === 0 && (
            <p className="meta" style={{ fontSize: "0.85rem" }}>
              {tFasc("batch.reassignNoMatch")}
            </p>
          )}
          {results.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => onPick({ id: p.id, display_name: p.display_name })}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "0.5rem 0.75rem",
                marginBottom: "0.25rem",
                background: "transparent",
                color: "inherit",
                border: "1px solid var(--bv-card-border, #e5e7eb)",
                borderRadius: 6,
                cursor: "pointer",
                font: "inherit",
              }}
            >
              <strong>{p.display_name}</strong>
              {p.birth_date && (
                <span className="meta" style={{ marginLeft: "0.5rem", fontSize: "0.8rem" }}>
                  {p.birth_date}
                </span>
              )}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "0.75rem" }}>
          <button type="button" className="ghost" onClick={onClose}>
            {tFasc("batch.clearSelection")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}

/**
 * Drive-style destination picker for the bulk Move action. The user
 * walks the patient's folder tree (breadcrumb + click-to-descend) and
 * commits the *current* folder as the destination via "Sposta qui".
 * The patient root is always selectable as a destination from the
 * breadcrumb root state.
 */
function MoveTargetFolderModal({
  patientId,
  onPick,
  onClose,
}: {
  patientId: string;
  onPick: (folderId: string | null) => void;
  onClose: () => void;
}) {
  const tFasc = useTranslations("fascicolo");
  const [path, setPath] = useState("/");
  const [breadcrumb, setBreadcrumb] = useState<{ name: string; path: string }[]>([]);
  const [folders, setFolders] = useState<TreeNode[]>([]);
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    patientTreeApi
      .tree(patientId, path)
      .then((listing) => {
        if (cancelled) return;
        setFolders(listing.nodes.filter((n) => n.type === "folder"));
        setBreadcrumb(listing.breadcrumb.map((b) => ({ name: b.name, path: b.path })));
        setCurrentFolderId(listing.folder_id);
      })
      .catch(() => {
        if (!cancelled) setFolders([]);
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [patientId, path]);

  return (
    <NativeDialog
      open
      onClose={onClose}
      ariaLabel={tFasc("batch.moveDialogLabel")}
      className="bv-dialog"
    >
      <div
        style={{
          background: "var(--bv-card-bg, #fff)",
          color: "var(--bv-fg, inherit)",
          border: "1px solid var(--bv-card-border, #d0d5dd)",
          borderRadius: 10,
          boxShadow: "0 18px 42px rgba(0,0,0,0.35)",
          padding: "1rem 1.25rem",
          width: "min(560px, 95%)",
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>{tFasc("batch.moveDialogTitle")}</h2>
        <p className="meta" style={{ marginTop: 0, fontSize: "0.85rem" }}>
          {tFasc("batch.moveDialogHint")}
        </p>
        <nav
          aria-label="breadcrumb"
          style={{
            fontSize: "0.85rem",
            margin: "0.5rem 0",
            display: "flex",
            flexWrap: "wrap",
            gap: "0.25rem",
            alignItems: "center",
          }}
        >
          <button
            type="button"
            className="ghost"
            onClick={() => setPath("/")}
            style={{ padding: "0.1rem 0.4rem" }}
          >
            {tFasc("batch.moveDialogRoot")}
          </button>
          {breadcrumb.map((b) => (
            <span
              key={b.path}
              style={{
                display: "inline-flex",
                gap: "0.25rem",
                alignItems: "center",
              }}
            >
              <span aria-hidden>/</span>
              <button
                type="button"
                className="ghost"
                onClick={() => setPath(b.path)}
                style={{ padding: "0.1rem 0.4rem" }}
              >
                {b.name}
              </button>
            </span>
          ))}
        </nav>
        <div style={{ flex: 1, overflowY: "auto" }}>
          {busy && (
            <p className="meta" style={{ fontSize: "0.8rem" }}>
              …
            </p>
          )}
          {path !== "/" && (
            <button
              type="button"
              onClick={() => {
                const segs = path.split("/").filter(Boolean);
                const parent = segs.length <= 1 ? "/" : `/${segs.slice(0, -1).join("/")}`;
                setPath(parent);
              }}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "0.4rem 0.6rem",
                marginBottom: "0.2rem",
                background: "transparent",
                color: "inherit",
                border: "1px dashed var(--bv-card-border, #e5e7eb)",
                borderRadius: 6,
                cursor: "pointer",
                font: "inherit",
                fontStyle: "italic",
              }}
              title={tFasc("batch.moveDialogParentTitle")}
            >
              {tFasc("batch.moveDialogParent")}
            </button>
          )}
          {!busy && folders.length === 0 && (
            <p className="meta" style={{ fontSize: "0.85rem" }}>
              {tFasc("batch.moveDialogNoSubfolders")}
            </p>
          )}
          {folders.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setPath(f.path ?? path + (path.endsWith("/") ? "" : "/") + f.name)}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "0.4rem 0.6rem",
                marginBottom: "0.2rem",
                background: "transparent",
                color: "inherit",
                border: "1px solid var(--bv-card-border, #e5e7eb)",
                borderRadius: 6,
                cursor: "pointer",
                font: "inherit",
              }}
            >
              📁 {f.name}
            </button>
          ))}
        </div>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            marginTop: "0.75rem",
            gap: "0.5rem",
          }}
        >
          <button type="button" className="ghost" onClick={onClose}>
            {tFasc("batch.clearSelection")}
          </button>
          <button
            type="button"
            onClick={() => onPick(currentFolderId)}
            title={
              currentFolderId
                ? tFasc("batch.moveDialogSubmitInside")
                : tFasc("batch.moveDialogSubmitRoot")
            }
          >
            {tFasc("batch.moveDialogSubmit")}
          </button>
        </div>
      </div>
    </NativeDialog>
  );
}

function ViewModeButton({
  label,
  active,
  onClick,
  title,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={active ? undefined : "ghost"}
      style={{
        fontSize: "0.85rem",
        padding: "0.3rem 0.7rem",
        background: active ? "#e96b1f" : undefined,
        color: active ? "#fff" : undefined,
        borderColor: active ? "#e96b1f" : undefined,
      }}
    >
      {label}
    </button>
  );
}
