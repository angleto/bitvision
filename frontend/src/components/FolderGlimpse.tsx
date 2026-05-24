"use client";

// Transparent "stack" preview of what's inside a folder. Two pieces:
//   * 1-3 micro-tiles (study thumbnails when present, glyphs otherwise)
//     fanned out behind the folder icon — macOS Stacks lookalike.
//   * a kind-dominant tag (e.g. "studio + referto") rendered as a
//     subtle badge on top of the folder so a glance tells the user
//     what's inside without entering.
//
// Driven entirely by `preview` + `preview_kinds` from the backend
// (`api/patient_tree.FolderPreviewEntry`); when the fields are absent
// the glimpse degrades to a plain folder icon, so the component works
// against older API versions during rollout.

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { API_BASE_URL, type FolderPreviewEntry, getStoredToken } from "@/lib/api";

interface Props {
  preview: FolderPreviewEntry[] | null | undefined;
  previewKinds: Record<string, number> | null | undefined;
  /** Required to fetch document thumbnails living in the peek stack
   *  (the endpoint is patient-scoped). When omitted, document tiles
   *  degrade to the discreet icon variant. */
  patientId?: string;
  /** Falls back to a plain folder icon at this size when no preview. */
  fallbackSize?: number;
}

const STACK_HEIGHT = 110;
const STACK_WIDTH = 140;

export default function FolderGlimpse({
  preview,
  previewKinds,
  patientId,
  fallbackSize = 56,
}: Props) {
  const items = (preview ?? []).slice(0, 3);
  if (items.length === 0) {
    return (
      <div
        style={{
          padding: "0.6rem 0",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          height: STACK_HEIGHT,
        }}
      >
        <FolderGlyph size={fallbackSize} />
      </div>
    );
  }
  return (
    <div
      aria-hidden="true"
      style={{
        position: "relative",
        width: "100%",
        height: STACK_HEIGHT,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {/* The folder glyph sits at the bottom-front, tilted slightly so
          the stacked previews behind it read as "inside the folder"
          instead of "next to the folder". The wrapper carries
          ``folder-glimpse-stack`` so globals.css can scale it up on
          card hover, expanding the peek without re-laying-out the
          per-tile transforms. */}
      <div
        className="folder-glimpse-stack"
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 0,
          transformOrigin: "center 60%",
        }}
      >
        {items.map((it, i) => {
          // Per-tile transforms are driven by CSS in
          // ``globals.css`` (``.folder-peek-tile``) so the hover
          // state can fan the tiles out into a non-overlapping
          // spread. We pass the ordinal + the centre of the stack
          // as CSS variables so the same rule scales to 1, 2 or 3
          // tiles symmetrically around the folder.
          const z = 10 + i;
          const tileStyle: React.CSSProperties & Record<string, string | number> = {
            position: "absolute",
            width: STACK_WIDTH * 0.72,
            height: STACK_HEIGHT * 0.62,
            zIndex: z,
            boxShadow: "0 2px 5px rgba(0,0,0,0.18)",
            opacity: 0.96,
            borderRadius: 4,
            overflow: "hidden",
            border: "1px solid rgba(0,0,0,0.08)",
            "--peek-i": String(i),
            "--peek-mid": String((items.length - 1) / 2),
          };
          return (
            <PeekTile
              // The preview is at most 3 items, never re-ordered
              // independently of the parent listing; positional key
              // is safe and there's no stable id on the entry to use.
              // biome-ignore lint/suspicious/noArrayIndexKey: static 3-item peek; positional key is intentional.
              key={i}
              item={it}
              patientId={patientId}
              className="folder-peek-tile"
              style={tileStyle}
            />
          );
        })}
      </div>
      <div
        style={{
          position: "absolute",
          left: "50%",
          bottom: 4,
          transform: "translateX(-50%)",
          zIndex: 99,
          filter: "drop-shadow(0 2px 4px rgba(0,0,0,0.25))",
        }}
      >
        <FolderGlyph size={48} />
      </div>
      {/* The kind-dominant badge used to live here ("studio + referto"),
          but it duplicated the per-kind breakdown that the grid hover
          preview already shows. We surface item kinds in exactly one
          place now (the hover overlay) so the card stays clean. */}
    </div>
  );
}

function FolderGlyph({ size }: { size: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="#e96b1f"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: "block" }}
    >
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
    </svg>
  );
}

