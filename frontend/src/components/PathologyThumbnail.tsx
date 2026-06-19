"use client";

// Cover image for a pathology whole-slide (WSI) card.
//
// Fetches the backend-rendered JPEG thumbnail
// (``/api/pathology-slides/{id}/thumbnail``) and overlays the stain
// chip (top-left) plus a "WSI" badge (bottom-right) so the user can
// read the slide type at a glance, the way DICOM studies are tagged
// with the "DICOM" badge. On 404 / fetch failure it falls back to a
// generic document glyph.
//
// Used both on the Drive-style fascicolo tree (ContentPane) and on the
// public pathology library grid (/pathology). Public slides are served
// anonymously, so the Authorization header is attached only when a
// session cookie shim returns a token (it currently no-ops under the
// HttpOnly cookie model); ``credentials: "include"`` carries the cookie
// for the authenticated tree view.

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { API_BASE_URL, getStoredToken } from "@/lib/api";

interface Props {
  slideId: string;
  stain: string | null;
  /** Card cover height in px. The library grid uses a taller cover than
   *  the dense fascicolo tree, so it stays a prop. */
  height?: number;
}

export default function PathologyThumbnail({ slideId, stain, height = 110 }: Props) {
  const tThumb = useTranslations("thumbnail");
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(`${API_BASE_URL}/api/pathology-slides/${slideId}/thumbnail`, {
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
  }, [slideId]);

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height,
        background: "#1c1410",
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
          style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
        />
      ) : failed ? (
        <svg
          aria-hidden="true"
          width={48}
          height={48}
          viewBox="0 0 24 24"
          fill="none"
          stroke="#a08070"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
          <path d="M14 3v6h6" />
        </svg>
      ) : (
        <div style={{ color: "#a08070", fontSize: "0.75rem" }}>{tThumb("loading")}</div>
      )}
      {stain && (
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
            letterSpacing: "0.04em",
          }}
        >
          {stain}
        </span>
      )}
      <span
        style={{
          position: "absolute",
          bottom: 4,
          right: 4,
          background: "rgba(127, 29, 29, 0.85)",
          color: "#fff",
          fontSize: "0.6rem",
          padding: "1px 5px",
          borderRadius: 3,
          letterSpacing: "0.05em",
          fontWeight: 600,
        }}
      >
        WSI
      </span>
    </div>
  );
}
