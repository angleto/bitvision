"use client";

// Unified annotation list panel.
//
// Aggregates every "marker" attached to the active study — measurements,
// 3D fiducials, in-image text labels, and clinical notes. Notes can be
// either plain text (no spatial anchor) or viewer-anchored ({x, y, z}
// stored in ``ClinicalNote.anchor``); the latter group by slice
// alongside real markers, the former end up in the "across slices"
// bucket. Per-row actions: jump-to, edit (notes only), pin/unpin
// (notes only), delete.
//
// Rows are grouped by slice so a radiologist scrolling a CT can
// quickly locate "what did I leave on slice 47". 3D fiducials live
// in their own "across slices" group at the bottom.
//
// A small inline form at the top lets the user add a plain text note
// on the active study without leaving the viewer; viewer-anchored
// note creation continues to live in the image overlay.
//
// JSON / DICOM SR import-export buttons live at the top — see
// services/markers_sr.py for the underlying conversion.

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useModal } from "@/components/ModalHost";
import {
  ApiError,
  type ClinicalNote,
  type Marker,
  type MarkerKind,
  getStoredToken,
  markersApi,
  patientsApi,
} from "@/lib/api";

interface Props {
  patientId: string;
  studyId: string;
  /** When provided, the panel scopes its data to the active series:
   * markers/notes shown are those targeting the study itself OR any
   * of these series (rows on *other* series of the same study are
   * filtered out). The first id is treated as the primary series for
   * note-creation: new notes use ``target_kind=series`` and
   * ``target_id=seriesIds[0]`` so their provenance is unambiguous.
   * Additional ids cover overlays the user is actively viewing
   * (e.g. the PET fusion series of a PET-CT deep link) so that
   * markers anchored to the overlay still surface in the side panel.
   * Omitting it falls back to study-only scope. */
  seriesIds?: string[];
  /** Callback fired when the user clicks "jump to" on a row. The
   * parent moves the viewport crosshair to this voxel. v3.5.1 added
   * the optional ``markerId`` (server-side row id, present whenever
   * the row comes from a persisted marker — i.e. not from a
   * client-side reading note) so the parent can highlight the
   * matching outline on the canvas. v3.6 adds ``sourceSeriesId`` so
   * the parent can round-trip the voxel through world coords when
   * the marker lives on a series different from the MPR primary
   * (PT-on-CT fusion review): without it the IJK gets interpreted
   * in the wrong volume's index space and the crosshair lands
   * outside the primary's bounds, blacking out sag/cor. */
  onJumpTo: (
    voxel: [number, number, number],
    markerId?: string,
    sourceSeriesId?: string | null,
  ) => void;
  /** Fired after every successful refresh with the persisted markers
   * the panel knows about. The viewer parent feeds this list into
   * ``CornerstoneMPRLayout``'s ``overlayMarkers`` prop so the SVG
   * overlay can draw bboxes / fiducials / text-overlays that
   * Cornerstone itself does not render. Notes are excluded — they
   * have no spatial geometry beyond the optional voxel anchor and
   * the SVG overlay would have nothing to render. */
  onMarkersLoaded?: (markers: Marker[]) => void;
  /** Server-side marker id that should appear "selected" in the row
   * list (matching outline class). Null clears the highlight. */
  focusedMarkerId?: string | null;
  /** Optional refresh trigger from the parent — bump to reload. */
  refreshKey?: number;
  /** Fired with the server-side marker id after a successful delete.
   * The viewer parent uses this to drop the matching Cornerstone
   * annotation overlay (otherwise the SVG persists on the canvas) and
   * filter the in-memory measurements list (otherwise the next sync
   * pass restores the entry from the cs annotation state). */
  onMarkerDeleted?: (markerId: string) => void;
  /** Fired after the user edits a marker label inline (panel) and
   * the server PATCH succeeds. The viewer mirrors the new label into
   * the Cornerstone annotation (so the on-canvas overlay updates)
   * and into ``allMeasurements`` (so React state stays consistent
   * across re-renders / report-composer). */
  onMarkerLabelChanged?: (markerId: string, label: string) => void;
}

