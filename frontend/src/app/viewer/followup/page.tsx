"use client";

// Follow-up comparison viewer: baseline + follow-up side by side with a
// registration-aware synchronised crosshair. When the two studies share a
// frame of reference, the crosshair maps directly (world→world); otherwise
// the user runs "Align" (a rigid registration) and the stored 4x4 LPS
// matrix maps the world point across frames so both panes track the same
// anatomy. Reuses ``CornerstoneMPRLayout`` (one ref per pane).

import type { MPRLayoutHandle } from "@/components/MPRLayoutTypes";
import type { VolumeData } from "@/components/VolumeViewer";
import { type Mat4, type Vec3, applyAffine, invertAffine, isMat4 } from "@/lib/affine";
import { ApiError, type Series, fetchVolume, registrationsApi, studiesApi } from "@/lib/api";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";

const CornerstoneMPRLayout = dynamic(() => import("@/components/CornerstoneMPRLayout"), {
  ssr: false,
});

interface PaneState {
  seriesId: string;
  series: Series | null;
  volume: VolumeData | null;
  err: string | null;
}

function useSeriesPane(seriesId: string | null): PaneState {
  const [pane, setPane] = useState<PaneState>({
    seriesId: seriesId ?? "",
    series: null,
    volume: null,
    err: null,
  });
  useEffect(() => {
    if (!seriesId) return;
    let cancelled = false;
    setPane({ seriesId, series: null, volume: null, err: null });
    void (async () => {
      try {
        const series = await studiesApi.series(seriesId);
        const { header, scalars } = await fetchVolume(seriesId);
        if (cancelled) return;
        setPane({
          seriesId,
          series,
          err: null,
          volume: {
            dimensions: [header.nx, header.ny, header.nz],
            spacing: header.spacing,
            scalars,
            range: header.valueRange,
            origin: header.origin,
            direction: header.direction,
            frameOfReferenceUid: header.frameOfReferenceUid,
          },
        });
      } catch (e) {
        if (!cancelled) {
          setPane((p) => ({ ...p, err: e instanceof ApiError ? e.message : String(e) }));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [seriesId]);
  return pane;
}

export default function FollowupComparePage() {
  return (
    <Suspense
      fallback={
        <main>
          <p className="meta">Loading…</p>
        </main>
      }
    >
      <FollowupCompareInner />
    </Suspense>
  );
}

function FollowupCompareInner() {
  const t = useTranslations("compare");
  const search = useSearchParams();
  const baselineId = search.get("baseline") ?? search.getAll("s")[0] ?? null;
  const followupId = search.get("followup") ?? search.getAll("s")[1] ?? null;

  const base = useSeriesPane(baselineId);
  const follow = useSeriesPane(followupId);

  const baseRef = useRef<MPRLayoutHandle | null>(null);
  const followRef = useRef<MPRLayoutHandle | null>(null);
  const syncingRef = useRef(false);

  const [sync, setSync] = useState(true);
  const [matrix, setMatrix] = useState<Mat4 | null>(null);
  const [aligning, setAligning] = useState(false);
  const [alignErr, setAlignErr] = useState<string | null>(null);

  const sameFoR = useMemo(() => {
    const a = base.volume?.frameOfReferenceUid;
    const b = follow.volume?.frameOfReferenceUid;
    return !!a && !!b && a === b;
  }, [base.volume, follow.volume]);

  const invMatrix = useMemo(() => (matrix ? invertAffine(matrix) : null), [matrix]);

  // matrix maps baseline(fixed) world → follow-up(moving) world.
  function mapBaseToFollow(w: Vec3): Vec3 {
    if (sameFoR || !matrix) return w;
    return applyAffine(matrix, w);
  }
  function mapFollowToBase(w: Vec3): Vec3 {
    if (sameFoR || !invMatrix) return w;
    return applyAffine(invMatrix, w);
  }

  function pushSync(
    fromRef: typeof baseRef,
    toRef: typeof baseRef,
    pos: [number, number, number],
    map: (w: Vec3) => Vec3,
  ) {
    if (!sync || syncingRef.current) return;
    const from = fromRef.current;
    const to = toRef.current;
    if (!from?.getCrosshairWorld || !to?.setCrosshairWorld) return;
    const w = from.getCrosshairWorld(pos);
    if (!w) return;
    syncingRef.current = true;
    to.setCrosshairWorld(map(w));
    // Release the echo guard on the next frame.
    requestAnimationFrame(() => {
      syncingRef.current = false;
    });
  }

  async function align() {
    if (!baselineId || !followupId) return;
    setAligning(true);
    setAlignErr(null);
    try {
      const reg = await registrationsApi.create({
        fixed_series_id: baselineId,
        moving_series_id: followupId,
        kind: "rigid",
      });
      // Poll until terminal (registration runs on a worker).
      for (let i = 0; i < 60; i++) {
        const cur = await registrationsApi.get(reg.id);
        if (cur.status === "succeeded") {
          const m = cur.result_meta?.lps_matrix;
          if (isMat4(m)) setMatrix(m);
          else setAlignErr(t("noMatrix"));
          return;
        }
        if (cur.status === "failed" || cur.status === "cancelled") {
          setAlignErr(cur.error ?? t("alignFailed"));
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      setAlignErr(t("alignTimeout"));
    } catch (e) {
      setAlignErr(e instanceof ApiError ? e.message : t("alignFailed"));
    } finally {
      setAligning(false);
    }
  }

  if (!baselineId || !followupId) {
    return (
      <main style={{ padding: "2rem" }}>
        <Link href="/studies">← studies</Link>
        <h1>{t("title")}</h1>
        <p className="meta">{t("pickHint")}</p>
      </main>
    );
  }

  const alignState = sameFoR ? "sameFrame" : matrix ? "registered" : "unaligned";
  const alignColor =
    alignState === "unaligned" ? "var(--bv-warning, #b45309)" : "var(--bv-success, #047857)";

  return (
    <div
      style={{
        height: "calc(100vh - 56px)",
        background: "#000",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          flex: "0 0 auto",
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          padding: "0.4rem 0.75rem",
          background: "var(--bv-card-bg, #11151c)",
          color: "var(--bv-fg, #e6ecf3)",
          borderBottom: "1px solid var(--bv-card-border, #1a1f2b)",
          flexWrap: "wrap",
        }}
      >
        <strong style={{ fontSize: "0.9rem" }}>{t("title")}</strong>
        <label
          style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.82rem" }}
        >
          <input type="checkbox" checked={sync} onChange={(e) => setSync(e.target.checked)} />
          {t("sync")}
        </label>
        <span
          className="badge"
          style={{ color: alignColor, borderColor: alignColor, whiteSpace: "nowrap" }}
        >
          {t(`align.${alignState}`)}
        </span>
        {!sameFoR && (
          <button type="button" className="ghost" disabled={aligning} onClick={align}>
            {aligning ? t("aligning") : t("alignButton")}
          </button>
        )}
        {alignErr && <span style={{ color: "var(--bv-danger, #f87171)" }}>{alignErr}</span>}
      </div>

      <div style={{ flex: "1 1 auto", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2 }}>
        {[
          {
            pane: base,
            ref: baseRef,
            label: t("baseline"),
            other: followRef,
            map: mapBaseToFollow,
          },
          {
            pane: follow,
            ref: followRef,
            label: t("followup"),
            other: baseRef,
            map: mapFollowToBase,
          },
        ].map(({ pane, ref, label, other, map }) => (
          <div
            key={label}
            style={{
              position: "relative",
              background: "#000",
              border: "1px solid #1a1f2b",
              display: "grid",
              gridTemplateRows: "auto 1fr",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "0.25rem 0.5rem",
                fontSize: "0.78rem",
                color: "#cbd5e1",
                background: "#0b0e13",
                borderBottom: "1px solid #1a1f2b",
              }}
            >
              {label}
              {pane.series?.series_description ? ` · ${pane.series.series_description}` : ""}
            </div>
            {pane.err ? (
              <div className="error" style={{ padding: "1rem" }}>
                {pane.err}
              </div>
            ) : pane.volume ? (
              <CornerstoneMPRLayout
                ref={ref}
                volume={pane.volume}
                showAxial
                showSagittal
                showCoronal
                show3D={false}
                layout="2x2"
                seriesId={pane.seriesId}
                modality={pane.series?.modality ?? null}
                seriesDescription={pane.series?.series_description ?? undefined}
                activeTool="wl"
                onCrosshairChange={(pos) => pushSync(ref, other, pos, map)}
              />
            ) : (
              <div style={{ color: "#6b7280", padding: "1rem" }}>loading…</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
