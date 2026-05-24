"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { API_BASE_URL, getStoredToken } from "@/lib/api";

interface Props {
  seriesId: string;
  sliceCount: number;
  className?: string;
}

/**
 * Multi-slice DICOM preview card. Renders one slice at a time and lets
 * the user scrub through the series in three ways:
 *
 *   1. Range slider at the bottom — always visible, no ambiguity.
 *   2. Mouse wheel ON the image — only when the cursor is over the
 *      image, the page itself does not scroll.
 *   3. Click into image then ← / → arrows — keyboard navigation.
 *
 * The wheel handler must be attached as a non-passive native listener
 * because React 17+ binds wheel as ``{ passive: true }`` by default,
 * which silently drops ``preventDefault()`` and the page scrolls
 * underneath the cursor.
 */
export default function SeriesPreview({ seriesId, sliceCount, className }: Props) {
  const t = useTranslations("seriesPreview");
  // ``null`` on first render means "let the backend pick the default
  // slice". The thumbnail endpoint returns an ``X-Slice-Index`` header
  // identifying the instance it actually served — typically the middle
  // image-like instance, skipping any SR/PR/KO that share the series
  // UID. We sync ``index`` to that value once so the slider lands on
  // the right slot. After the user moves the slider/wheel/keyboard the
  // index is no longer ``null`` and we honour their choice verbatim.
  const [index, setIndex] = useState<number | null>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [noPixels, setNoPixels] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Image fetch
  // Image fetch with: (1) AbortController so a stale request from a
  // previous index never overwrites the latest one, (2) 60ms debounce
  // so dragging the slider doesn't flood the backend with requests
  // mid-gesture (~16 fetches per second otherwise), (3) we keep the
  // previous frame visible (``setSrc`` only fires on success) so the
  // canvas doesn't blank between frames — the user perceives a smooth
  // crossfade instead of jerky black-then-image.
  //
  // Note: ``noPixels`` and ``loadErr`` are intentionally NOT in the
  // deps array. Including them re-fired the effect after every error
  // → cascading re-fetches.
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;

    const t = window.setTimeout(() => {
      setLoading(true);
      const url =
        index === null
          ? `${API_BASE_URL}/api/series/${seriesId}/thumbnail`
          : `${API_BASE_URL}/api/series/${seriesId}/thumbnail?index=${index}`;
      let servedIndex: number | null = null;
      fetch(url, { credentials: "include", headers, signal: ac.signal })
        .then((r) => {
          if (r.status === 422) {
            setNoPixels(true);
            setLoadErr(null);
            throw new Error("no pixel data");
          }
          if (r.status === 404) {
            setLoadErr("notfound");
            throw new Error("404");
          }
          if (!r.ok) throw new Error(`${r.status}`);
          const hdr = r.headers.get("x-slice-index");
          if (hdr !== null) {
            const parsed = Number.parseInt(hdr, 10);
            if (Number.isFinite(parsed)) servedIndex = parsed;
          }
          return r.blob();
        })
        .then((blob) => {
          setNoPixels(false);
          setLoadErr(null);
          const blobUrl = URL.createObjectURL(blob);
          setSrc((prev) => {
            if (prev) URL.revokeObjectURL(prev);
            return blobUrl;
          });
          // Adopt the backend's default exactly once (when the user
          // has not yet picked an index). Without this the slider
          // would still show position 0 even though the backend was
          // serving slot N/2.
          if (index === null && servedIndex !== null) {
            setIndex(servedIndex);
          }
        })
        .catch((e: unknown) => {
          if (ac.signal.aborted) return;
          const msg = e instanceof Error ? e.message : "load";
          if (msg !== "no pixel data" && msg !== "404") {
            setLoadErr("error");
          }
        })
        .finally(() => {
          if (!ac.signal.aborted) setLoading(false);
        });
    }, 60);

    return () => {
      window.clearTimeout(t);
      ac.abort();
    };
  }, [seriesId, index]);

  // Native wheel listener — must be non-passive to call preventDefault.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || sliceCount <= 1) return;
    const onWheel = (e: WheelEvent) => {
      // Only react when the cursor is actually over the image area.
      e.preventDefault();
      e.stopPropagation();
      const delta = e.deltaY > 0 ? 1 : -1;
      setIndex((prev) => {
        const base = prev ?? Math.floor(sliceCount / 2);
        return Math.max(0, Math.min(sliceCount - 1, base + delta));
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      el.removeEventListener("wheel", onWheel);
    };
  }, [sliceCount]);

  const isMulti = sliceCount > 1;
  // Until the first thumbnail response lands, we don't yet know which
  // slot the backend picked — show the would-be middle slot in the
  // overlay so the slider thumb has something coherent to track.
  const displayIndex = index ?? Math.floor(sliceCount / 2);

  return (
    <div
      className={className}
      style={{
        position: "relative",
        width: "100%",
        background: "#111",
        borderRadius: 6,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        ref={containerRef}
        style={{
          position: "relative",
          width: "100%",
          aspectRatio: "1",
          // ``overscroll-behavior: contain`` belt-and-braces with the
          // preventDefault above: even if the wheel listener is somehow
          // bypassed, scroll won't propagate past this element.
          overscrollBehavior: "contain",
          background: "#000",
          cursor: isMulti ? "ns-resize" : "default",
        }}
        tabIndex={isMulti ? 0 : -1}
        onKeyDown={(e) => {
          if (!isMulti) return;
          if (e.key === "ArrowDown" || e.key === "ArrowRight") {
            e.preventDefault();
            setIndex((p) => {
              const base = p ?? Math.floor(sliceCount / 2);
              return Math.min(sliceCount - 1, base + 1);
            });
          } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
            e.preventDefault();
            setIndex((p) => {
              const base = p ?? Math.floor(sliceCount / 2);
              return Math.max(0, base - 1);
            });
          }
        }}
      >
        {noPixels ? (
          <Placeholder primary={t("noPixelsPrimary")} secondary={t("noPixelsSecondary")} icon />
        ) : loadErr === "notfound" ? (
          <Placeholder
            primary={t("previewUnavailablePrimary")}
            secondary={t("previewUnavailableSecondary")}
          />
        ) : loadErr === "error" ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              color: "#cf6e6e",
              fontSize: "0.8rem",
              padding: "1rem",
              textAlign: "center",
            }}
          >
            {t("loadError")}
          </div>
        ) : src ? (
          <img
            src={src}
            alt={t("sliceAlt", { n: displayIndex + 1, total: sliceCount })}
            draggable={false}
            style={{
              display: "block",
              width: "100%",
              height: "100%",
              objectFit: "contain",
              // Subtle dim while a new slice is being fetched so the
              // user gets feedback during a slider drag without the
              // canvas blanking out.
              opacity: loading ? 0.85 : 1,
              transition: "opacity 0.08s linear",
            }}
          />
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              color: "#666",
              fontSize: "0.85rem",
            }}
          >
            {t("loading")}
          </div>
        )}
        <div
          style={{
            position: "absolute",
            bottom: 4,
            right: 6,
            fontSize: "0.7rem",
            color: "#ccc",
            background: "rgba(0,0,0,0.6)",
            padding: "1px 6px",
            borderRadius: 4,
          }}
        >
          {displayIndex + 1} / {sliceCount}
        </div>
      </div>

      {/* Slider control: visible whenever there's more than one slice.
          A real <input type="range"> is a clearer affordance than the
          "wheel over image" gesture and works on touch devices where
          wheel doesn't exist.
          The card is often nested inside a <Link> (study detail) so we
          have to swallow the click + pointerdown to stop the parent
          Link's navigation when the user drags the slider. */}
      {isMulti && (
        <div
          // The slider container swallows pointer / wheel events to
          // stop the parent <Link> from navigating mid-scrub. Same
          // semantic for keyboard: stopPropagation on Enter / Space
          // would let the user activate the slider's input by
          // tabbing to it without bubbling to the parent Link.
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
          onKeyDown={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          onWheel={(e) => e.stopPropagation()}
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            padding: "0.4rem 0.5rem",
            background: "#181a22",
            borderTop: "1px solid #1c2230",
          }}
        >
          <input
            type="range"
            min={0}
            max={sliceCount - 1}
            step={1}
            value={displayIndex}
            onChange={(e) => setIndex(Number.parseInt(e.target.value, 10))}
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
            aria-label={`Slice (1 di ${sliceCount})`}
            style={{
              flex: 1,
              accentColor: "#e96b1f",
              minWidth: 0,
            }}
          />
          <span
            style={{
              fontFamily: "ui-monospace, Menlo, monospace",
              fontSize: "0.72rem",
              color: "#9aa3b8",
              minWidth: "3.5em",
              textAlign: "right",
            }}
          >
            {displayIndex + 1}/{sliceCount}
          </span>
        </div>
      )}
    </div>
  );
}

function Placeholder({
  primary,
  secondary,
  icon,
}: {
  primary: string;
  secondary?: string;
  icon?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        height: "100%",
        color: "#9aa3b8",
        fontSize: "0.78rem",
        textAlign: "center",
        padding: "1rem",
        gap: "0.4rem",
      }}
    >
      {icon && (
        <svg
          width="28"
          height="28"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
          <path d="M14 3v6h6" />
          <path d="M9 13h6M9 17h4" />
        </svg>
      )}
      <span>{primary}</span>
      {secondary && <span style={{ fontSize: "0.7rem", opacity: 0.7 }}>{secondary}</span>}
    </div>
  );
}
