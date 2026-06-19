"use client";

/*
 * /pathology/[slideId] — Deep Zoom whole-slide viewer.
 *
 * Loads the slide metadata + its Deep Zoom ``.dzi`` descriptor, then
 * opens an OpenSeadragon viewer over a CUSTOM tile source.
 *
 * Why a custom tile source (not the .dzi URL): the backend serves tiles
 * at ``/tiles/{level}/{col}/{row}`` (slash-separated), not the DZI
 * default ``{col}_{row}.{format}``. OpenSeadragon's ``DziTileSource``
 * would build the default URL shape, so we hand it a plain tile-source
 * descriptor with our own ``getTileUrl``. Public slides are served
 * anonymously, so tiles need no auth header.
 *
 * Level math (Deep Zoom): a DZI pyramid has
 *   levelCount = ceil(log2(max(width, height))) + 1
 * levels, indexed 0..levelCount-1. Level ``levelCount-1`` is full
 * resolution (one tile-grid covering width x height); level 0 is a
 * single 1x1-ish pixel. We pass ``minLevel: 0`` and
 * ``maxLevel: levelCount - 1`` so OpenSeadragon requests the same level
 * indices the backend tiler produced.
 *
 * Cleanup: the OpenSeadragon instance is destroyed on unmount (and
 * before any re-init) to avoid WebGL/context + DOM leaks.
 */

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";
import type OpenSeadragonNS from "openseadragon";
import { useEffect, useRef, useState } from "react";

import LicenseBadge from "@/components/LicenseBadge";
import { ApiError, type PathologySlide, getPathologySlide, pathologySlidesApi } from "@/lib/api";

interface DziInfo {
  width: number;
  height: number;
  tileSize: number;
  tileOverlap: number;
  format: string;
}

/**
 * Parse the Deep Zoom ``.dzi`` XML descriptor.
 *
 * Shape: ``<Image Format="jpeg" Overlap="1" TileSize="254">
 *           <Size Width="W" Height="H"/></Image>``.
 *
 * Uses the browser ``DOMParser`` (this is a client component). Returns
 * null when the document is malformed or the required attributes are
 * absent, so the caller can show the error state instead of feeding
 * NaN dimensions into OpenSeadragon.
 */
function parseDzi(xml: string): DziInfo | null {
  try {
    const doc = new DOMParser().parseFromString(xml, "application/xml");
    if (doc.querySelector("parsererror")) return null;
    const image = doc.querySelector("Image");
    const size = doc.querySelector("Size");
    if (!image || !size) return null;
    const width = Number(size.getAttribute("Width"));
    const height = Number(size.getAttribute("Height"));
    const tileSize = Number(image.getAttribute("TileSize"));
    const tileOverlap = Number(image.getAttribute("Overlap") ?? "0");
    const format = image.getAttribute("Format") ?? "jpeg";
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
      return null;
    }
    if (!Number.isFinite(tileSize) || tileSize <= 0) return null;
    return {
      width,
      height,
      tileSize,
      tileOverlap: Number.isFinite(tileOverlap) ? tileOverlap : 0,
      format,
    };
  } catch {
    return null;
  }
}

type LoadState = "loading" | "ready" | "notfound" | "error";

