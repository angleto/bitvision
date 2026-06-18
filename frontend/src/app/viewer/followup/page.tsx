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
import { type PrimaryPlane, seriesOptionLabel } from "@/lib/seriesMatch";
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

// Resolves the SIBLING series of the study a pane is showing, so the pane can
// offer a series selector. Keyed on the study (not the series) so switching
// series within the same study does not refetch the list. Acquisition plane is
// resolved per series for medical labeling/matching (progressive; a per-series
// failure degrades to "unknown" rather than blocking the picker).
function useStudySeries(seriesId: string | null): {
  list: Series[];
  planeOf: (id: string) => PrimaryPlane | null;
} {
  const [studyId, setStudyId] = useState<string | null>(null);
  const [list, setList] = useState<Series[]>([]);
  const [planes, setPlanes] = useState<Record<string, PrimaryPlane>>({});

  useEffect(() => {
    if (!seriesId) return;
    let cancelled = false;
    void (async () => {
      try {
        const s = await studiesApi.series(seriesId);
        if (!cancelled) setStudyId(s.study_id);
      } catch {
        /* the pane surfaces its own load error */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [seriesId]);

  useEffect(() => {
    if (!studyId) return;
    let cancelled = false;
    void (async () => {
      try {
        const detail = await studiesApi.detail(studyId);
        if (cancelled) return;
        setList(detail.series);
        const entries = await Promise.all(
          detail.series.map(async (s) => {
            try {
              const dm = await studiesApi.displayMetadata(s.id);
              return [s.id, dm.primary_plane] as const;
            } catch {
              return [s.id, "unknown" as PrimaryPlane] as const;
            }
          }),
        );
        if (!cancelled) setPlanes(Object.fromEntries(entries));
      } catch {
        /* selector just won't populate */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [studyId]);

  return { list, planeOf: (id) => planes[id] ?? null };
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
  const [baselineId, setBaselineId] = useState<string | null>(
    () => search.get("baseline") ?? search.getAll("s")[0] ?? null,
  );
  const [followupId, setFollowupId] = useState<string | null>(
    () => search.get("followup") ?? search.getAll("s")[1] ?? null,
  );

  const base = useSeriesPane(baselineId);
  const follow = useSeriesPane(followupId);
  const baseStudy = useStudySeries(baselineId);
  const followStudy = useStudySeries(followupId);

  const baseRef = useRef<MPRLayoutHandle | null>(null);
  const followRef = useRef<MPRLayoutHandle | null>(null);
  const syncingRef = useRef(false);

  const [sync, setSync] = useState(true);
  const [matrix, setMatrix] = useState<Mat4 | null>(null);
  const [aligning, setAligning] = useState(false);
  const [alignErr, setAlignErr] = useState<string | null>(null);

  // Keep the URL in sync (shareable / survives refresh) without remounting:
  // after load, component state — not the URL — is the source of truth.
  function syncUrl(b: string | null, f: string | null) {
    if (typeof window === "undefined") return;
    const qs = new URLSearchParams();
    if (b) qs.set("baseline", b);
    if (f) qs.set("followup", f);
    window.history.replaceState(null, "", `${window.location.pathname}?${qs.toString()}`);
  }
  // Picking a different series on either side invalidates the previous rigid
  // registration, so drop the matrix; the pane reloads and sameFoR / Align
  // recompute for the new pair.
  function pickBaseline(id: string) {
    if (!id || id === baselineId) return;
    setBaselineId(id);
    setMatrix(null);
    setAlignErr(null);
    syncUrl(id, followupId);
  }
  function pickFollowup(id: string) {
    if (!id || id === followupId) return;
    setFollowupId(id);
    setMatrix(null);
    setAlignErr(null);
    syncUrl(baselineId, id);
  }
  function planeLabel(p: PrimaryPlane | null): string | null {
    return p && p !== "unknown" ? t(`plane.${p}`) : null;
  }

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
            study: baseStudy,
            selectedId: baselineId,
            onPick: pickBaseline,
          },
          {
            pane: follow,
            ref: followRef,
            label: t("followup"),
            other: baseRef,
            map: mapFollowToBase,
            study: followStudy,
            selectedId: followupId,
            onPick: pickFollowup,
          },
        ].map(({ pane, ref, label, other, map, study, selectedId, onPick }) => (
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
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "0.25rem 0.5rem",
                fontSize: "0.78rem",
                color: "#cbd5e1",
                background: "#0b0e13",
                borderBottom: "1px solid #1a1f2b",
              }}
            >
              <span style={{ color: "#94a3b8", whiteSpace: "nowrap" }}>{label}</span>
              {study.list.length > 0 ? (
                <select
                  aria-label={t("selectSeries")}
                  value={selectedId ?? ""}
                  onChange={(e) => onPick(e.target.value)}
                  style={{
                    flex: 1,
                    minWidth: 0,
                    background: "#0b0e13",
                    color: "#e6ecf3",
                    border: "1px solid #1a1f2b",
                    borderRadius: 6,
                    padding: "2px 6px",
                    fontSize: "0.76rem",
                  }}
                >
                  {study.list.map((s) => (
                    <option key={s.id} value={s.id}>
                      {seriesOptionLabel(s, planeLabel(study.planeOf(s.id)))}
                    </option>
                  ))}
                </select>
              ) : (
                <span
                  style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                >
                  {pane.series?.series_description ?? "…"}
                </span>
              )}
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
