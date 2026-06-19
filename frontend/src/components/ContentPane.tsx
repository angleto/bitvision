"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";

import FolderExportButton from "@/components/FolderExportButton";
import FolderGlimpse from "@/components/FolderGlimpse";
import LicenseBadge from "@/components/LicenseBadge";
import { useModal } from "@/components/ModalHost";
import PathologyThumbnail from "@/components/PathologyThumbnail";
import SendFolderButton from "@/components/SendFolderButton";
import SendStudyButton from "@/components/SendStudyButton";
import StudyExportButton from "@/components/StudyExportButton";
import {
  API_BASE_URL,
  type TreeNode,
  getStoredToken,
  patientTreeApi,
  patientsApi,
} from "@/lib/api";

export type ViewMode = "grid" | "list" | "timeline";

interface Props {
  patientId: string;
  nodes: TreeNode[];
  viewMode: ViewMode;
  onNavigate: (path: string) => void;
  /**
   * When provided, folder cards/rows expose a delete affordance that
   * invokes this callback (the parent owns the confirm flow + API call
   * + reload). Omit to hide the affordance for read-only viewers.
   */
  onDeleteFolder?: (node: TreeNode) => void;
  /**
   * Rename callback. Invoked for renamable rows (folder / study /
   * document). The parent owns the modal prompt + API call so the
   * pane stays a dumb renderer.
   */
  onRenameItem?: (node: TreeNode) => void;
  /**
   * Selection model. When ``selectedIds`` is non-null the pane renders
   * checkboxes on each row and emits toggle events through
   * ``onToggleSelection``; the parent is responsible for batch
   * actions (delete, move, share, ...). Pass ``null`` to render the
   * pane as read-only (no checkboxes).
   *
   * The list view also supports Finder-style modifier-aware clicks
   * and keyboard nav (arrow up/down, shift+arrow extends, cmd-click
   * toggles, shift-click range selects). The richer interactions go
   * through ``onSetSelection`` (replace) — toggle stays for the
   * checkbox path and grid-view single-row clicks.
   */
  selectedIds?: Set<string> | null;
  onToggleSelection?: (node: TreeNode) => void;
  onSetSelection?: (nodes: TreeNode[]) => void;
  /**
   * Drag-and-drop hook. The pane resolves the drop into a
   * ``(dragged, target)`` pair and lets the parent decide what
   * the drop means: dropping on a folder usually moves; dropping
   * a leaf on another leaf usually means "create a folder named
   * after the target and group both inside". Omit to disable
   * drag-drop entirely (read-only view).
   */
  onDropOnNode?: (dragged: TreeNode, target: TreeNode) => void;
  /**
   * Synthetic ".." parent-folder shortcut, shown at the top of the
   * grid / list when the current view is inside a folder. Click
   * navigates to ``parentPath``; a drop dispatches via
   * ``onDropOnParent`` with the resolved ``parentFolderId``
   * (``null`` = patient root). Omit at the root level.
   */
  parentNavigation?: {
    parentPath: string;
    parentFolderId: string | null;
  };
  onDropOnParent?: (dragged: TreeNode, parentFolderId: string | null) => void;
  /**
   * Optional hooks that mirror the in-grid drag state up to the parent
   * layout. Used by ``FascicoloDriveLayout`` to light up the breadcrumb
   * crumbs as drop targets while a card is being dragged. The internal
   * ``draggingId`` state still drives the in-grid visual; these are
   * fired on top so external listeners stay in sync.
   */
  onDragStartId?: (id: string) => void;
  onDragEnd?: () => void;
  /**
   * "Add to another folder" action. When provided, document cards
   * surface a chain-link button on the bottom toolbar; clicking it
   * opens the parent's folder picker so the user can create a
   * hardlink without losing the current containment. The same gesture
   * is also available via Cmd/Alt+drag on a folder target.
   */
  onHardlinkItem?: (node: TreeNode) => void;
  /**
   * Cmd/Alt+drag = copy/hardlink instead of move. The pane reports
   * the target plus the modifier flag; the parent decides whether to
   * issue ``tree/move`` (default) or ``add_item_to_folder`` (copy).
   */
  onHardlinkOnNode?: (dragged: TreeNode, target: TreeNode) => void;
  onHardlinkOnParent?: (dragged: TreeNode, parentFolderId: string | null) => void;
  onHardlinkOnSegmentId?: (dragged: TreeNode, segmentFolderId: string | null) => void;
}

/**
 * The center pane of the Drive-style fascicolo. Renders folders + leaf items
 * as a grid of cards, a list table, or a chronological timeline.
 *
 * Click semantics (all leaf URLs are patient-namespaced so cross-patient
 * navigation is structurally inexpressible — the legacy non-namespaced
 * forms still work via redirect, but the user never sees them):
 *   folder       -> onNavigate(path)
 *   study        -> /patients/<id>/studies/<target_id>
 *   document     -> /patients/<id>/documents/<doc_id>
 *   report       -> /patients/<id>/studies/<study_id> (report lives inside a study)
 *   annotation   -> /patients/<id>/studies/<study_id>
 *   consultation -> /patients/<id>/consultations/<consult_id>
 */
export default function ContentPane({
  patientId,
  nodes,
  viewMode,
  onNavigate,
  onDeleteFolder,
  onRenameItem,
  selectedIds,
  onToggleSelection,
  onSetSelection,
  onDropOnNode,
  parentNavigation,
  onDropOnParent,
  onDragStartId: onDragStartIdExt,
  onDragEnd: onDragEndExt,
  onHardlinkItem,
  onHardlinkOnNode,
  onHardlinkOnParent,
}: Props) {
  const router = useRouter();
  const modal = useModal();

  /**
   * URL a leaf node opens. ``null`` means the node is non-navigable
   * (folder — handled by ``onNavigate``) or a leaf with no target id.
   * The cards render an ``<a href={nodeHref(n)}>`` whenever this is
   * non-null so cmd-click / right-click / middle-click "open in new
   * tab" behave as the user expects from any other web app.
   */
  const nodeHref = (node: TreeNode): string | null => {
    if (node.type === "folder") return null;
    const id = node.target_id;
    if (!id) return null;
    switch (node.type) {
      case "study":
      case "report":
      case "annotation":
        // Reports / annotations resolve to the parent study (the
        // detail page hosts the relevant tab).
        return `/patients/${patientId}/studies/${id}`;
      case "document":
        return `/patients/${patientId}/documents/${id}`;
      case "consultation":
        return `/patients/${patientId}/consultations/${id}`;
    }
    return null;
  };

  const handleOpen = (node: TreeNode) => {
    if (node.type === "folder") {
      onNavigate(node.path);
      return;
    }
    const href = nodeHref(node);
    if (href) router.push(href);
  };

  const tFasc = useTranslations("fascicolo");

  // Per-item download state. Single id at a time keeps the UX
  // unambiguous: one ⬇ icon spinner, one in-flight blob fetch. Larger
  // ZIP-style folder downloads run via the export Job pipeline (see
  // ExportFascicoloDialog) and are tracked separately.
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const handleDownload = useCallback(
    async (n: TreeNode) => {
      // Studies use the async Job pipeline + ``StudyExportButton``
      // so they flow through ``useScopedJob`` instead of this
      // synchronous handler — see the bottom-toolbar branch below.
      if (n.type !== "document") return;
      const id = n.target_id ?? n.id;
      if (!id) return;
      setDownloadingId(n.id);
      try {
        await patientsApi.downloadDocument(id, n.name);
      } catch (err) {
        // The blob fetch path can fail for: missing binary (text-only
        // doc), permissions, or storage outage. Show the message in
        // an in-app modal (themed, escape-dismissible) instead of
        // ``window.alert`` (browser-blocking, "localhost says..."
        // prefix, no styling).
        await modal.alert({
          message: err instanceof Error ? err.message : String(err),
          variant: "danger",
        });
      } finally {
        setDownloadingId(null);
      }
    },
    [modal],
  );

  // Drag-drop plumbing: a single source per drag op, resolved back
  // to the full TreeNode at drop time so the parent doesn't have to
  // re-walk the listing. Stored as state (not just dataTransfer)
  // because ``onDragOver`` needs to know what's being dragged to
  // decide whether to highlight the target as a valid drop zone.
  // IMPORTANT: this hook (and ``handleDropOnNode`` below) must stay
  // ABOVE every conditional ``return`` in this component — moving the
  // empty-state branch back above them would re-introduce the
  // "Rendered fewer hooks than expected" runtime error users see when
  // navigating in/out of empty folders.
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const handleDropOnNode = useCallback(
    (target: TreeNode, draggedId: string | null) => {
      if (!onDropOnNode || !draggedId || draggedId === target.id) return;
      const dragged = nodes.find((n) => n.id === draggedId);
      if (!dragged) return;
      onDropOnNode(dragged, target);
    },
    [nodes, onDropOnNode],
  );

  // The empty-state shortcut still applies at the patient root, but
  // when we're inside a sub-folder the user must always be able to
  // see the ".." pseudo-row even if the folder has no children — so
  // we render the view shell instead of the bare empty card.
  if (nodes.length === 0 && !parentNavigation) {
    return (
      <div
        style={{
          padding: "3rem 1rem",
          textAlign: "center",
          border: "2px dashed var(--bv-card-border, #e5e7eb)",
          borderRadius: 10,
          color: "var(--bv-muted, #667085)",
        }}
      >
        <div style={{ marginBottom: "0.25rem" }}>
          <TypeIcon type="folder" size={32} />
        </div>
        <div>{tFasc("emptyFolderTitle")}</div>
        <div style={{ fontSize: "0.85rem", marginTop: "0.25rem" }}>{tFasc("emptyFolderHint")}</div>
      </div>
    );
  }

  const sharedSelectionProps = {
    selectedIds: selectedIds ?? null,
    onToggleSelection,
    onDropOnNode: onDropOnNode ? handleDropOnNode : undefined,
    onHardlinkOnNode,
    onHardlinkItem,
    draggingId,
    onDragStartId: (id: string) => {
      setDraggingId(id);
      onDragStartIdExt?.(id);
    },
    onDragEnd: () => {
      setDraggingId(null);
      onDragEndExt?.();
    },
  } as const;
  if (viewMode === "list") {
    return (
      <ListView
        patientId={patientId}
        nodes={nodes}
        nodeHref={nodeHref}
        onOpen={handleOpen}
        onDeleteFolder={onDeleteFolder}
        onRenameItem={onRenameItem}
        onDownload={handleDownload}
        downloadingId={downloadingId}
        {...sharedSelectionProps}
        onSetSelection={onSetSelection}
        parentNavigation={parentNavigation}
        onDropOnParent={onDropOnParent}
        onHardlinkOnParent={onHardlinkOnParent}
      />
    );
  }
  if (viewMode === "timeline") {
    return <TimelineView nodes={nodes} onOpen={handleOpen} onDeleteFolder={onDeleteFolder} />;
  }
  return (
    <GridView
      patientId={patientId}
      nodes={nodes}
      nodeHref={nodeHref}
      onOpen={handleOpen}
      onDeleteFolder={onDeleteFolder}
      onRenameItem={onRenameItem}
      onDownload={handleDownload}
      downloadingId={downloadingId}
      {...sharedSelectionProps}
      parentNavigation={parentNavigation}
      onDropOnParent={onDropOnParent}
      onHardlinkOnParent={onHardlinkOnParent}
    />
  );
}

