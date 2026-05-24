"use client";

// Rotating-MIP cine widget for the PET viewer sidebar.
//
// Backend serves a single PNG sprite-sheet with N frames stacked
// vertically. We render one frame at a time by translating an
// <img> inside a clipped box, like classic CSS sprite animation.
// Auto-play loops; the slider lets the user scrub manually.
//
// Why a sprite-sheet instead of N PNGs: one network round-trip,
// one decode pass in the browser, predictable cache behaviour.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, type MipCineManifest, getStoredToken, petMipApi } from "@/lib/api";

interface Props {
  seriesId: string;
}

const DEFAULT_FRAMES = 36;
const DEFAULT_HEIGHT = 384;
const PLAY_FPS = 12;

export default function MIPCinePanel({ seriesId }: Props) {
  const tv = useTranslations("viewer");
  const [manifest, setManifest] = useState<MipCineManifest | null>(null);
  // The sprite endpoint requires the same JWT the rest of the API
  // demands, but a bare ``<img src>`` request from the browser doesn't
  // carry our Authorization header. Fetch the bytes through the
  // authed ``fetch`` path and convert to an object URL so the
  // browser is happy to render the cached PNG.
  const [spriteBlobUrl, setSpriteBlobUrl] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const rafRef = useRef<number | null>(null);
  const lastTickRef = useRef<number>(0);

  // biome-ignore lint/correctness/useExhaustiveDependencies: viewer lifecycle effect — re-running on derived deps would tear down GPU resources.
  const generate = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const m = await petMipApi.cine(seriesId, {
        num_frames: DEFAULT_FRAMES,
        target_height: DEFAULT_HEIGHT,
      });
      setManifest(m);
      setFrame(0);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : tv("mipGenerationFailed"));
    } finally {
      setLoading(false);
    }
  }, [seriesId]);

  // Animation loop: requestAnimationFrame-driven so it pauses correctly
  // when the tab is hidden, and ticks at PLAY_FPS regardless of monitor
  // refresh rate.
  useEffect(() => {
    if (!playing || !manifest) {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      return;
    }
    const loop = (now: number) => {
      const dt = now - lastTickRef.current;
      const interval = 1000 / PLAY_FPS;
      if (dt >= interval) {
        lastTickRef.current = now;
        setFrame((f) => (f + 1) % manifest.frame_count);
      }
      rafRef.current = requestAnimationFrame(loop);
    };
    lastTickRef.current = performance.now();
    rafRef.current = requestAnimationFrame(loop);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [playing, manifest]);

  const angleDeg = manifest ? (frame * 360) / manifest.frame_count : 0;

  // Fetch the sprite PNG with auth once the manifest is available.
  // The blob URL is stable for the lifetime of the manifest; we
  // revoke it on unmount / re-fetch to avoid leaking object URLs.
  useEffect(() => {
    if (!manifest) {
      setSpriteBlobUrl(null);
      return;
    }
    let cancelled = false;
    let createdUrl: string | null = null;
    const ac = new AbortController();
    const token = getStoredToken();
    const headers: Record<string, string> = {};
    if (token) headers.authorization = `Bearer ${token}`;
    const url = petMipApi.spriteUrl(seriesId, {
      frames: DEFAULT_FRAMES,
      height: DEFAULT_HEIGHT,
    });
    fetch(url, { credentials: "include", headers, signal: ac.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setSpriteBlobUrl(createdUrl);
      })
      .catch((e) => {
        if (!cancelled) {
          setErr(e instanceof Error ? e.message : "MIP sprite fetch failed");
        }
      });
    return () => {
      cancelled = true;
      ac.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [manifest, seriesId]);

  return (
    <>
      <h2>{tv("mipTitle")}</h2>
      <p className="meta" style={{ marginTop: "-0.4rem", fontSize: "0.72rem" }}>
        {tv("mipDescription")}
      </p>

      {err && <p className="error">{err}</p>}

      {!manifest && !loading && (
        <button
          type="button"
          className="viewer-btn"
          onClick={generate}
          disabled={loading}
          style={{ width: "100%" }}
        >
          {tv("mipGenerate")}
        </button>
      )}

      {loading && <p className="meta">{tv("mipComputing", { n: DEFAULT_FRAMES })}</p>}

      {manifest && !spriteBlobUrl && !err && <p className="meta">…</p>}

      {manifest && spriteBlobUrl && (
        <>
          <div
            style={{
              width: "100%",
              maxWidth: manifest.frame_width,
              aspectRatio: `${manifest.frame_width} / ${manifest.frame_height}`,
              overflow: "hidden",
              border: "1px solid var(--bv-card-border, #2a2f37)",
              borderRadius: 4,
              background: "#000",
              position: "relative",
              margin: "0.4rem auto 0.5rem",
            }}
          >
            <img
              src={spriteBlobUrl}
              alt={tv("mipFrameAlt", { angle: angleDeg.toFixed(0) })}
              style={{
                position: "absolute",
                top: `-${frame * manifest.frame_height}px`,
                left: 0,
                width: manifest.frame_width,
                height: manifest.frame_height * manifest.frame_count,
                display: "block",
                imageRendering: "pixelated",
              }}
              draggable={false}
            />
            <div
              style={{
                position: "absolute",
                top: 4,
                right: 6,
                fontSize: "0.7rem",
                color: "#ccc",
                background: "rgba(0,0,0,0.4)",
                padding: "0.1rem 0.4rem",
                borderRadius: 3,
                fontFamily: "monospace",
              }}
            >
              {angleDeg.toFixed(0)}°
            </div>
          </div>

          <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
            <button
              type="button"
              className="viewer-btn"
              onClick={() => setPlaying((p) => !p)}
              style={{ minWidth: 64 }}
            >
              {playing ? tv("mipPause") : tv("mipPlay")}
            </button>
            <input
              type="range"
              min={0}
              max={manifest.frame_count - 1}
              value={frame}
              onChange={(e) => {
                setPlaying(false);
                setFrame(Number(e.target.value));
              }}
              style={{ flex: 1 }}
              aria-label={tv("mipAngleAria")}
            />
          </div>

          <p className="meta" style={{ fontSize: "0.7rem", marginTop: "0.4rem" }}>
            {tv("mipWindowSummary", {
              units: manifest.units,
              lo: manifest.suv_window[0].toFixed(1),
              hi: manifest.suv_window[1].toFixed(1),
              n: manifest.frame_count,
            })}
          </p>
        </>
      )}
    </>
  );
}