// One row in the list — abstract over real Marker rows + ClinicalNote
// reading-notes that we surface here for unified UX.
interface Row {
  id: string;
  source: "marker" | "note";
  kind: MarkerKind | "reading-note";
  /** Whether the underlying entity targets the whole study or a
   * specific series. The panel shows a tiny scope badge so the user
   * never confuses a study-wide remark with a series-bound finding. */
  targetScope: "study" | "series";
  /** Series id the row's ``voxel`` is expressed in. Equals the
   * marker/note's ``target_id`` when ``target_kind="series"``; null
   * when the entity is study-wide. The viewer parent uses it to
   * round-trip IJK → world → primary-IJK on jump-to so the crosshair
   * lands on the right anatomical point even when the row lives on
   * the PT overlay while the MPR primary is the CT (or vice versa). */
  sourceSeriesId: string | null;
  z: number | null; // axial slice index, or null for 3D / unknown
  voxel: [number, number, number] | null;
  summary: string;
  /** User-supplied label (marker.body trimmed, or the note body for
   * notes). Held alongside ``summary`` so the inline edit dialog can
   * pre-fill the input without re-parsing the summary string. */
  body: string | null;
  raw: Marker | ClinicalNote;
}

const ICONS: Record<string, string> = {
  "measurement.distance": "📏",
  "measurement.angle": "📐",
  "measurement.area": "▱",
  "measurement.ellipse": "◯",
  "measurement.freehand": "✎",
  "measurement.arrow": "➤",
  "measurement.text": "T",
  "measurement.probe": "·",
  "measurement.bbox": "▭",
  "bbox.lesion": "⬚",
  fiducial: "🔘",
  "reading-note": "📝",
  "text-overlay": "T",
};

function markerRow(m: Marker, tKind: (key: string) => string): Row {
  // For 2D measurements, the geometry is {axis, points: [[x,y,z]...]}.
  // For text-overlay and fiducial, we look at "anchor" / "point".
  const g = m.geometry as {
    points?: number[][];
    anchor?: number[];
    point?: number[];
    min_ijk?: number[];
    max_ijk?: number[];
  } | null;
  let voxel: [number, number, number] | null = null;
  const asTriple = (raw: unknown[] | undefined): [number, number, number] | null => {
    if (!raw || raw.length < 3) return null;
    const t: [number, number, number] = [Number(raw[0]), Number(raw[1]), Number(raw[2])];
    if (!Number.isFinite(t[0]) || !Number.isFinite(t[1]) || !Number.isFinite(t[2])) return null;
    return t;
  };
  if (g?.points && g.points.length > 0) {
    voxel = asTriple(g.points[0]);
  } else if (g?.anchor) {
    voxel = asTriple(g.anchor);
  } else if (g?.point) {
    voxel = asTriple(g.point);
  } else if (g?.min_ijk && g?.max_ijk) {
    // bbox.lesion — jump to the centroid so click-to-locate lands
    // inside the box instead of on one of its corners.
    const mn = asTriple(g.min_ijk);
    const mx = asTriple(g.max_ijk);
    if (mn && mx) {
      voxel = [
        Math.round((mn[0] + mx[0]) / 2),
        Math.round((mn[1] + mx[1]) / 2),
        Math.round((mn[2] + mx[2]) / 2),
      ];
    }
  }

  let valuePart: string | null = null;
  if (m.computed && typeof m.computed === "object") {
    const v = (m.computed as { value?: unknown; unit?: unknown }).value;
    const u = (m.computed as { value?: unknown; unit?: unknown }).unit;
    if (v != null) valuePart = `${v}${u ? ` ${u}` : ""}`;
  }
  const trimmedBody = typeof m.body === "string" ? m.body.trim() : "";
  const labelPart = trimmedBody.length > 0 ? trimmedBody : null;
  // Label first (the user-supplied free text), then a separator and
  // the auto-computed measurement. If neither is present the row
  // falls back to the marker kind so it's still identifiable.
  // Translated kind label, used both for the value-only branch
  // (prepended for context) and as the last-resort fallback. Falls
  // back to the cleaned-up ``measurement.distance`` → ``distance``
  // form when no translation is registered, never to the raw key
  // (which leaked through as ``measurement.distance`` in the UI).
  const kindKey = m.kind.replace(/[.-]/g, "_");
  const translatedKind = tKind(kindKey);
  const kindLabel =
    translatedKind === kindKey ? m.kind.replace(/^measurement\./, "") : translatedKind;
  let summary = "";
  if (labelPart && valuePart) {
    const lp = labelPart.length > 50 ? `${labelPart.slice(0, 50)}…` : labelPart;
    summary = `${lp} · ${valuePart}`;
  } else if (labelPart) {
    summary = labelPart.length > 60 ? `${labelPart.slice(0, 60)}…` : labelPart;
  } else if (valuePart) {
    summary = `${kindLabel}: ${valuePart}`;
  } else {
    summary = kindLabel;
  }

  return {
    id: m.id,
    source: "marker",
    kind: m.kind,
    targetScope: m.target_kind === "series" ? "series" : "study",
    sourceSeriesId: m.target_kind === "series" ? m.target_id : null,
    z: m.kind === "fiducial" ? null : (voxel?.[2] ?? null),
    voxel,
    summary,
    body: labelPart,
    raw: m,
  };
}