// ---- Grid view ----

interface DragDropProps {
  onDropOnNode?: (target: TreeNode, draggedId: string | null) => void;
  onHardlinkOnNode?: (dragged: TreeNode, target: TreeNode) => void;
  onHardlinkItem?: (node: TreeNode) => void;
  draggingId?: string | null;
  onDragStartId?: (id: string) => void;
  onDragEnd?: () => void;
}

/** Kinds whose name is editable. Mirrors the backend whitelist. */
const RENAMABLE_KINDS = new Set<TreeNode["type"]>(["folder", "study", "document"]);

function GridView({
  patientId,
  nodes,
  nodeHref,
  onOpen,
  onDeleteFolder,
  onRenameItem,
  onDownload,
  downloadingId,
  selectedIds,
  onToggleSelection,
  onDropOnNode,
  onHardlinkOnNode,
  onHardlinkItem,
  draggingId,
  onDragStartId,
  onDragEnd,
  parentNavigation,
  onDropOnParent,
  onHardlinkOnParent,
}: {
  patientId: string;
  nodes: TreeNode[];
  /** Resolves a leaf node to its destination URL (study / document /
   *  consultation detail page). Returns null for folders and for
   *  leaves with no target id; callers render those as <button>. */
  nodeHref: (n: TreeNode) => string | null;
  onOpen: (n: TreeNode) => void;
  onDeleteFolder?: (n: TreeNode) => void;
  onRenameItem?: (n: TreeNode) => void;
  onDownload?: (n: TreeNode) => void;
  downloadingId?: string | null;
  selectedIds?: Set<string> | null;
  onToggleSelection?: (n: TreeNode) => void;
  parentNavigation?: { parentPath: string; parentFolderId: string | null };
  onDropOnParent?: (dragged: TreeNode, parentFolderId: string | null) => void;
  onHardlinkOnParent?: (dragged: TreeNode, parentFolderId: string | null) => void;
} & DragDropProps) {
  const showCheckbox = !!selectedIds && !!onToggleSelection;
  const dragEnabled = !!onDropOnNode;
  const tFasc = useTranslations("fascicolo");
  const tDocTypes = useTranslations("fascicolo.documentTypes");
  const [hoverId, setHoverId] = useState<string | null>(null);
  const PARENT_ROW_ID = "__parent__";
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))",
        gap: "0.75rem",
      }}
    >
      {parentNavigation && (
        <ParentShortcutCard
          isDragOver={dragEnabled && hoverId === PARENT_ROW_ID && draggingId !== null}
          onClick={() =>
            onOpen({
              id: PARENT_ROW_ID,
              type: "folder",
              name: "..",
              path: parentNavigation.parentPath,
            } as TreeNode)
          }
          onDragOver={(e) => {
            if (!dragEnabled || draggingId === null) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            setHoverId(PARENT_ROW_ID);
          }}
          onDragLeave={() => {
            if (hoverId === PARENT_ROW_ID) setHoverId(null);
          }}
          onDrop={(e) => {
            if (!dragEnabled) return;
            e.preventDefault();
            const id = e.dataTransfer.getData("text/plain") || draggingId || null;
            setHoverId(null);
            if (!id) return;
            const dragged = nodes.find((n) => n.id === id);
            if (!dragged) return;
            // Cmd (mac) / Alt (linux+windows) held = hardlink (copy);
            // plain drop = move. Mirrors Finder semantics so users
            // who learn the gesture once carry it across the app.
            const copyMode = e.altKey || e.metaKey;
            if (copyMode && onHardlinkOnParent) {
              onHardlinkOnParent(dragged, parentNavigation.parentFolderId);
            } else if (onDropOnParent) {
              onDropOnParent(dragged, parentNavigation.parentFolderId);
            }
          }}
        />
      )}
      {nodes.map((n) => {
        const isDragOver = dragEnabled && hoverId === n.id && draggingId !== n.id;
        const canDelete = n.type === "folder" && !!onDeleteFolder;
        const canRename = RENAMABLE_KINDS.has(n.type) && !!onRenameItem;
        // Studies render their own ``StudyExportButton`` which owns
        // the queued/running/ready/failed state via ``useScopedJob``.
        // Documents still use the synchronous ``DownloadButton`` +
        // ``onDownload`` round-trip.
        const canDownload = n.type === "document" && !!onDownload;
        const isStudy = n.type === "study";
        const isFolder = n.type === "folder";
        const studyTargetId = isStudy ? (n.target_id ?? n.id) : null;
        // Selection checkbox + action icons (rename / delete / download)
        // all live in the card foot. Keeping them on a single strip
        // avoids a top bar that would clip the folder peek-tile stack
        // and frees the thumbnail area for the primary "open"
        // affordance.
        const hasBottomToolbar =
          showCheckbox || canDelete || canRename || canDownload || isStudy || isFolder;
        return (
          <div
            key={n.id}
            // ``data-item-id`` is read by the fascicolo-level hash effect
            // when the user comes back from a detail page (via either the
            // browser back-button or the in-page "← folder" link, which
            // appends ``#item-<id>``). The matching card is scrolled
            // into view and pulsed with ``.bv-highlight-flash`` so the
            // user sees where they were.
            data-item-id={n.target_id ?? n.id}
            draggable={dragEnabled}
            onDragStart={(e) => {
              if (!dragEnabled) return;
              e.dataTransfer.setData("text/plain", n.id);
              e.dataTransfer.effectAllowed = "move";
              onDragStartId?.(n.id);
            }}
            onDragEnd={() => onDragEnd?.()}
            onDragOver={(e) => {
              if (!dragEnabled || draggingId === null || draggingId === n.id) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              setHoverId(n.id);
            }}
            onDragLeave={() => {
              if (hoverId === n.id) setHoverId(null);
            }}
            onDrop={(e) => {
              if (!dragEnabled) return;
              e.preventDefault();
              const id = e.dataTransfer.getData("text/plain") || draggingId || null;
              setHoverId(null);
              if (!id || id === n.id) return;
              const copyMode = e.altKey || e.metaKey;
              if (copyMode && onHardlinkOnNode) {
                const dragged = nodes.find((x) => x.id === id);
                if (dragged) onHardlinkOnNode(dragged, n);
              } else {
                onDropOnNode?.(n, id);
              }
            }}
            className={`card${n.type === "folder" ? " folder-grid-card" : ""}`}
            style={{
              position: "relative",
              outline: isDragOver ? "2px solid #e96b1f" : undefined,
              borderRadius: 8,
              padding: 0,
              margin: 0,
              // The folder hover preview is positioned ``top: 100%`` and
              // would be clipped by ``overflow: hidden``. Cards that
              // never expand (non-folders) keep clipping so their drag
              // outline doesn't bleed.
              overflow: n.type === "folder" ? "visible" : "hidden",
              display: "flex",
              flexDirection: "column",
            }}
          >
            {(() => {
              // Navigable leaves (study / document / consultation /
              // report / annotation) render as <Link> so cmd-click,
              // middle-click, right-click "open in new tab" and the
              // URL preview on hover all work — the previous <button>
              // approach forfeited those affordances. Folders stay as
              // <button> because their click is in-page navigation
              // (onNavigate updates the path query string).
              const href = nodeHref(n);
              const cardInnerStyle: React.CSSProperties = {
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: "0.5rem",
                padding: "0.75rem",
                background: "transparent",
                border: "none",
                color: "inherit",
                cursor: "pointer",
                textAlign: "center",
                margin: 0,
                width: "100%",
                textDecoration: "none",
              };
              const Wrapper = href ? Link : "button";
              const wrapperProps = href
                ? ({ href, style: cardInnerStyle, title: n.name } as const)
                : {
                    type: "button" as const,
                    onClick: () => onOpen(n),
                    style: cardInnerStyle,
                    title: n.name,
                  };
              return (
                // biome-ignore lint/suspicious/noExplicitAny: union over Link vs button
                <Wrapper {...(wrapperProps as any)}>
                  {n.type === "study" && n.thumbnail_series_id ? (
                    <StudyThumbnail
                      seriesId={n.thumbnail_series_id}
                      modality={n.modality ?? null}
                    />
                  ) : n.type === "pathology_slide" ? (
                    <PathologyThumbnail slideId={n.target_id ?? n.id} stain={n.stain ?? null} />
                  ) : n.type === "folder" ? (
                    <FolderGlimpse
                      preview={n.preview ?? null}
                      previewKinds={n.preview_kinds ?? null}
                      patientId={patientId}
                    />
                  ) : n.type === "document" &&
                    supportsDocumentThumbnail(n.mime_type ?? null, n.name) ? (
                    <DocumentThumbnail
                      patientId={patientId}
                      docId={n.target_id ?? n.id}
                      mimeType={n.mime_type ?? null}
                      filename={n.name}
                    />
                  ) : (
                    <div
                      style={{
                        position: "relative",
                        padding: "0.6rem 0",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        height: 110,
                      }}
                    >
                      <TypeIcon type={n.type} size={56} />
                      {n.type === "document" && (
                        <FileTypeBadge mime={n.mime_type ?? null} filename={n.name} />
                      )}
                    </div>
                  )}
                  <div
                    style={{
                      fontSize: "0.85rem",
                      fontWeight: 500,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                      width: "100%",
                    }}
                  >
                    {n.name}
                  </div>
                  {/* Two timestamps stacked: primary acquisition / clinical
              date (for studies/docs the imaging or document date,
              with backend fallback to created_at), secondary
              "ultima modifica". The icons telegraph which is which
              without crowding the card. */}
                  <DateStamps
                    date={n.date}
                    updatedAt={n.updated_at}
                    kindFallback={tFasc(`kind.${n.type}`)}
                  />
                  {/* Item counts intentionally not shown here: the per-kind
              breakdown lives only in the hover preview overlay
              (FolderHoverDetails). */}
                  {n.type === "document" && n.document_type && (
                    <div
                      className="badge"
                      style={{
                        fontSize: "0.65rem",
                        padding: "1px 6px",
                        background: "var(--bv-card-bg, #fff)",
                        color: "var(--bv-fg-soft, #475569)",
                        border: "1px solid var(--bv-card-border, #e5e7eb)",
                        borderRadius: 999,
                        maxWidth: "100%",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={tDocTypes(n.document_type)}
                    >
                      {tDocTypes(n.document_type)}
                    </div>
                  )}
                  {/* Study + series cards: surface the radiology-relevant
              count summary under the date. The backend computes
              ``series_count`` / ``instance_count`` once per request
              (see patient_tree._attach_study_thumbnails). */}
                  {n.type === "study" &&
                    (typeof n.series_count === "number" ||
                      typeof n.instance_count === "number") && (
                      <div className="meta" style={{ fontSize: "0.7rem", opacity: 0.85 }}>
                        {typeof n.series_count === "number" &&
                          `${n.series_count} ${n.series_count === 1 ? "series" : "series"}`}
                        {typeof n.series_count === "number" &&
                          typeof n.instance_count === "number" &&
                          " · "}
                        {typeof n.instance_count === "number" &&
                          `${n.instance_count} ${n.instance_count === 1 ? "image" : "images"}`}
                      </div>
                    )}
                  {/* OpenData public-dataset chip: only renders when the
                      backend populated license_spdx on the study row
                      (i.e. tier=t4). Compact variant keeps the card
                      density; click opens the same citation dialog as
                      the StudyDetailContent header. */}
                  {n.type === "study" && n.license_spdx && (
                    <div style={{ marginTop: 4 }}>
                      <LicenseBadge
                        study={{
                          license_spdx: n.license_spdx ?? null,
                          license_url: n.license_url ?? null,
                          citation_text: n.citation_text ?? null,
                          citation_required: n.citation_required ?? false,
                          source_collection: n.source_collection ?? null,
                        }}
                        variant="compact"
                      />
                    </div>
                  )}
                  {/* Pathology slide meta line: stain + magnification +
                      source_format chips, plus the LicenseBadge when the
                      slide carries OpenData provenance. Step 1 surfacing
                      only — the tile-pyramid viewer is Step 2. */}
                  {n.type === "pathology_slide" && (
                    <div
                      style={{
                        marginTop: 4,
                        display: "flex",
                        flexWrap: "wrap",
                        gap: 4,
                        alignItems: "center",
                        fontSize: "0.7rem",
                      }}
                    >
                      {n.stain && <span className="badge">{n.stain}</span>}
                      {typeof n.magnification === "number" && (
                        <span className="badge">{n.magnification}x</span>
                      )}
                      {n.source_format && (
                        <span
                          className="badge"
                          style={{ opacity: 0.7, fontFamily: "ui-monospace, monospace" }}
                        >
                          {n.source_format}
                        </span>
                      )}
                      {n.license_spdx && (
                        <LicenseBadge
                          study={{
                            license_spdx: n.license_spdx ?? null,
                            license_url: n.license_url ?? null,
                            citation_text: n.citation_text ?? null,
                            citation_required: n.citation_required ?? false,
                            source_collection: n.source_collection ?? null,
                          }}
                          variant="compact"
                        />
                      )}
                    </div>
                  )}
                  {n.type === "series" && typeof n.instance_count === "number" && (
                    <div className="meta" style={{ fontSize: "0.7rem", opacity: 0.85 }}>
                      {n.instance_count} {n.instance_count === 1 ? "image" : "images"}
                    </div>
                  )}
                </Wrapper>
              );
            })()}
            {hasBottomToolbar && (
              <div
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => e.stopPropagation()}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 4,
                  padding: "4px 6px",
                  minHeight: 28,
                  borderTop: "1px solid var(--bv-card-border, #e5e7eb)",
                  background: "var(--bv-card-bg, #fff)",
                  // Keep these icons clickable above the folder
                  // peek-tile fan-out (z-index 10-12).
                  position: "relative",
                  zIndex: 25,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  {showCheckbox && (
                    <label
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        cursor: "pointer",
                        padding: "0 2px",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds?.has(n.id) ?? false}
                        onChange={() => onToggleSelection?.(n)}
                        aria-label={tFasc("list.selectRow", { name: n.name })}
                      />
                    </label>
                  )}
                  {n.type === "document" &&
                    typeof n.folder_count === "number" &&
                    n.folder_count >= 2 && <HardlinkBadge count={n.folder_count} />}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  {n.type === "document" && onHardlinkItem && (
                    <HardlinkAddButton onClick={() => onHardlinkItem(n)} />
                  )}
                  {canDownload && (
                    <DownloadButton busy={downloadingId === n.id} onClick={() => onDownload?.(n)} />
                  )}
                  {isStudy && studyTargetId && (
                    <StudyExportButton studyId={studyTargetId} studyLabel={n.name} variant="icon" />
                  )}
                  {isStudy && studyTargetId && (
                    <SendStudyButton
                      studyId={studyTargetId}
                      patientId={patientId}
                      studyLabel={n.name}
                      variant="icon"
                    />
                  )}
                  {isFolder && (
                    <FolderExportButton
                      folderId={n.id}
                      patientId={patientId}
                      folderLabel={n.name}
                    />
                  )}
                  {isFolder && (
                    <SendFolderButton folderId={n.id} patientId={patientId} folderLabel={n.name} />
                  )}
                  {canRename && <RenameButton onClick={() => onRenameItem?.(n)} />}
                  {canDelete && <FolderDeleteButton onClick={() => onDeleteFolder?.(n)} />}
                </div>
              </div>
            )}
            {n.type === "folder" && (
              <FolderHoverDetails
                description={n.description ?? null}
                // Prefer the recursive aggregate (counts everything
                // reachable through nested sub-folders) when the
                // backend populated it. Falls back to the direct-
                // child counts for nodes that haven't been re-
                // walked yet.
                kindCounts={n.recursive_kind_counts ?? n.kind_counts ?? null}
                pairedStudyReport={n.paired_study_report_count ?? null}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/**
 * Tooltip-style overlay shown under a folder card on hover (desktop)
 * with the optional description plus a per-kind breakdown of the
 * folder's contents. Always rendered for folder cards; visibility is
 * controlled by ``.folder-grid-card:hover`` in globals.css. On touch
 * devices the same content is reachable via the edit-metadata dialog
 * (``@media (hover: none)`` keeps this overlay hidden there).
 */
function FolderHoverDetails({
  description,
  kindCounts,
  pairedStudyReport,
}: {
  description: string | null;
  kindCounts: Record<string, number> | null;
  pairedStudyReport?: number | null;
}) {
  const t = useTranslations("fascicolo.hover");
  const tCounts = useTranslations("fascicolo.hover.kindCounts");
  // Stable display order so the line reads consistently regardless of
  // map iteration order. Kinds not in this list (future ones) get
  // appended at the end. ``folder`` is the recursive nested-folder
  // count surfaced by the backend's recursive_kind_counts.
  const KIND_ORDER = [
    "study",
    "series",
    "report",
    "document",
    "consultation",
    "annotation",
    "folder",
  ] as const;
  const entries = Object.entries(kindCounts ?? {}).filter(([, n]) => n > 0);
  const orderedEntries = [
    ...KIND_ORDER.flatMap((k) => {
      const found = entries.find(([kk]) => kk === k);
      return found ? [found] : [];
    }),
    ...entries.filter(([k]) => !KIND_ORDER.includes(k as (typeof KIND_ORDER)[number])),
  ];
  if (!description && orderedEntries.length === 0) return null;
  return (
    <div className="folder-hover-details" role="tooltip" aria-hidden>
      {description && (
        <div className="folder-hover-description">
          <ReactMarkdown
            components={{
              // Compact rendering: paragraph margins shrink to a few
              // px so multi-line description stays in the hover-card
              // visual budget; bold/italic/lists/links keep default
              // styling. ``a`` opens in a new tab to avoid yanking
              // the user out of the fascicolo grid.
              p: ({ children }) => <p style={{ margin: "0 0 4px" }}>{children}</p>,
              ul: ({ children }) => (
                <ul style={{ margin: "0 0 4px", paddingLeft: "1.2em" }}>{children}</ul>
              ),
              ol: ({ children }) => (
                <ol style={{ margin: "0 0 4px", paddingLeft: "1.2em" }}>{children}</ol>
              ),
              a: ({ href, children }) => (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ),
            }}
          >
            {description}
          </ReactMarkdown>
        </div>
      )}
      {pairedStudyReport && pairedStudyReport > 0 && (
        <p
          style={{
            margin: "0 0 4px",
            fontSize: "0.78rem",
            color: "var(--bv-accent, #e96b1f)",
            fontWeight: 500,
          }}
        >
          {tCounts("pairedStudyReport", { n: pairedStudyReport })}
        </p>
      )}
      {orderedEntries.length > 0 && (
        <ul>
          {orderedEntries.map(([kind, n]) => (
            <li key={kind}>
              {KIND_ORDER.includes(kind as (typeof KIND_ORDER)[number])
                ? tCounts(kind, { n })
                : `${n} ${kind}`}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * ".." pseudo-card rendered at the top of the grid view when we're
 * inside a sub-folder. Click navigates one level up; a drop dispatches
 * the parent's ``onDropOnParent`` so the user can drag items out of
 * the current folder without first navigating up.
 */
function ParentShortcutCard({
  isDragOver,
  onClick,
  onDragOver,
  onDragLeave,
  onDrop,
}: {
  isDragOver: boolean;
  onClick: () => void;
  onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave: () => void;
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void;
}) {
  const tFasc = useTranslations("fascicolo");
  return (
    <div
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      style={{
        position: "relative",
        outline: isDragOver ? "2px solid #e96b1f" : undefined,
        borderRadius: 8,
      }}
    >
      <button
        type="button"
        onClick={onClick}
        title={tFasc("list.goParent")}
        className="card"
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "0.5rem",
          padding: "0.75rem",
          background: "var(--bv-card-bg, #fff)",
          border: "1px dashed var(--bv-card-border, #e5e7eb)",
          color: "inherit",
          cursor: "pointer",
          textAlign: "center",
          margin: 0,
          width: "100%",
        }}
      >
        <div
          style={{
            padding: "0.6rem 0",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: 110,
          }}
        >
          <TypeIcon type="folder" size={56} />
        </div>
        <div style={{ fontSize: "0.85rem", fontWeight: 500 }}>..</div>
        <div className="meta" style={{ fontSize: "0.75rem" }}>
          {tFasc("list.parentFolder")}
        </div>
      </button>
    </div>
  );
}

/**
 * Chain-link badge surfaced on document cards when the document is
 * hardlinked from N >= 2 folders. Tells the user the same file is
 * reachable from multiple cartelle and is NOT a duplicate. The
 * tooltip carries the count; the icon is a Lucide-style chain glyph
 * inlined as SVG so we don't pull a fresh dep just for one badge.
 */
function HardlinkBadge({ count }: { count: number }) {
  const tFasc = useTranslations("fascicolo");
  return (
    <span
      title={tFasc("list.hardlinkTooltip", { count })}
      aria-label={tFasc("list.hardlinkTooltip", { count })}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 2,
        padding: "1px 6px",
        borderRadius: 999,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        background: "var(--bv-card-bg, #fff)",
        color: "var(--bv-fg, #0f172a)",
        fontSize: "0.65rem",
        fontFamily: "ui-monospace, monospace",
        lineHeight: 1,
        pointerEvents: "auto",
      }}
    >
      <svg
        width="10"
        height="10"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5" />
        <path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5" />
      </svg>
      {count}
    </span>
  );
}

/**
 * Card toolbar button that triggers the folder picker so the user can
 * link the document to an additional folder (hardlink). Distinct
 * affordance from "move" so the gesture is discoverable without the
 * Cmd/Alt+drag trick.
 */
function HardlinkAddButton({ onClick }: { onClick: () => void }) {
  const tFasc = useTranslations("fascicolo");
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={tFasc("list.hardlinkAdd")}
      aria-label={tFasc("list.hardlinkAdd")}
      style={{
        width: 22,
        height: 22,
        padding: 0,
        borderRadius: 6,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        background: "var(--bv-card-bg, #fff)",
        color: "var(--bv-fg, #0f172a)",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5" />
        <path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5" />
      </svg>
    </button>
  );
}

/**
 * Compact "x" overlay shown on folder cards when the parent supplies a
 * deletion handler. Stops propagation so the click doesn't also navigate
 * into the folder.
 */
function FolderDeleteButton({ onClick }: { onClick: () => void }) {
  const tFasc = useTranslations("fascicolo");
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={tFasc("list.deleteFolder")}
      aria-label={tFasc("list.deleteFolder")}
      style={{
        width: 22,
        height: 22,
        padding: 0,
        borderRadius: 6,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        background: "var(--bv-card-bg, #fff)",
        color: "var(--bv-danger, #b42318)",
        fontSize: "0.85rem",
        lineHeight: 1,
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      ×
    </button>
  );
}

function DownloadButton({
  onClick,
  busy,
}: {
  onClick: () => void;
  busy: boolean;
}) {
  const tFasc = useTranslations("fascicolo");
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      disabled={busy}
      title={tFasc("list.download")}
      aria-label={tFasc("list.download")}
      style={{
        width: 22,
        height: 22,
        padding: 0,
        borderRadius: 6,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        background: "var(--bv-card-bg, #fff)",
        color: "var(--bv-fg, #0f172a)",
        fontSize: "0.78rem",
        lineHeight: 1,
        cursor: busy ? "wait" : "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        opacity: busy ? 0.55 : 1,
      }}
    >
      {busy ? "…" : "⬇"}
    </button>
  );
}

function RenameButton({ onClick }: { onClick: () => void }) {
  const tFasc = useTranslations("fascicolo");
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={tFasc("list.rename")}
      aria-label={tFasc("list.rename")}
      style={{
        width: 22,
        height: 22,
        padding: 0,
        borderRadius: 6,
        border: "1px solid var(--bv-card-border, #e5e7eb)",
        background: "var(--bv-card-bg, #fff)",
        color: "var(--bv-fg, #0f172a)",
        fontSize: "0.78rem",
        lineHeight: 1,
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      ✎
    </button>
  );
}

/**
 * Cover image for a study card. Fetches the middle slice of the
 * preferred series at thumbnail size (256px on the long edge); falls
 * back to a "DICOM" placeholder if the request fails (e.g. structured
 * report series with no pixel data).
 */
function StudyThumbnail({
  seriesId,
  modality,
}: {
  seriesId: string;
  modality: string | null;
}) {
  const tThumb = useTranslations("thumbnail");
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(`${API_BASE_URL}/api/series/${seriesId}/thumbnail?max_side=256`, {
      credentials: "include",
      headers,
      signal: ac.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        setSrc(URL.createObjectURL(blob));
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [seriesId]);

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: 110,
        background: "#0a0d14",
        borderRadius: 6,
        overflow: "hidden",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {src ? (
        <img
          src={src}
          alt={tThumb("studyAlt")}
          draggable={false}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
            display: "block",
          }}
        />
      ) : failed ? (
        <TypeIcon type="study" size={48} />
      ) : (
        <div style={{ color: "#475569", fontSize: "0.75rem" }}>{tThumb("loading")}</div>
      )}
      {modality && (
        <span
          style={{
            position: "absolute",
            top: 4,
            left: 4,
            background: "rgba(0,0,0,0.65)",
            color: "#e6ecf3",
            fontSize: "0.65rem",
            padding: "1px 5px",
            borderRadius: 3,
            fontFamily: "ui-monospace, monospace",
            letterSpacing: "0.04em",
          }}
        >
          {modality}
        </span>
      )}
      <span
        style={{
          position: "absolute",
          bottom: 4,
          right: 4,
          background: "rgba(233,107,31,0.85)",
          color: "#fff",
          fontSize: "0.6rem",
          padding: "1px 5px",
          borderRadius: 3,
          letterSpacing: "0.05em",
          fontWeight: 600,
        }}
      >
        DICOM
      </span>
    </div>
  );
}

// ``PathologyThumbnail`` was extracted to its own component
// (``@/components/PathologyThumbnail``) so the public pathology library
// grid (/pathology) can reuse it without duplicating the fetch / badge
// logic. Imported at the top of this file.

/**
 * Cover image for a document card. Fetches the backend-rendered JPEG
 * thumbnail (PDF first page or downscaled raster image) and overlays
 * a MIME-derived badge ("PDF", "JPG", ...) so the user can tell file
 * types at a glance, the way DICOM studies are tagged with the
 * "DICOM" badge above. On 404 / fetch failure we fall back to the
 * generic document icon plus the same badge.
 */
function DocumentThumbnail({
  patientId,
  docId,
  mimeType,
  filename,
}: {
  patientId: string;
  docId: string;
  mimeType: string | null;
  filename: string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(`${API_BASE_URL}/api/patients/${patientId}/documents/${docId}/thumbnail?max_side=256`, {
      credentials: "include",
      headers,
      signal: ac.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        setSrc(URL.createObjectURL(blob));
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [patientId, docId]);

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: 110,
        background: "#f4f4f5",
        borderRadius: 6,
        overflow: "hidden",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {src ? (
        <img
          src={src}
          alt={filename}
          draggable={false}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
            display: "block",
          }}
        />
      ) : failed ? (
        <TypeIcon type="document" size={48} />
      ) : (
        <div style={{ color: "#475569", fontSize: "0.75rem" }}>…</div>
      )}
      <FileTypeBadge mime={mimeType} filename={filename} />
    </div>
  );
}

/**
 * Lower-right "PDF" / "JPG" / "PNG" / ... badge for non-DICOM cards.
 * Mirrors the position and styling of the "DICOM" badge that flags
 * study cards so file kind is readable at a glance.
 */
function FileTypeBadge({
  mime,
  filename,
}: {
  mime: string | null;
  filename: string;
}) {
  const label = fileTypeBadgeText(mime, filename);
  if (!label) return null;
  return (
    <span
      style={{
        position: "absolute",
        bottom: 4,
        right: 4,
        background: "rgba(71,85,105,0.85)",
        color: "#fff",
        fontSize: "0.6rem",
        padding: "1px 5px",
        borderRadius: 3,
        letterSpacing: "0.05em",
        fontWeight: 600,
      }}
    >
      {label}
    </span>
  );
}

const _MIME_TO_BADGE: Record<string, string> = {
  "application/pdf": "PDF",
  "image/png": "PNG",
  "image/jpeg": "JPG",
  "image/webp": "WEBP",
  "image/gif": "GIF",
  "image/tiff": "TIFF",
  "image/bmp": "BMP",
  "image/svg+xml": "SVG",
  "text/plain": "TXT",
  "text/markdown": "MD",
  "application/json": "JSON",
  "application/xml": "XML",
};

function fileTypeBadgeText(mime: string | null, filename: string): string | null {
  const ct = (mime ?? "").toLowerCase();
  if (ct in _MIME_TO_BADGE) return _MIME_TO_BADGE[ct];
  if (ct.startsWith("image/")) {
    // Generic fallback for image/<exotic>: surface the subtype upper-cased.
    return ct.split("/", 2)[1].toUpperCase().slice(0, 6);
  }
  const ext = filename.toLowerCase().split(".").pop() ?? "";
  if (!ext || ext === filename.toLowerCase()) return null;
  switch (ext) {
    case "pdf":
      return "PDF";
    case "png":
      return "PNG";
    case "jpg":
    case "jpeg":
      return "JPG";
    case "webp":
      return "WEBP";
    case "gif":
      return "GIF";
    case "tif":
    case "tiff":
      return "TIFF";
    case "txt":
      return "TXT";
    case "md":
    case "markdown":
      return "MD";
    case "json":
      return "JSON";
    case "xml":
      return "XML";
    default:
      return ext.toUpperCase().slice(0, 5);
  }
}

function supportsDocumentThumbnail(mime: string | null, filename: string): boolean {
  // Mirrors ``services.document_thumbnails.is_supported_thumbnail_mime``
  // so the frontend doesn't fire a request that's guaranteed to 404.
  const ct = (mime ?? "").toLowerCase();
  if (ct === "application/pdf") return true;
  if (ct.startsWith("image/")) return true;
  const ext = filename.toLowerCase().split(".").pop() ?? "";
  return ["pdf", "png", "jpg", "jpeg", "webp", "gif", "tif", "tiff"].includes(ext);
}

// ---- List view ----

function ListView({
  patientId,
  nodes,
  nodeHref,
  onOpen,
  onDeleteFolder,
  onRenameItem,
  onDownload,
  downloadingId,
  selectedIds,
  onToggleSelection,
  onSetSelection,
  onDropOnNode,
  onHardlinkOnNode: _onHardlinkOnNode,
  onHardlinkItem: _onHardlinkItem,
  draggingId,
  onDragStartId,
  onDragEnd,
  parentNavigation,
  onDropOnParent,
  onHardlinkOnParent: _onHardlinkOnParent,
}: {
  patientId: string;
  nodes: TreeNode[];
  nodeHref: (n: TreeNode) => string | null;
  onOpen: (n: TreeNode) => void;
  onDeleteFolder?: (n: TreeNode) => void;
  onRenameItem?: (n: TreeNode) => void;
  onDownload?: (n: TreeNode) => void;
  downloadingId?: string | null;
  selectedIds?: Set<string> | null;
  onToggleSelection?: (n: TreeNode) => void;
  onSetSelection?: (ns: TreeNode[]) => void;
  parentNavigation?: { parentPath: string; parentFolderId: string | null };
  onDropOnParent?: (dragged: TreeNode, parentFolderId: string | null) => void;
  onHardlinkOnParent?: (dragged: TreeNode, parentFolderId: string | null) => void;
} & DragDropProps) {
  // Hardlink gestures live in the grid view today; the list view
  // keeps the move-only DnD until a separate UX pass for tabular
  // gestures. Variables prefixed with ``_`` to silence unused
  // checker.
  void _onHardlinkOnNode;
  void _onHardlinkItem;
  void _onHardlinkOnParent;
  const tFasc = useTranslations("fascicolo");
  const showActions = !!onDeleteFolder || !!onRenameItem || !!onDownload;
  const showCheckbox = !!selectedIds && !!onToggleSelection;
  const dragEnabled = !!onDropOnNode;
  const [hoverId, setHoverId] = useState<string | null>(null);
  const PARENT_ROW_ID = "__parent__";

  // Inline tree expansion: every folder row carries a chevron caret;
  // toggling it materialises that folder's children inline (indented
  // under their parent) rather than navigating into the folder. The
  // initial state is "everything expanded" so the user sees the
  // hierarchy up-front without clicking through. Children are fetched
  // lazily but in parallel on first render.
  const initialExpanded = useMemo(
    () => new Set(nodes.filter((n) => n.type === "folder").map((n) => n.id)),
    [nodes],
  );
  const [expanded, setExpanded] = useState<Set<string>>(initialExpanded);
  const [childrenByFolderId, setChildrenByFolderId] = useState<Map<string, TreeNode[]>>(new Map());

  // Reset expand state when the listing scope changes (parent
  // navigated into a different folder). Without this the previous
  // folder's expanded ids would leak into the new view.
  useEffect(() => {
    setExpanded(initialExpanded);
  }, [initialExpanded]);

  // Lazy-fetch children for every expanded folder that we don't have
  // cached yet. Runs in parallel; failures are swallowed so a single
  // 403 doesn't break the whole listing.
  useEffect(() => {
    const todo = Array.from(expanded).filter((id) => !childrenByFolderId.has(id));
    if (todo.length === 0) return;
    let cancelled = false;
    const findFolder = (id: string): TreeNode | undefined => {
      // Top-level first, then any already-loaded children.
      const top = nodes.find((n) => n.id === id);
      if (top) return top;
      for (const list of childrenByFolderId.values()) {
        const hit = list.find((n) => n.id === id);
        if (hit) return hit;
      }
      return undefined;
    };
    Promise.all(
      todo.map(async (id) => {
        const folder = findFolder(id);
        if (!folder || folder.type !== "folder" || !folder.path) return null;
        try {
          const listing = await patientTreeApi.tree(patientId, folder.path);
          return [id, listing.nodes] as const;
        } catch {
          return [id, [] as TreeNode[]] as const;
        }
      }),
    ).then((pairs) => {
      if (cancelled) return;
      setChildrenByFolderId((prev) => {
        const next = new Map(prev);
        for (const pair of pairs) {
          if (pair) next.set(pair[0], pair[1]);
        }
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [expanded, childrenByFolderId, nodes, patientId]);

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Flatten the listing into a depth-tagged sequence for rendering.
  // Folders that are expanded have their children spliced in
  // immediately after the parent row, recursively, with an integer
  // ``depth`` driving the left-pad of the name cell.
  type DisplayRow = { node: TreeNode; depth: number };
  const displayRows = useMemo<DisplayRow[]>(() => {
    const out: DisplayRow[] = [];
    const visit = (list: TreeNode[], depth: number) => {
      for (const n of list) {
        out.push({ node: n, depth });
        if (n.type === "folder" && expanded.has(n.id)) {
          const kids = childrenByFolderId.get(n.id);
          if (kids && kids.length > 0) visit(kids, depth + 1);
        }
      }
    };
    visit(nodes, 0);
    return out;
  }, [nodes, expanded, childrenByFolderId]);

  // Finder-style state: ``focusIndex`` is the row keyboard nav points
  // at; ``anchorIndex`` is the row a shift-extension ranges from
  // (typically the last row clicked without shift).
  const [focusIndex, setFocusIndex] = useState<number>(-1);
  const [anchorIndex, setAnchorIndex] = useState<number>(-1);

  const replaceRange = useCallback(
    (a: number, b: number) => {
      if (!onSetSelection) return;
      const lo = Math.max(0, Math.min(a, b));
      const hi = Math.min(displayRows.length - 1, Math.max(a, b));
      onSetSelection(displayRows.slice(lo, hi + 1).map((r) => r.node));
    },
    [displayRows, onSetSelection],
  );

  const handleRowClick = useCallback(
    (e: React.MouseEvent, idx: number, n: TreeNode) => {
      if (!showCheckbox) {
        // Selection model isn't active — fall through to the
        // legacy "open on click" behaviour the read-only viewers
        // (non-owners) rely on.
        onOpen(n);
        return;
      }
      const meta = e.metaKey || e.ctrlKey;
      if (e.shiftKey && anchorIndex >= 0 && onSetSelection) {
        replaceRange(anchorIndex, idx);
        setFocusIndex(idx);
        return;
      }
      if (meta) {
        onToggleSelection?.(n);
        setAnchorIndex(idx);
        setFocusIndex(idx);
        return;
      }
      // Plain click: exclusive single-row selection. The previous
      // selection (if any) is replaced, the anchor moves here.
      onSetSelection?.([n]);
      setAnchorIndex(idx);
      setFocusIndex(idx);
    },
    [showCheckbox, onOpen, anchorIndex, onSetSelection, replaceRange, onToggleSelection],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTableSectionElement>) => {
      if (!showCheckbox || displayRows.length === 0) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown" && e.key !== "Enter") return;
      e.preventDefault();
      if (e.key === "Enter") {
        if (focusIndex >= 0 && focusIndex < displayRows.length)
          onOpen(displayRows[focusIndex].node);
        return;
      }
      const dir = e.key === "ArrowUp" ? -1 : 1;
      const next = Math.max(
        0,
        Math.min(displayRows.length - 1, focusIndex < 0 ? 0 : focusIndex + dir),
      );
      if (e.shiftKey && anchorIndex >= 0) {
        replaceRange(anchorIndex, next);
      } else {
        onSetSelection?.([displayRows[next].node]);
        setAnchorIndex(next);
      }
      setFocusIndex(next);
    },
    [showCheckbox, displayRows, focusIndex, anchorIndex, onOpen, onSetSelection, replaceRange],
  );
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden", margin: 0 }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
        <thead>
          <tr style={{ background: "#fafbfc", borderBottom: "1px solid #e5e7eb" }}>
            {showCheckbox && (
              <th style={{ ...thStyle, width: 36 }}>
                <input
                  type="checkbox"
                  aria-label={tFasc("list.selectAll")}
                  checked={nodes.length > 0 && nodes.every((n) => selectedIds?.has(n.id))}
                  ref={(el) => {
                    if (el) {
                      const some = nodes.some((n) => selectedIds?.has(n.id));
                      const all = nodes.every((n) => selectedIds?.has(n.id));
                      el.indeterminate = some && !all;
                    }
                  }}
                  onChange={(e) => {
                    e.stopPropagation();
                    const allSelected = nodes.every((n) => selectedIds?.has(n.id));
                    // Either select-all or clear-all by toggling each
                    // node — the parent reduces the calls into one
                    // state update via functional setState.
                    if (allSelected) {
                      for (const n of nodes) onToggleSelection?.(n);
                    } else {
                      for (const n of nodes) if (!selectedIds?.has(n.id)) onToggleSelection?.(n);
                    }
                  }}
                />
              </th>
            )}
            <th style={thStyle}>{tFasc("list.name")}</th>
            <th style={thStyle}>{tFasc("list.type")}</th>
            <th style={thStyle}>{tFasc("list.date")}</th>
            <th style={{ ...thStyle, textAlign: "right" }}>{tFasc("list.size")}</th>
            {showActions && (
              <th style={{ ...thStyle, width: 36 }} aria-label={tFasc("list.actions")} />
            )}
          </tr>
        </thead>
        <tbody
          tabIndex={showCheckbox ? 0 : -1}
          onKeyDown={handleKeyDown}
          // The keyboard handler lives on the body so arrow keys
          // work no matter which row currently has focus. The body
          // is normally not focusable; we make it ``tabIndex={0}``
          // only when the selection model is active.
          style={{ outline: "none" }}
        >
          {parentNavigation && (
            <tr
              onDragOver={(e) => {
                if (!dragEnabled || draggingId === null) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                setHoverId(PARENT_ROW_ID);
              }}
              onDragLeave={() => {
                if (hoverId === PARENT_ROW_ID) setHoverId(null);
              }}
              onDrop={(e) => {
                if (!dragEnabled || !onDropOnParent) return;
                e.preventDefault();
                const id = e.dataTransfer.getData("text/plain") || draggingId || null;
                setHoverId(null);
                if (!id) return;
                const dragged = nodes.find((n) => n.id === id);
                if (!dragged) return;
                onDropOnParent(dragged, parentNavigation.parentFolderId);
              }}
              onClick={() =>
                onOpen({
                  id: PARENT_ROW_ID,
                  type: "folder",
                  name: "..",
                  path: parentNavigation.parentPath,
                } as TreeNode)
              }
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpen({
                    id: PARENT_ROW_ID,
                    type: "folder",
                    name: "..",
                    path: parentNavigation.parentPath,
                  } as TreeNode);
                }
              }}
              tabIndex={0}
              style={{
                borderBottom: "1px solid #f0f1f4",
                cursor: "pointer",
                background:
                  dragEnabled && hoverId === PARENT_ROW_ID ? "rgba(233,107,31,0.18)" : undefined,
                outline: dragEnabled && hoverId === PARENT_ROW_ID ? "2px solid #e96b1f" : undefined,
              }}
              title={tFasc("list.goParent")}
            >
              {showCheckbox && <td style={tdStyle} />}
              <td style={tdStyle}>
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "0.5rem",
                    fontWeight: 500,
                  }}
                >
                  <TypeIcon type="folder" size={16} />
                  ..
                </span>
              </td>
              <td style={tdStyle} className="meta">
                {tFasc("list.parentFolder")}
              </td>
              <td style={tdStyle} />
              <td style={{ ...tdStyle, textAlign: "right" }} />
              {showActions && <td style={tdStyle} />}
            </tr>
          )}
          {displayRows.map(({ node: n, depth }, idx) => {
            const isSelected = selectedIds?.has(n.id) ?? false;
            const isFocused = idx === focusIndex && showCheckbox;
            const isDragOver = dragEnabled && hoverId === n.id && draggingId !== n.id;
            return (
              <tr
                key={n.id}
                data-item-id={n.target_id ?? n.id}
                draggable={dragEnabled}
                onDragStart={(e) => {
                  if (!dragEnabled) return;
                  e.dataTransfer.setData("text/plain", n.id);
                  e.dataTransfer.effectAllowed = "move";
                  onDragStartId?.(n.id);
                }}
                onDragEnd={() => onDragEnd?.()}
                onDragOver={(e) => {
                  if (!dragEnabled || draggingId === null || draggingId === n.id) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                  setHoverId(n.id);
                }}
                onDragLeave={() => {
                  if (hoverId === n.id) setHoverId(null);
                }}
                onDrop={(e) => {
                  if (!dragEnabled) return;
                  e.preventDefault();
                  const id = e.dataTransfer.getData("text/plain") || draggingId || null;
                  setHoverId(null);
                  onDropOnNode?.(n, id);
                }}
                onClick={(e) => handleRowClick(e, idx, n)}
                // ``handleKeyDown`` is the table-level Enter handler
                // wired on <tbody>; the row only needs Space → open
                // for parity with ``onClick`` because Tab focus lands
                // on the row directly (no parent that captures keys).
                onKeyDown={(e) => {
                  if (e.key === " ") {
                    e.preventDefault();
                    onOpen(n);
                  }
                }}
                onDoubleClick={() => onOpen(n)}
                style={{
                  borderBottom: "1px solid #f0f1f4",
                  cursor: "pointer",
                  background: isDragOver
                    ? "rgba(233,107,31,0.18)"
                    : isSelected
                      ? "rgba(233,107,31,0.10)"
                      : isFocused
                        ? "rgba(0,0,0,0.04)"
                        : undefined,
                  outline: isDragOver
                    ? "2px solid #e96b1f"
                    : isFocused
                      ? "1px solid #e96b1f"
                      : undefined,
                }}
              >
                {showCheckbox && (
                  <td
                    style={tdStyle}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => e.stopPropagation()}
                  >
                    <input
                      type="checkbox"
                      aria-label={tFasc("list.selectRow", { name: n.name })}
                      checked={isSelected}
                      onChange={() => onToggleSelection?.(n)}
                    />
                  </td>
                )}
                <td style={tdStyle}>
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      paddingLeft: `${depth * 18}px`,
                    }}
                  >
                    {n.type === "folder" ? (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleExpanded(n.id);
                        }}
                        aria-label={
                          expanded.has(n.id) ? tFasc("list.collapse") : tFasc("list.expand")
                        }
                        style={{
                          background: "transparent",
                          border: "none",
                          color: "inherit",
                          padding: 0,
                          width: 18,
                          height: 18,
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          cursor: "pointer",
                          fontSize: "0.85rem",
                          lineHeight: 1,
                        }}
                      >
                        {expanded.has(n.id) ? "▾" : "▸"}
                      </button>
                    ) : (
                      <span style={{ display: "inline-block", width: 18 }} />
                    )}
                    <TypeIcon type={n.type} size={16} />
                    {/* Wrap the name in a real <Link> when the row
                        targets a URL: cmd/middle-click then opens
                        in a new tab. Plain click on the row keeps
                        the existing handleRowClick semantics
                        (selection model + open) — we stop propagation
                        on the link click only when modifier keys are
                        present so the row-level click can still run
                        for plain clicks. */}
                    {(() => {
                      const href = nodeHref(n);
                      if (!href) return n.name;
                      return (
                        <Link
                          href={href}
                          style={{ color: "inherit", textDecoration: "none" }}
                          onClick={(e) => {
                            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) {
                              // let the browser open in new tab / new
                              // window; don't bubble to the row.
                              e.stopPropagation();
                              return;
                            }
                            // Plain click: cancel default Link nav and
                            // defer to row-level handleRowClick which
                            // handles selection vs open.
                            e.preventDefault();
                          }}
                        >
                          {n.name}
                        </Link>
                      );
                    })()}
                  </span>
                </td>
                <td style={tdStyle}>{tFasc(`kind.${n.type}`)}</td>
                <td style={tdStyle}>
                  <DateStamps date={n.date} updatedAt={n.updated_at} />
                </td>
                <td style={{ ...tdStyle, textAlign: "right" }}>{formatSize(n.size_bytes)}</td>
                {showActions && (
                  <td
                    style={{ ...tdStyle, textAlign: "right", whiteSpace: "nowrap" }}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => e.stopPropagation()}
                  >
                    {n.type === "study" && (
                      <StudyExportButton
                        studyId={n.target_id ?? n.id}
                        studyLabel={n.name}
                        variant="icon"
                      />
                    )}
                    {n.type === "study" && (
                      <SendStudyButton
                        studyId={n.target_id ?? n.id}
                        patientId={patientId}
                        studyLabel={n.name}
                        variant="icon"
                      />
                    )}
                    {n.type === "document" && onDownload && (
                      <button
                        type="button"
                        className="ghost"
                        disabled={downloadingId === n.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          onDownload(n);
                        }}
                        title={tFasc("list.download")}
                        aria-label={tFasc("list.download")}
                        style={{
                          padding: "0.1rem 0.4rem",
                          color: "var(--bv-fg-soft, #475569)",
                          fontSize: "0.85rem",
                          marginRight: "0.2rem",
                          cursor: downloadingId === n.id ? "wait" : "pointer",
                          opacity: downloadingId === n.id ? 0.55 : 1,
                        }}
                      >
                        {downloadingId === n.id ? "…" : "⬇"}
                      </button>
                    )}
                    {RENAMABLE_KINDS.has(n.type) && onRenameItem && (
                      <button
                        type="button"
                        className="ghost"
                        onClick={(e) => {
                          e.stopPropagation();
                          onRenameItem(n);
                        }}
                        title={tFasc("list.rename")}
                        aria-label={tFasc("list.rename")}
                        style={{
                          padding: "0.1rem 0.4rem",
                          color: "var(--bv-fg-soft, #475569)",
                          fontSize: "0.85rem",
                          marginRight: "0.2rem",
                        }}
                      >
                        ✎
                      </button>
                    )}
                    {n.type === "folder" && onDeleteFolder && (
                      <button
                        type="button"
                        className="ghost"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteFolder(n);
                        }}
                        title={tFasc("list.deleteFolder")}
                        aria-label={tFasc("list.deleteFolder")}
                        style={{
                          padding: "0.1rem 0.4rem",
                          color: "var(--bv-danger, #b42318)",
                          fontSize: "0.85rem",
                        }}
                      >
                        ×
                      </button>
                    )}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  textAlign: "left",
  padding: "0.5rem 0.75rem",
  fontWeight: 600,
  fontSize: "0.8rem",
  color: "#667085",
};

const tdStyle: React.CSSProperties = {
  padding: "0.5rem 0.75rem",
};

// ---- Timeline view ----

function TimelineView({
  nodes,
  onOpen,
  onDeleteFolder,
}: {
  nodes: TreeNode[];
  onOpen: (n: TreeNode) => void;
  onDeleteFolder?: (n: TreeNode) => void;
}) {
  const tFasc = useTranslations("fascicolo");
  // Sort leaf items by descending date. Folders (no date) get pushed to the
  // top so the user still sees navigable containers first.
  const sorted = useMemo(() => {
    const items = [...nodes];
    items.sort((a, b) => {
      if (!a.date && !b.date) return 0;
      if (!a.date) return -1;
      if (!b.date) return 1;
      return b.date.localeCompare(a.date);
    });
    return items;
  }, [nodes]);

  return (
    <div>
      {sorted.map((n) => (
        <div
          key={n.id}
          data-item-id={n.target_id ?? n.id}
          // biome-ignore lint/a11y/useSemanticElements: nested-button conflict (card hosts a <button>); outer is role=button + onKeyDown to keep keyboard parity.
          role="button"
          tabIndex={0}
          onClick={() => onOpen(n)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onOpen(n);
            }
          }}
          className="card"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            cursor: "pointer",
            marginBottom: "0.5rem",
          }}
        >
          <TypeIcon type={n.type} size={24} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontWeight: 500,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {n.name}
            </div>
            <div className="meta" style={{ fontSize: "0.8rem" }}>
              {tFasc(`kind.${n.type}`)}
              {n.date ? ` · ${formatDate(n.date)}` : ""}
            </div>
          </div>
          {n.type === "folder" && onDeleteFolder && (
            <button
              type="button"
              className="ghost"
              onClick={(e) => {
                e.stopPropagation();
                onDeleteFolder(n);
              }}
              title={tFasc("list.deleteFolder")}
              aria-label={tFasc("list.deleteFolder")}
              style={{
                padding: "0.15rem 0.5rem",
                color: "var(--bv-danger, #b42318)",
                fontSize: "0.9rem",
              }}
            >
              ×
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

// ---- Shared helpers ----

function formatSize(bytes: number | null): string {
  if (bytes == null) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/**
 * The backend emits a mix of plain ISO dates ("2026-04-08") and full
 * timestamps ("2026-04-25T20:25:51.130014+00:00"). We only want the
 * day-precision portion in the listing — consistent and readable.
 */
function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length >= 10 ? value.slice(0, 10) : value;
}

/**
 * Two-line date stamp shown on each fascicolo card and table row.
 * Primary line: creation date — what the backend exposes in
 * ``node.date`` (study_date for studies, document_date for
 * documents, created_at for folders/series/reports/consultations,
 * with backend-side fallback to created_at when the clinical date is
 * absent). Secondary line: last modification time, when the
 * underlying resource tracks it.
 *
 * The icons differentiate the two without forcing the user to read
 * labels: a small calendar for the creation date and a pencil for
 * the modification timestamp.
 */
function DateStamps({
  date,
  updatedAt,
  kindFallback,
}: {
  date: string | null | undefined;
  updatedAt?: string | null;
  kindFallback?: string;
}) {
  const tFasc = useTranslations("fascicolo");
  const primary = date ? formatDate(date) : (kindFallback ?? "-");
  const showSecondary = !!updatedAt;
  const secondary = showSecondary ? formatDate(updatedAt) : null;
  return (
    <div
      className="meta"
      style={{
        fontSize: "0.75rem",
        lineHeight: 1.3,
        display: "flex",
        flexDirection: "column",
        gap: "0.1rem",
      }}
    >
      <span
        style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem" }}
        title={tFasc("dateCreatedTitle")}
      >
        {date && <CalendarIcon />}
        <span>{primary}</span>
      </span>
      {secondary && (
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.3rem",
            opacity: 0.7,
            fontSize: "0.7rem",
          }}
          title={tFasc("dateUpdatedTitle")}
        >
          <PencilIcon />
          <span>{secondary}</span>
        </span>
      )}
    </div>
  );
}

function CalendarIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
    </svg>
  );
}

function TypeIcon({ type, size = 24 }: { type: TreeNode["type"]; size?: number }) {
  const color = iconColor(type);
  const stroke: React.SVGProps<SVGSVGElement> = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: color,
    strokeWidth: 2,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
  };
  switch (type) {
    case "folder":
      return (
        <svg aria-hidden="true" {...stroke}>
          <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
        </svg>
      );
    case "study":
      // layered squares → stack of images
      return (
        <svg aria-hidden="true" {...stroke}>
          <rect x="3" y="6" width="14" height="14" rx="2" />
          <rect x="7" y="2" width="14" height="14" rx="2" />
        </svg>
      );
    case "document":
      return (
        <svg aria-hidden="true" {...stroke}>
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
          <path d="M14 3v6h6" />
        </svg>
      );
    case "report":
      return (
        <svg aria-hidden="true" {...stroke}>
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
          <path d="M14 3v6h6" />
          <path d="M8 13h8M8 17h5" />
        </svg>
      );
    case "annotation":
      return (
        <svg aria-hidden="true" {...stroke}>
          <path d="M12 20h9" />
          <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
        </svg>
      );
    case "consultation":
      return (
        <svg aria-hidden="true" {...stroke}>
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
        </svg>
      );
  }
}

function iconColor(t: TreeNode["type"]): string {
  switch (t) {
    case "folder":
      return "#e96b1f";
    case "study":
      return "#1e40af";
    case "series":
      return "#1e3a8a";
    case "document":
      return "#475569";
    case "report":
      return "#059669";
    case "annotation":
      return "#854d0e";
    case "consultation":
      return "#6d28d9";
    case "pathology_slide":
      // Bordeaux for histology — distinct from the radiology blue
      // family above so the user reads the card kind at a glance.
      return "#7f1d1d";
  }
}