function PeekTile({
  item,
  style,
  patientId,
  className,
}: {
  item: FolderPreviewEntry;
  style: React.CSSProperties;
  patientId?: string;
  className?: string;
}) {
  if (item.type === "study" && item.thumbnail_series_id) {
    return (
      <div className={className} style={{ ...style, background: "#0a0d14" }}>
        <SeriesThumbnail seriesId={item.thumbnail_series_id} modality={item.modality ?? null} />
      </div>
    );
  }
  if (
    item.type === "document" &&
    patientId &&
    item.target_id &&
    isThumbnailableDoc(item.mime_type ?? null, item.name)
  ) {
    return (
      <div className={className} style={{ ...style, background: "#f4f4f5" }}>
        <DocumentPeek
          patientId={patientId}
          docId={item.target_id}
          mimeType={item.mime_type ?? null}
          filename={item.name}
        />
      </div>
    );
  }
  if (item.type === "folder") {
    // Folder tile rendered as a fallback when a parent contains
    // mostly sub-folders. Shows the sub-folder name + a recursive
    // descendant count so the user sees ``TAC (12)`` instead of an
    // empty folder glyph.
    return (
      <div
        className={className}
        style={{
          ...style,
          background: tileBackground(item.type),
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 4,
          padding: "0.4rem",
          textAlign: "center",
        }}
      >
        <PeekIcon kind={item.type} />
        <div
          style={{
            fontSize: "0.66rem",
            fontWeight: 500,
            color: "#444",
            lineHeight: 1.2,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            maxWidth: "100%",
          }}
          title={item.name}
        >
          {item.name}
        </div>
        {item.folder_descendant_count !== null &&
          item.folder_descendant_count !== undefined &&
          item.folder_descendant_count > 0 && (
            <div style={{ fontSize: "0.6rem", color: "#888" }}>{item.folder_descendant_count}</div>
          )}
      </div>
    );
  }
  return (
    <div
      className={className}
      style={{
        ...style,
        background: tileBackground(item.type),
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <PeekIcon kind={item.type} />
    </div>
  );
}

/** Mirrors ``services.document_thumbnails.is_supported_thumbnail_mime``
 *  + ContentPane.supportsDocumentThumbnail so we don't fire requests
 *  that are guaranteed to 404. Inlined (instead of importing from
 *  ContentPane) to keep this component self-contained. */
function isThumbnailableDoc(mime: string | null, filename: string): boolean {
  const ct = (mime ?? "").toLowerCase();
  if (ct === "application/pdf") return true;
  if (ct.startsWith("image/")) return true;
  const ext = filename.toLowerCase().split(".").pop() ?? "";
  return ["pdf", "png", "jpg", "jpeg", "webp", "gif", "tif", "tiff"].includes(ext);
}

function tileBackground(kind: FolderPreviewEntry["type"]): string {
  switch (kind) {
    case "study":
      return "#0a0d14";
    case "series":
      return "#0a0d14";
    case "report":
      return "#ecfdf5";
    case "document":
      return "#f5f5f4";
    case "consultation":
      return "#f3f0ff";
    case "annotation":
      return "#fef3c7";
    case "folder":
      return "#fff7e6";
    default:
      return "#fff";
  }
}

function PeekIcon({ kind }: { kind: FolderPreviewEntry["type"] }) {
  const color = peekIconColor(kind);
  const props: React.SVGProps<SVGSVGElement> = {
    width: 32,
    height: 32,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: color,
    strokeWidth: 1.7,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
  };
  switch (kind) {
    case "report":
      return (
        <svg aria-hidden="true" {...props}>
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
          <path d="M14 3v6h6" />
          <path d="M8 13h8M8 17h5" />
        </svg>
      );
    case "document":
      return (
        <svg aria-hidden="true" {...props}>
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
          <path d="M14 3v6h6" />
        </svg>
      );
    case "consultation":
      return (
        <svg aria-hidden="true" {...props}>
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
        </svg>
      );
    case "annotation":
      return (
        <svg aria-hidden="true" {...props}>
          <path d="M12 20h9" />
          <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
        </svg>
      );
    case "series":
    case "study":
      return (
        <svg aria-hidden="true" {...props}>
          <rect x="3" y="6" width="14" height="14" rx="2" />
          <rect x="7" y="2" width="14" height="14" rx="2" />
        </svg>
      );
    case "folder":
      return (
        <svg aria-hidden="true" {...props}>
          <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
        </svg>
      );
    default:
      return null;
  }
}

function peekIconColor(kind: FolderPreviewEntry["type"]): string {
  switch (kind) {
    case "study":
    case "series":
      return "#e2e8f0";
    case "report":
      return "#059669";
    case "consultation":
      return "#6d28d9";
    case "annotation":
      return "#854d0e";
    case "folder":
      return "#e96b1f";
    default:
      return "#475569";
  }
}

function DocumentPeek({
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
    fetch(`${API_BASE_URL}/api/patients/${patientId}/documents/${docId}/thumbnail?max_side=160`, {
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

  if (failed || !src) {
    // Discreet placeholder while loading / on failure: small icon
    // centered, not the full gray block so the peek tile still
    // reads as a document without occluding adjacent study tiles.
    void mimeType;
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <PeekIcon kind="document" />
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={filename}
      draggable={false}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
        display: "block",
      }}
    />
  );
}

function SeriesThumbnail({
  seriesId,
  modality,
}: {
  seriesId: string;
  modality: string | null;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(`${API_BASE_URL}/api/series/${seriesId}/thumbnail?max_side=160`, {
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

  if (failed || !src) {
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <PeekIcon kind="study" />
      </div>
    );
  }
  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <img
        src={src}
        alt=""
        draggable={false}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
        }}
      />
      {modality && (
        <span
          style={{
            position: "absolute",
            top: 2,
            left: 2,
            background: "rgba(0,0,0,0.65)",
            color: "#e6ecf3",
            fontSize: "0.55rem",
            padding: "0 4px",
            borderRadius: 3,
            fontFamily: "ui-monospace, monospace",
            letterSpacing: "0.04em",
          }}
        >
          {modality}
        </span>
      )}
    </div>
  );
}