export default function PathologySlideViewerPage() {
  const params = useParams<{ slideId: string }>();
  const slideId = params.slideId;
  const t = useTranslations("pathologyLibrary");

  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewerRef = useRef<OpenSeadragonNS.Viewer | null>(null);

  const [slide, setSlide] = useState<PathologySlide | null>(null);
  const [state, setState] = useState<LoadState>("loading");

  // Fetch metadata + DZI, then init OpenSeadragon. One effect keyed on
  // slideId so navigating between slides tears the old viewer down and
  // re-inits cleanly.
  useEffect(() => {
    let cancelled = false;
    const ac = new AbortController();

    // Defensive: dispose any prior viewer before a re-init.
    const disposePrior = () => {
      if (viewerRef.current) {
        viewerRef.current.destroy();
        viewerRef.current = null;
      }
    };

    async function init() {
      setState("loading");
      setSlide(null);
      disposePrior();
      try {
        // Metadata first — a 404 here is the clean "not public / not
        // found" branch and avoids a pointless DZI fetch.
        const meta = await getPathologySlide(slideId);
        if (cancelled) return;
        setSlide(meta);

        const dziResp = await fetch(pathologySlidesApi.dziUrl(slideId), {
          credentials: "include",
          cache: "no-store",
          signal: ac.signal,
        });
        if (!dziResp.ok) {
          if (!cancelled) setState(dziResp.status === 404 ? "notfound" : "error");
          return;
        }
        const dziXml = await dziResp.text();
        if (cancelled) return;
        const dzi = parseDzi(dziXml);
        if (!dzi) {
          if (!cancelled) setState("error");
          return;
        }

        // Deep Zoom level count from the base dimensions.
        const maxDim = Math.max(dzi.width, dzi.height);
        const levelCount = Math.ceil(Math.log2(maxDim)) + 1;
        const maxLevel = levelCount - 1;

        // Dynamic import: OpenSeadragon touches ``window`` at module
        // eval, so it must not run during SSR. The viewer effect only
        // runs client-side, so importing here is safe.
        const OpenSeadragon = (await import("openseadragon")).default;
        if (cancelled || !containerRef.current) return;

        // Custom tile source. Passing a plain descriptor (not a .dzi
        // URL) makes OpenSeadragon adopt our ``getTileUrl`` while still
        // computing the tile grid from width/height/tileSize.
        const tileSource = {
          width: dzi.width,
          height: dzi.height,
          tileSize: dzi.tileSize,
          tileOverlap: dzi.tileOverlap,
          minLevel: 0,
          maxLevel,
          getTileUrl: (level: number, x: number, y: number) =>
            pathologySlidesApi.tileUrl(slideId, level, x, y),
        };

        disposePrior();
        const viewer = OpenSeadragon({
          element: containerRef.current,
          prefixUrl: "",
          // No bundled nav-image icons (we ship none); use the simple
          // built-in button shapes instead of 404-ing on sprite PNGs.
          showNavigator: true,
          navigatorPosition: "TOP_RIGHT",
          showNavigationControl: true,
          navImages: undefined,
          crossOriginPolicy: "Anonymous",
          // Tiles are served with their own cache headers; let OSD keep
          // a generous in-memory cache for smooth panning.
          maxImageCacheCount: 512,
          gestureSettingsMouse: { clickToZoom: false },
          tileSources: tileSource,
        });
        viewerRef.current = viewer;
        if (!cancelled) setState("ready");
      } catch (e) {
        if (cancelled || (e instanceof DOMException && e.name === "AbortError")) return;
        if (e instanceof ApiError && e.status === 404) setState("notfound");
        else setState("error");
      }
    }

    init();

    return () => {
      cancelled = true;
      ac.abort();
      disposePrior();
    };
  }, [slideId]);

  return (
    <main style={{ padding: "1.25rem", maxWidth: 1400, margin: "0 auto" }}>
      <header style={{ marginBottom: "0.85rem" }}>
        <Link href="/pathology" style={{ fontSize: "0.85rem" }}>
          ← {t("backToLibrary")}
        </Link>
        <h1 style={{ margin: "0.35rem 0 0", fontSize: "1.3rem" }}>
          {slide?.stain || t("viewerTitle")}
        </h1>
        {slide && (
          <div
            className="meta"
            style={{
              fontSize: "0.82rem",
              marginTop: "0.3rem",
              display: "flex",
              gap: "0.6rem",
              flexWrap: "wrap",
              alignItems: "center",
            }}
          >
            {slide.source_collection && (
              <span>
                {t("collectionLabel")}: {slide.source_collection}
              </span>
            )}
            <span>
              {t("sourceFormatLabel")}: {slide.source_format}
            </span>
            {slide.magnification != null && (
              <span>
                {t("magnificationLabel")}: {slide.magnification}x
              </span>
            )}
            {slide.base_width != null && slide.base_height != null && (
              <span>
                {t("dimensionsLabel")}: {slide.base_width} × {slide.base_height}
              </span>
            )}
            {slide.license_spdx && (
              <LicenseBadge
                study={{
                  license_spdx: slide.license_spdx,
                  license_url: slide.license_url,
                  citation_text: slide.citation_text,
                  citation_required: slide.citation_required,
                  source_collection: slide.source_collection,
                  commercial_use_allowed: slide.commercial_use_allowed,
                }}
                commercialUseAllowed={slide.commercial_use_allowed}
              />
            )}
          </div>
        )}
      </header>

      {state === "loading" && <p className="meta">{t("viewerLoading")}</p>}
      {state === "notfound" && <p className="meta">{t("viewerNotFound")}</p>}
      {state === "error" && <p style={{ color: "var(--bv-error, #cf6e6e)" }}>{t("viewerError")}</p>}

      {/* The OpenSeadragon container is always mounted (the viewer needs
          a stable DOM node), but hidden until ``ready`` so the loading /
          error copy reads cleanly above it. */}
      <div
        ref={containerRef}
        aria-label={t("viewerTitle")}
        style={{
          width: "100%",
          height: "70vh",
          minHeight: 420,
          background: "#0c0a09",
          borderRadius: 8,
          overflow: "hidden",
          display: state === "ready" ? "block" : "none",
        }}
      />
      {state === "ready" && (
        <p className="meta" style={{ fontSize: "0.78rem", marginTop: "0.5rem" }}>
          {t("viewerHint")}
        </p>
      )}
    </main>
  );
}