function noteRow(n: ClinicalNote): Row {
  // ``anchor`` is the canonical voxel reference (migration 0039 lifted
  // it out of a ``[crosshair X:Y:Z]`` body prefix). When absent the
  // note is "across slices" — a plain text remark on the study.
  const voxel: [number, number, number] | null = n.anchor
    ? [n.anchor.x, n.anchor.y, n.anchor.z]
    : null;
  const summary = n.body.length > 60 ? `${n.body.slice(0, 60)}…` : n.body;
  return {
    id: `note:${n.id}`,
    source: "note",
    kind: "reading-note",
    targetScope: n.target_kind === "series" ? "series" : "study",
    sourceSeriesId: n.target_kind === "series" ? n.target_id : null,
    z: voxel ? voxel[2] : null,
    voxel,
    summary,
    body: n.body,
    raw: n,
  };
}

type FilterKey = "all" | "measurements" | "text" | "fiducials" | "notes";

// Hoisted (pure function) so its identity is stable across renders;
// the useMemo hooks below can keep their dep arrays minimal.
function groupBySlice(items: Row[]): Array<[string, Row[]]> {
  const buckets: Map<string, Row[]> = new Map();
  for (const r of items) {
    const key = r.z == null ? "__across__" : `slice:${r.z}`;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(r);
    else buckets.set(key, [r]);
  }
  return Array.from(buckets.entries());
}

function filterMatches(row: Row, f: FilterKey): boolean {
  if (f === "all") return true;
  if (f === "measurements") return row.kind.startsWith("measurement.");
  if (f === "text") return row.kind === "measurement.text" || row.kind === "text-overlay";
  if (f === "fiducials") return row.kind === "fiducial";
  if (f === "notes") return row.source === "note";
  return true;
}

