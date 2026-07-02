"use client";

/**
 * Drag-and-drop importer for external segmentation files (NIfTI, NRRD).
 *
 * The heavy lifting happens server-side — this component is just the
 * upload form plus a listing of already-imported masks for the current
 * series. Clicking a label in the list loads the binary mask and hands
 * it to ``onMaskLoaded`` so the parent can push it into the volume
 * viewer. Decoupling upload from apply means the viewer can keep using
 * its threshold-based mask path unchanged.
 */

import { useTranslations } from "next-intl";
import { type DragEvent, useCallback, useEffect, useRef, useState } from "react";

import { useModal } from "@/components/ModalHost";
import { ApiError, type SegmentationItem, segmentationsApi } from "@/lib/api";

interface Props {
  seriesId: string;
  onMaskLoaded: (mask: Uint8Array, color: [number, number, number], label: string) => void;
  onMaskCleared: () => void;
  /** Bump to force a re-list — e.g. after the interactive segment tool
   *  (task 3af7a33d) persists a new mask, so it appears here without a
   *  manual refresh. */
  reloadSignal?: number;
}

// Keep `label` in the callback signature so callers that want to name
// the currently-active mask (e.g. for an overlay label) can use it.

const LABEL_PATTERN = /^[a-zA-Z0-9._-]{1,64}$/;

const DEFAULT_COLORS: [number, number, number][] = [
  [1, 0.3, 0.3],
  [0.3, 0.8, 0.4],
  [0.3, 0.5, 1],
  [1, 0.8, 0.2],
  [0.8, 0.3, 0.9],
  [0.2, 0.9, 0.9],
];

function colorForIndex(i: number): [number, number, number] {
  return DEFAULT_COLORS[i % DEFAULT_COLORS.length];
}