export default function MarkerListPanel({
  patientId,
  studyId,
  seriesIds,
  onJumpTo,
  refreshKey,
  onMarkerDeleted,
  onMarkerLabelChanged,
  onMarkersLoaded,
  focusedMarkerId,
}: Props) {
  // Stable, deduplicated list of series ids the panel queries. The
  // first entry is the "primary": used as the default target_id for
  // newly-created notes. Subsequent entries are overlays (e.g. the
  // PET fusion series of a PET-CT view) so that markers anchored to
  // any visible series surface in the list.
  const activeSeriesIds = useMemo(
    () => Array.from(new Set((seriesIds ?? []).filter((s): s is string => !!s))),
    [seriesIds],
  );
  const primarySeriesId = activeSeriesIds[0] ?? null;
  const t = useTranslations("markerList");
  const tNotes = useTranslations("notesPanel");
  const tEdit = useTranslations("markerEdit");
  const tKind = useTranslations("markerKinds");
  const modal = useModal();
  const [rows, setRows] = useState<Row[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [busy, setBusy] = useState(false);
  const [newNoteBody, setNewNoteBody] = useState("");
  const [creatingNote, setCreatingNote] = useState(false);
  // Inline-editor state: which note row is being edited + its draft body.
  const [editingNoteId, setEditingNoteId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      // Fetch study-level + per-active-series rows in parallel and
      // merge. Without the series scope the panel would show
      // measurements/notes from unrelated series of the same study,
      // which is confusing for the radiologist who's looking at one
      // specific series. With a fusion overlay the panel queries
      // BOTH the primary and the overlay series so markers anchored
      // to the overlay (e.g. PET hot-spots written by an MCP agent
      // on a CT+PET-fusion view) still surface here.
      const studyMarkersJob = markersApi.list(patientId, {
        target_kind: "study",
        target_id: studyId,
      });
      const studyNotesJob = patientsApi.listNotes(patientId, {
        target_kind: "study",
        target_id: studyId,
      });
      const perSeriesJobs = activeSeriesIds.map((sid) =>
        Promise.all([
          markersApi.list(patientId, { target_kind: "series", target_id: sid }),
          patientsApi.listNotes(patientId, { target_kind: "series", target_id: sid }),
        ]),
      );
      const [studyMarkers, studyNotes, perSeries] = await Promise.all([
        studyMarkersJob,
        studyNotesJob,
        Promise.all(perSeriesJobs),
      ]);
      const seriesMarkers = perSeries.flatMap(([m]) => m);
      const seriesNotes = perSeries.flatMap(([, n]) => n);
      // Deduplicate by id: a marker targeting the study is fetched
      // once via the study query; the per-series queries are scoped
      // to ``target_kind=series`` so they can't collide, but if the
      // backend ever broadens that we don't want the same row twice.
      const seenMarkerIds = new Set<string>();
      const mergedMarkers = [...studyMarkers, ...seriesMarkers].filter((m) => {
        if (seenMarkerIds.has(m.id)) return false;
        seenMarkerIds.add(m.id);
        return true;
      });
      const seenNoteIds = new Set<string>();
      const mergedNotes = [...studyNotes, ...seriesNotes].filter((n) => {
        if (seenNoteIds.has(n.id)) return false;
        seenNoteIds.add(n.id);
        return true;
      });
      const out: Row[] = mergedMarkers.map((m) => markerRow(m, tKind));
      for (const n of mergedNotes) {
        out.push(noteRow(n));
      }
      // Filter the panel to only what the user can actually act on.
      // Pre-fix Cornerstone emit produced markers with empty
      // ``geometry.points`` and a generic ``body``-less row, which
      // showed up as un-navigable un-labelled "across slices"
      // entries — useless and confusing. A legitimate measurement
      // always carries a spatial anchor; without one the row can't
      // be jumped-to and the panel hides it.
      const cleaned = out.filter((r) => {
        if (r.source === "note") return true; // plain-text notes are fine
        if (r.voxel) return true; // navigable
        if (r.kind === "fiducial") return true; // 3D fiducials live elsewhere
        return false;
      });
      out.splice(0, out.length, ...cleaned);
      // Stable sort: by slice asc, then by created_at desc within slice
      out.sort((a, b) => {
        const za = a.z ?? Number.POSITIVE_INFINITY;
        const zb = b.z ?? Number.POSITIVE_INFINITY;
        if (za !== zb) return za - zb;
        const ta = (a.raw as { created_at?: string }).created_at ?? "";
        const tb = (b.raw as { created_at?: string }).created_at ?? "";
        return tb.localeCompare(ta);
      });
      setRows(out);
      // Lift the persisted markers up to the parent so the SVG
      // overlay on the viewport can render the ones Cornerstone does
      // not draw itself (bbox.lesion / fiducial / text-overlay).
      onMarkersLoaded?.(mergedMarkers);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("loadFailed"));
    }
  }, [patientId, studyId, activeSeriesIds, t, tKind, onMarkersLoaded]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: ``refreshKey`` is the explicit refetch trigger bumped after annotation mutations.
  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  // Measurements always render in their own section above the
  // "Annotazioni" header (radiologist requirement: measurements
  // first, free-text / fiducials below). The legacy filter still
  // exists but only narrows the annotations section.
  const measurementVisible = useMemo(
    () => rows.filter((r) => r.kind.startsWith("measurement.")),
    [rows],
  );
  const annotationVisible = useMemo(
    () =>
      rows
        .filter((r) => !r.kind.startsWith("measurement."))
        .filter((r) => filterMatches(r, filter)),
    [rows, filter],
  );
  const visible = useMemo(
    () => [...measurementVisible, ...annotationVisible],
    [measurementVisible, annotationVisible],
  );

  const groupedMeasurements = useMemo(() => groupBySlice(measurementVisible), [measurementVisible]);
  const groupedAnnotations = useMemo(() => groupBySlice(annotationVisible), [annotationVisible]);

  async function handleCreateNote(e: React.FormEvent) {
    e.preventDefault();
    const body = newNoteBody.trim();
    if (!body) return;
    setCreatingNote(true);
    setErr(null);
    try {
      // When the panel is mounted with a series scope, attach the
      // note to that specific series — it's what the radiologist
      // means by "this is a note on what I'm looking at right now".
      // Falls back to study-level when no series is active (e.g. on
      // a study-level dashboard).
      await patientsApi.createNote(
        patientId,
        primarySeriesId
          ? { target_kind: "series", target_id: primarySeriesId, body }
          : { target_kind: "study", target_id: studyId, body },
      );
      setNewNoteBody("");
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : tNotes("saveFailed"));
    } finally {
      setCreatingNote(false);
    }
  }

  function startEditNote(row: Row) {
    if (row.source !== "note") return;
    const note = row.raw as ClinicalNote;
    setEditingNoteId(note.id);
    setEditDraft(note.body);
  }

  async function saveEditNote(noteId: string) {
    const body = editDraft.trim();
    if (!body) return;
    try {
      await patientsApi.updateNote(patientId, noteId, { body });
      setEditingNoteId(null);
      setEditDraft("");
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : tNotes("updateFailed"));
    }
  }

  async function togglePin(row: Row) {
    if (row.source !== "note") return;
    const note = row.raw as ClinicalNote;
    try {
      await patientsApi.updateNote(patientId, note.id, { pinned: !note.pinned });
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : tNotes("updateFailed"));
    }
  }

  async function startEditMarkerLabel(row: Row) {
    if (row.source !== "marker") return;
    const next = await modal.prompt({
      title: tEdit("editLabelTitle"),
      label: tEdit("label"),
      defaultValue: row.body ?? "",
      placeholder: tEdit("labelPlaceholder"),
    });
    if (next == null) return;
    const trimmed = next.trim();
    try {
      // Pass an empty string (not null) when the user clears the
      // field — backend treats both the same; passing null would
      // skip the update on some PATCH wirings, leaving stale text.
      await markersApi.update(row.id, { body: trimmed });
      onMarkerLabelChanged?.(row.id, trimmed);
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("updateFailed"));
    }
  }

  async function handleDelete(row: Row) {
    const ok = await modal.confirm({
      message: t("confirmDelete"),
      destructive: true,
      confirmLabel: t("deleteOne"),
    });
    if (!ok) return;
    try {
      if (row.source === "marker") {
        await markersApi.remove(row.id);
        // Tell the viewer to drop the Cornerstone overlay + filter
        // its measurements list. Done before ``refresh()`` so the
        // sync pass that follows the React state update doesn't
        // race with the panel's own re-fetch.
        onMarkerDeleted?.(row.id);
      } else {
        // Note id has 'note:' prefix
        const noteId = row.id.replace(/^note:/, "");
        await patientsApi.deleteNote(patientId, noteId);
      }
      await refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : t("deleteFailed"));
    }
  }

  function handleExport(format: "json" | "sr") {
    const url = markersApi.exportUrl(studyId, format);
    const token = getStoredToken();
    if (!token) {
      setErr(t("exportFailed"));
      return;
    }
    // Use fetch to attach the bearer token, then download via blob.
    fetch(url, { credentials: "include", headers: { authorization: `Bearer ${token}` } })
      .then(async (resp) => {
        if (!resp.ok) throw new Error(String(resp.status));
        const blob = await resp.blob();
        const a = document.createElement("a");
        const objUrl = URL.createObjectURL(blob);
        a.href = objUrl;
        a.download = `markers-${studyId}.${format === "json" ? "json" : "dcm"}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(objUrl);
      })
      .catch(() => setErr(t("exportFailed")));
  }

  async function handleImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!file) return;
    setBusy(true);
    setErr(null);
    setInfo(null);
    try {
      const out = await markersApi.importFile(studyId, file);
      setInfo(t("imported", { n: out.imported }));
      await refresh();
    } catch (ex) {
      setErr(ex instanceof ApiError ? ex.message : t("importFailed"));
    } finally {
      setBusy(false);
    }
  }

  // Row-rendering helper, factored out so both the new "Misurazioni"
  // section above and the existing "Annotazioni" section can reuse the
  // same JSX (icon + summary + jump-to + edit + delete). Captures the
  // surrounding closures (handleDelete, startEditMarkerLabel, ...);
  // putting it inline keeps the implicit dependency on local state
  // simple compared with extracting a separate sub-component.
  const renderGroups = (groups: Array<[string, Row[]]>, emptyText: string): React.ReactNode => {
    if (groups.length === 0) {
      return (
        <p className="meta" style={{ fontSize: "0.78rem" }}>
          {emptyText}
        </p>
      );
    }
    return (
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {groups.map(([key, bucket]) => (
          <li key={key} style={{ marginBottom: "0.4rem" }}>
            <div
              className="meta"
              style={{
                fontSize: "0.7rem",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                margin: "0.4rem 0 0.2rem",
              }}
            >
              {key === "__across__"
                ? t("groupAcrossSlices")
                : t("groupSlice", { n: Number.parseInt(key.split(":")[1], 10) })}
            </div>
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {bucket.map((r) => {
                const noteRaw = r.source === "note" ? (r.raw as ClinicalNote) : null;
                const isEditing = noteRaw !== null && editingNoteId === noteRaw.id;
                if (isEditing && noteRaw) {
                  return (
                    <li
                      key={r.id}
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: "0.3rem",
                        padding: "0.3rem 0.4rem",
                        borderBottom: "1px solid #2a2f3b",
                      }}
                    >
                      <textarea
                        value={editDraft}
                        onChange={(e) => setEditDraft(e.target.value)}
                        rows={2}
                        style={{ width: "100%", fontSize: "0.78rem", resize: "vertical" }}
                      />
                      <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.25rem" }}>
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => {
                            setEditingNoteId(null);
                            setEditDraft("");
                          }}
                          style={{ fontSize: "0.7rem" }}
                        >
                          {tNotes("cancel")}
                        </button>
                        <button
                          type="button"
                          className="viewer-btn"
                          onClick={() => saveEditNote(noteRaw.id)}
                          disabled={!editDraft.trim()}
                          style={{ fontSize: "0.7rem" }}
                        >
                          {tNotes("save")}
                        </button>
                      </div>
                    </li>
                  );
                }
                const isFocused = r.source === "marker" && focusedMarkerId === r.id;
                return (
                  <li
                    key={r.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "0.4rem",
                      padding: "0.3rem 0.4rem",
                      borderBottom: "1px solid #2a2f3b",
                      fontSize: "0.78rem",
                      background: isFocused ? "rgba(251, 146, 60, 0.12)" : undefined,
                      outline: isFocused ? "1px solid rgba(251, 146, 60, 0.7)" : undefined,
                      borderRadius: isFocused ? 4 : undefined,
                    }}
                  >
                    <span style={{ width: 18, textAlign: "center" }} aria-hidden>
                      {ICONS[r.kind] ?? "•"}
                    </span>
                    <span
                      className="badge"
                      title={
                        r.targetScope === "series"
                          ? "Attached to this series"
                          : "Attached to the whole study"
                      }
                      style={{
                        fontSize: "0.6rem",
                        padding: "0.05rem 0.25rem",
                        background: r.targetScope === "series" ? "#1e40af" : "#475569",
                        color: "#fff",
                        textTransform: "uppercase",
                        letterSpacing: "0.04em",
                      }}
                    >
                      {r.targetScope === "series" ? "ser" : "stu"}
                    </span>
                    <span
                      style={{
                        flex: 1,
                        minWidth: 0,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {r.summary}
                      {noteRaw?.pinned && (
                        <span
                          className="badge"
                          style={{
                            marginLeft: "0.4rem",
                            fontSize: "0.65rem",
                            background: "#e96b1f",
                            color: "#fff",
                          }}
                        >
                          {tNotes("pinnedBadge")}
                        </span>
                      )}
                    </span>
                    {r.voxel && (
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => {
                          if (!r.voxel) return;
                          // Pass the server-side marker id for markers so
                          // the page can flip ``focusedMarkerId`` and the
                          // overlay can pulse the matching outline.
                          // Notes have no spatial outline; the optional
                          // second arg stays undefined.
                          onJumpTo(
                            r.voxel,
                            r.source === "marker" ? r.id : undefined,
                            r.sourceSeriesId,
                          );
                        }}
                        title={t("jumpTo")}
                        style={{ fontSize: "0.7rem", padding: "0.1rem 0.4rem" }}
                      >
                        →
                      </button>
                    )}
                    {noteRaw && (
                      <>
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => startEditNote(r)}
                          title={tNotes("edit")}
                          style={{ fontSize: "0.7rem", padding: "0.1rem 0.4rem" }}
                        >
                          ✎
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          onClick={() => togglePin(r)}
                          title={noteRaw.pinned ? tNotes("unpin") : tNotes("pin")}
                          style={{
                            fontSize: "0.7rem",
                            padding: "0.1rem 0.4rem",
                            color: noteRaw.pinned ? "#e96b1f" : undefined,
                          }}
                        >
                          📌
                        </button>
                      </>
                    )}
                    {r.source === "marker" && (
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => startEditMarkerLabel(r)}
                        title={t("editLabel")}
                        style={{ fontSize: "0.7rem", padding: "0.1rem 0.4rem" }}
                      >
                        ✎
                      </button>
                    )}
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => handleDelete(r)}
                      title={t("deleteOne")}
                      style={{
                        fontSize: "0.7rem",
                        padding: "0.1rem 0.4rem",
                        color: "var(--bv-danger, #d6322e)",
                      }}
                    >
                      ×
                    </button>
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
    );
  };

  return (
    <>
      {/* Misurazioni section: rendered above ANNOTAZIONI per the
          radiologist requirement (measurements first, free-text and
          fiducials below). Always visible — no filter-button row,
          since the section IS the measurements view. */}
      <section style={{ marginTop: "0.6rem" }}>
        <h2>{t("measurementsSectionTitle")}</h2>
        {renderGroups(groupedMeasurements, t("measurementsEmpty"))}
      </section>

      <section style={{ marginTop: "0.6rem" }}>
        <h2>{t("sectionTitle")}</h2>

        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.3rem",
            marginBottom: "0.4rem",
          }}
        >
          {/* Filters narrow the annotations section only — the
              measurements section above is always shown in full, so
              the legacy ``measurements`` filter would be redundant
              and is omitted. */}
          {(
            [
              ["all", t("filterAll")],
              ["text", t("filterText")],
              ["fiducials", t("filterFiducials")],
              ["notes", t("filterNotes")],
            ] as [FilterKey, string][]
          ).map(([k, label]) => (
            <button
              key={k}
              type="button"
              className={filter === k ? "viewer-btn viewer-btn--active" : "viewer-btn"}
              onClick={() => setFilter(k)}
              style={{ fontSize: "0.7rem", padding: "0.15rem 0.45rem" }}
            >
              {label}
            </button>
          ))}
        </div>

        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.3rem",
            marginBottom: "0.5rem",
          }}
        >
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.7rem" }}
            onClick={() => handleExport("json")}
            disabled={busy || rows.length === 0}
            title={t("exportJson")}
          >
            ⤓ JSON
          </button>
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.7rem" }}
            onClick={() => handleExport("sr")}
            disabled={busy || rows.length === 0}
            title={t("exportSr")}
          >
            ⤓ SR
          </button>
          <button
            type="button"
            className="viewer-btn"
            style={{ fontSize: "0.7rem" }}
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
            title={t("import")}
          >
            ⤒ {t("import")}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".json,.dcm,application/json,application/dicom"
            onChange={handleImport}
            style={{ display: "none" }}
          />
        </div>

        <form
          onSubmit={handleCreateNote}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "0.3rem",
            marginBottom: "0.5rem",
          }}
        >
          <textarea
            value={newNoteBody}
            onChange={(e) => setNewNoteBody(e.target.value)}
            placeholder={tNotes("placeholder")}
            rows={2}
            style={{ width: "100%", fontSize: "0.78rem", resize: "vertical" }}
          />
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              type="submit"
              className="viewer-btn"
              disabled={creatingNote || !newNoteBody.trim()}
              style={{ fontSize: "0.7rem", padding: "0.15rem 0.55rem" }}
            >
              {creatingNote ? "…" : tNotes("addNote")}
            </button>
          </div>
        </form>

        {err && (
          <p className="error" style={{ fontSize: "0.78rem", marginTop: "0.4rem" }}>
            {err}
          </p>
        )}
        {info && (
          <p
            className="meta"
            style={{ fontSize: "0.74rem", marginTop: "0.4rem", color: "#6ad19a" }}
          >
            {info}
          </p>
        )}

        {renderGroups(groupedAnnotations, t("empty"))}
      </section>
    </>
  );
}