export default function SegmentationImporter({
  seriesId,
  onMaskLoaded,
  onMaskCleared,
  reloadSignal,
}: Props) {
  const modal = useModal();
  const tSeg = useTranslations("segmentationImporter");
  const [items, setItems] = useState<SegmentationItem[]>([]);
  const [label, setLabel] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [activeLabel, setActiveLabel] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await segmentationsApi.list(seriesId);
      setItems(res.items);
    } catch (e) {
      // Listing can fail silently — the user can still upload.
      if (e instanceof ApiError && e.status !== 404) setErr(e.message);
    }
  }, [seriesId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Re-list when the parent bumps reloadSignal (a new mask was persisted,
  // e.g. by the interactive segment tool).
  useEffect(() => {
    if (reloadSignal === undefined) return;
    refresh();
  }, [reloadSignal, refresh]);

  const handleFile = useCallback(
    (f: File | null) => {
      setFile(f);
      setErr(null);
      if (f && !label) {
        // Suggest a label from the filename, sanitised.
        const base = f.name.replace(/\.nii(\.gz)?$|\.nrrd$/i, "");
        const safe = base.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 64);
        if (safe) setLabel(safe);
      }
    },
    [label],
  );

  const onDrop = useCallback(
    (ev: DragEvent<HTMLDivElement>) => {
      ev.preventDefault();
      setDragOver(false);
      const f = ev.dataTransfer.files?.[0];
      if (f) handleFile(f);
    },
    [handleFile],
  );

  const submit = useCallback(async () => {
    if (!file) {
      setErr("choose a file first");
      return;
    }
    if (!LABEL_PATTERN.test(label)) {
      setErr("label must be 1-64 chars: letters, digits, . _ -");
      return;
    }
    setUploading(true);
    setErr(null);
    try {
      await segmentationsApi.upload(seriesId, label, file);
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "upload failed");
    } finally {
      setUploading(false);
    }
  }, [file, label, seriesId, refresh]);

  const applyLabel = useCallback(
    async (item: SegmentationItem, idx: number) => {
      try {
        const bytes = await segmentationsApi.fetchMask(seriesId, item.label);
        onMaskLoaded(bytes, colorForIndex(idx), item.label);
        setActiveLabel(item.label);
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "fetch failed");
      }
    },
    [seriesId, onMaskLoaded],
  );

  const removeLabel = useCallback(
    async (item: SegmentationItem) => {
      const ok = await modal.confirm({
        message: `Delete segmentation "${item.label}"?`,
        destructive: true,
        confirmLabel: "Delete",
      });
      if (!ok) return;
      try {
        await segmentationsApi.remove(seriesId, item.label);
        if (activeLabel === item.label) {
          onMaskCleared();
          setActiveLabel(null);
        }
        await refresh();
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "delete failed");
      }
    },
    [modal, seriesId, refresh, activeLabel, onMaskCleared],
  );

  // Auto-segment progress state. ``running`` flips while the worker
  // job is in flight; ``rois`` is the snapshot of which ROIs were
  // requested so the polling loop knows when to stop. Polling
  // backs off once the expected labels have all appeared OR the
  // ceiling timeout (10 min on CPU fast mode) elapses.
  const [autoRunning, setAutoRunning] = useState(false);
  const [autoSummary, setAutoSummary] = useState<string | null>(null);

  const runAutoSegment = useCallback(
    async (rois: string[] | null) => {
      setAutoRunning(true);
      setAutoSummary(null);
      setErr(null);
      const started = Date.now();
      try {
        const resp = await segmentationsApi.autoSegment(seriesId, {
          roi_subset: rois ?? undefined,
        });
        const expected = new Set<string>(resp.rois ?? rois ?? []);
        // Poll the listing every 5s. CPU TotalSegmentator on a
        // typical CT abdomen takes 5-15 min in fast mode; we cap
        // at 20 min and surface a "still running, check back later"
        // message rather than wait forever.
        const TIMEOUT_MS = 20 * 60 * 1000;
        const POLL_MS = 5000;
        // eslint-disable-next-line no-constant-condition
        while (true) {
          await new Promise((r) => setTimeout(r, POLL_MS));
          const list = await segmentationsApi.list(seriesId);
          const got = new Set(list.items.map((i) => i.label));
          // When ROIs were specified explicitly we wait for all of
          // them; otherwise we wait until at least one mask appears
          // and then return the current snapshot — first arrival
          // tells the user the engine is alive.
          if (expected.size > 0) {
            const allPresent = [...expected].every((r) => got.has(r));
            if (allPresent) {
              setAutoSummary(
                tSeg("completed", {
                  n: expected.size,
                  seconds: ((Date.now() - started) / 1000).toFixed(0),
                }),
              );
              await refresh();
              break;
            }
          } else if (got.size > 0) {
            setAutoSummary(
              tSeg("inProgress", {
                n: got.size,
                seconds: ((Date.now() - started) / 1000).toFixed(0),
              }),
            );
            await refresh();
            await new Promise((r) => setTimeout(r, POLL_MS));
            const final = await segmentationsApi.list(seriesId);
            await refresh();
            setAutoSummary(
              tSeg("completed", {
                n: final.items.length,
                seconds: ((Date.now() - started) / 1000).toFixed(0),
              }),
            );
            break;
          }
          if (Date.now() - started > TIMEOUT_MS) {
            setAutoSummary(tSeg("timeoutHint"));
            break;
          }
        }
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : "auto-segment failed");
      } finally {
        setAutoRunning(false);
      }
    },
    [seriesId, refresh, tSeg],
  );

  // Auto-segment UI is feature-flagged off by default. The CPU
  // TotalSegmentator pipeline is too slow + opaque to expose to
  // end users right now (5-30 min per scan, no ETA, no cancel,
  // first-run weight download can stall). Backend route + worker
  // task stay live so curl-driven workflows keep working; flip
  // ``NEXT_PUBLIC_ENABLE_AUTO_SEGMENT=true`` at build time once
  // the engine path is reliable + i18n + ETA + cancel are wired.
  const autoSegmentEnabled = process.env.NEXT_PUBLIC_ENABLE_AUTO_SEGMENT === "true";
  // Touch the run handler so the linter doesn't flag it as unused
  // while the panel is hidden — re-enabling the feature should be
  // a single toggle, not a hunt for dead state.
  void runAutoSegment;
  void autoRunning;
  void autoSummary;

  return (
    <>
      {autoSegmentEnabled && (
        <>
          <h2>Auto-segment (TotalSegmentator)</h2>
          <div
            className="card"
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.4rem",
              marginBottom: "0.5rem",
            }}
          >
            <p className="meta" style={{ fontSize: "0.7rem", marginTop: 0, marginBottom: 0 }}>
              CPU pipeline: 5-15 min per abdominal scan. Produced ROIs appear in the import list
              below as they are saved.
            </p>
            <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
              <button
                type="button"
                className="viewer-btn"
                style={{ fontSize: "0.7rem" }}
                onClick={() => runAutoSegment(["liver"])}
                disabled={autoRunning}
              >
                Liver
              </button>
              <button
                type="button"
                className="viewer-btn"
                style={{ fontSize: "0.7rem" }}
                onClick={() =>
                  runAutoSegment(["liver", "kidney_right", "kidney_left", "spleen", "pancreas"])
                }
                disabled={autoRunning}
              >
                Abdomen
              </button>
              <button
                type="button"
                className="viewer-btn"
                style={{ fontSize: "0.7rem" }}
                onClick={() => runAutoSegment(null)}
                disabled={autoRunning}
              >
                All
              </button>
              {autoRunning && (
                <span className="meta" style={{ fontSize: "0.7rem" }}>
                  running...
                </span>
              )}
            </div>
            {autoSummary && (
              <p
                className="meta"
                style={{
                  fontSize: "0.7rem",
                  marginBottom: 0,
                  color: "#6ad19a",
                }}
              >
                {autoSummary}
              </p>
            )}
          </div>
        </>
      )}

      <h2>Import segmentation</h2>
      <div className="card" style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") fileInputRef.current?.click();
          }}
          // biome-ignore lint/a11y/useSemanticElements: drop-zone hosts the file <input>; promoting the wrapper to <button> would break the input's drag-target and click semantics.
          role="button"
          tabIndex={0}
          style={{
            border: `1px dashed ${dragOver ? "#e96b1f" : "#444"}`,
            borderRadius: 4,
            padding: "0.8rem",
            textAlign: "center",
            cursor: "pointer",
            fontSize: "0.75rem",
            color: dragOver ? "#e96b1f" : "#aaa",
            background: dragOver ? "rgba(233,107,31,0.08)" : "transparent",
          }}
        >
          {file ? (
            <>
              <div style={{ color: "#ddd" }}>{file.name}</div>
              <div className="meta" style={{ fontSize: "0.65rem" }}>
                {(file.size / 1_048_576).toFixed(2)} MiB
              </div>
            </>
          ) : (
            <>drop .nii / .nii.gz / .nrrd here, or click to browse</>
          )}
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept=".nii,.nii.gz,.nrrd"
          style={{ display: "none" }}
          onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
        />
        <label className="meta" style={{ display: "block", fontSize: "0.7rem" }}>
          Label
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g. tumor"
            style={{
              width: "100%",
              marginTop: "0.2rem",
              background: "#111",
              border: "1px solid #444",
              color: "#ddd",
              padding: "0.25rem 0.4rem",
              fontSize: "0.75rem",
            }}
          />
        </label>
        <div style={{ display: "flex", gap: "0.4rem" }}>
          <button
            type="button"
            className="viewer-btn"
            onClick={submit}
            disabled={uploading || !file || !label}
            style={{ opacity: uploading || !file || !label ? 0.5 : 1 }}
          >
            {uploading ? "Uploading…" : "Upload"}
          </button>
          {file && (
            <button
              type="button"
              className="viewer-btn"
              onClick={() => {
                setFile(null);
                if (fileInputRef.current) fileInputRef.current.value = "";
              }}
            >
              Clear
            </button>
          )}
        </div>
        {err && <div style={{ fontSize: "0.7rem", color: "#f88" }}>{err}</div>}
      </div>

      {items.length > 0 && (
        <>
          <h2>Imported segmentations ({items.length})</h2>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.2rem" }}>
            {items.map((item, i) => (
              <div
                key={item.label}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.4rem",
                  padding: "0.25rem 0.35rem",
                  background: activeLabel === item.label ? "#1c2433" : "transparent",
                  border: "1px solid #2a2f3b",
                  borderRadius: 3,
                }}
              >
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    background: `rgb(${colorForIndex(i)
                      .map((c) => Math.round(c * 255))
                      .join(",")})`,
                  }}
                />
                <span style={{ flex: 1, fontSize: "0.75rem", color: "#ddd" }}>{item.label}</span>
                <span className="meta" style={{ fontSize: "0.6rem" }}>
                  {(item.size_bytes / 1024).toFixed(0)} KiB
                </span>
                <button
                  type="button"
                  className="viewer-btn"
                  style={{ fontSize: "0.65rem", padding: "0.1rem 0.35rem" }}
                  onClick={() => applyLabel(item, i)}
                >
                  {activeLabel === item.label ? "Active" : "Apply"}
                </button>
                <button
                  type="button"
                  onClick={() => removeLabel(item)}
                  style={{
                    background: "none",
                    border: "none",
                    color: "#f66",
                    cursor: "pointer",
                    fontSize: "0.8rem",
                    padding: "0 4px",
                  }}
                  title="Delete"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
          {activeLabel && (
            <button
              type="button"
              className="viewer-btn"
              style={{ marginTop: "0.3rem" }}
              onClick={() => {
                onMaskCleared();
                setActiveLabel(null);
              }}
            >
              Clear active mask
            </button>
          )}
        </>
      )}
    </>
  );
}
